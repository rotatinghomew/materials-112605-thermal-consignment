"""导热片寄售履约：片级代管核对、实耗裁决、阶梯计价与账单调节。

只依赖标准库。时间字段一律使用带时区偏移的 ``datetime``，业务归账以
``happened``/``occurred`` 等业务时间为准，``arrived``/``reported`` 只表示
对接方何时能够提供回执，不能代替业务时间。
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from partner import parse_time

FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)

CONTRACT_MODES = ("retro", "marginal")
PACKAGE_STATES = ("sealed", "open")
MOVEMENT_STATES = ("dispatched", "accepted", "reported_lost")
RETURN_TYPES = ("unused", "credit_return")
RECEIPT_PHASES = ("accepted", "settled")

# 实耗裁决原因
R_LOST = "lost_in_transit"          # 遗失期间，任何工厂都不能确认
R_NO_CUSTODY = "no_custody"         # 确认工厂在业务时间并未经手该片
R_UNAUTHORIZED_SITE = "unauthorized_site"
R_UNAUTHORIZED_TRANSFER = "unauthorized_transfer"
R_DUPLICATE = "duplicate_confirmation"

# 账单差额原因
D_LATE = "late_report"
D_CREDIT = "credit_return"
D_RETIER = "re_tier"
BLOCK_UNKNOWN = "acceptance_unknown"


class ConsignmentError(ValueError):
    """交换资料无法通过引用或时序校验。"""


# ---------------------------------------------------------------------------
# 资料装载与校验
# ---------------------------------------------------------------------------
class ExchangeData:
    """读取交换资料并建立索引，校验跨集合引用与时间语义。"""

    COLLECTIONS = ("contracts", "pieces", "orders", "movements",
                   "usage", "returns", "receipts")

    def __init__(self, raw: dict):
        self.raw = raw
        missing = [name for name in self.COLLECTIONS if name not in raw]
        if missing:
            raise ConsignmentError(f"缺少资料集合: {missing}")
        self.index = {
            name: {rec["id"]: rec for rec in raw[name]}
            for name in self.COLLECTIONS
        }
        self._validate()

    @classmethod
    def load(cls, path: str | Path) -> "ExchangeData":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def get(self, collection: str, record_id: str) -> dict:
        try:
            return self.index[collection][record_id]
        except KeyError:
            raise ConsignmentError(
                f"{collection} 中不存在记录 {record_id}") from None

    def contract_of_piece(self, piece_id: str) -> dict:
        return self.get("contracts", self.get("pieces", piece_id)["contract"])

    def order_of_piece(self, piece_id: str) -> dict:
        matches = [o for o in self.raw["orders"]
                   if piece_id in o["pieces"]]
        if len(matches) != 1:
            raise ConsignmentError(
                f"片 {piece_id} 必须恰好落在一张采购委托上，实际 {len(matches)} 张")
        return matches[0]

    def movements_of(self, piece_id: str) -> list[dict]:
        rows = [m for m in self.raw["movements"] if m["piece"] == piece_id]
        rank = {"dispatched": 0, "reported_lost": 1, "accepted": 2}
        return sorted(rows, key=lambda m: (parse_time(m["happened"]),
                                           rank[m["state"]]))

    def _validate(self) -> None:
        for name in self.COLLECTIONS:
            ids = [rec["id"] for rec in self.raw[name]]
            if any(not isinstance(i, str) or not i for i in ids):
                raise ConsignmentError(f"{name} 的 id 必须为非空字符串")
            if len(ids) != len(set(ids)):
                raise ConsignmentError(f"{name} 存在重复 id")
        self._validate_contracts()
        self._validate_pieces()
        self._validate_orders()
        self._validate_movements()
        self._validate_usage()
        self._validate_returns()
        self._validate_receipts()

    def _validate_contracts(self) -> None:
        for rec in self.raw["contracts"]:
            if rec["mode"] not in CONTRACT_MODES:
                raise ConsignmentError(f"合同 {rec['id']} 价格体例未知")
            tiers = rec["tiers"]
            starts = [pair[0] for pair in tiers]
            if starts[0] != 1 or starts != sorted(starts) or len(starts) != len(set(starts)):
                raise ConsignmentError(f"合同 {rec['id']} 阶梯起点须从 1 严格递增")
            if any(not isinstance(pair[1], int) or pair[1] <= 0 for pair in tiers):
                raise ConsignmentError(f"合同 {rec['id']} 单价须为正整数（分）")
            if not rec["sites"] or len(rec["sites"]) != len(set(rec["sites"])):
                raise ConsignmentError(f"合同 {rec['id']} 计量站点非法")
            if not isinstance(rec["return_credit"], bool):
                raise ConsignmentError(f"合同 {rec['id']} return_credit 须为布尔值")

    def _validate_pieces(self) -> None:
        for rec in self.raw["pieces"]:
            contract = self.get("contracts", rec["contract"])
            if rec["site"] not in contract["sites"]:
                raise ConsignmentError(f"片 {rec['id']} 初始地点不在合同站点内")
            if rec["package"] not in PACKAGE_STATES:
                raise ConsignmentError(f"片 {rec['id']} 开封状态非法")

    def _validate_orders(self) -> None:
        for rec in self.raw["orders"]:
            contract = self.get("contracts", rec["contract"])
            if rec["intended_site"] not in contract["sites"]:
                raise ConsignmentError(f"委托 {rec['id']} 指定工厂不在合同内")
            if rec["fallback"] is not None:
                if rec["fallback"] not in contract["sites"]:
                    raise ConsignmentError(f"委托 {rec['id']} 备用工厂不在合同内")
                if rec["fallback"] == rec["intended_site"]:
                    raise ConsignmentError(f"委托 {rec['id']} 备用工厂与原工厂相同")
            if not rec["pieces"] or len(rec["pieces"]) != len(set(rec["pieces"])):
                raise ConsignmentError(f"委托 {rec['id']} 片清单非法")
            for piece_id in rec["pieces"]:
                piece = self.get("pieces", piece_id)
                if piece["contract"] != rec["contract"]:
                    raise ConsignmentError(f"委托 {rec['id']} 引用了别的合同的片")
            parse_time(rec["ship"])

    def _validate_movements(self) -> None:
        by_piece: dict[str, list[dict]] = {}
        for rec in self.raw["movements"]:
            piece = self.get("pieces", rec["piece"])
            contract = self.get("contracts", piece["contract"])
            for side in ("sender", "receiver"):
                if rec[side] not in contract["sites"]:
                    raise ConsignmentError(f"移动 {rec['id']} 的工厂不在合同内")
            if rec["state"] not in MOVEMENT_STATES:
                raise ConsignmentError(f"移动 {rec['id']} 状态非法")
            happened, arrived = parse_time(rec["happened"]), parse_time(rec["arrived"])
            if arrived < happened:
                raise ConsignmentError(f"移动 {rec['id']} 回执早于业务发生")
            by_piece.setdefault(rec["piece"], []).append(rec)
        for piece_id, rows in by_piece.items():
            ordered = self.movements_of(piece_id)
            open_dispatches = []
            for rec in ordered:
                if rec["state"] == "dispatched":
                    open_dispatches.append(rec)
                elif rec["state"] == "accepted":
                    if not open_dispatches:
                        raise ConsignmentError(
                            f"移动 {rec['id']} 缺少对应的发出记录")
                    dispatch = open_dispatches.pop()
                    if rec["sender"] != dispatch["sender"] or \
                            rec["receiver"] != dispatch["receiver"]:
                        raise ConsignmentError(
                            f"移动 {rec['id']} 与发出票 {dispatch['id']} 工厂不一致")
                # reported_lost 可由工厂独立上报，不强制配对原票

    def _validate_usage(self) -> None:
        seen_seq: dict[tuple[str, str], int] = {}
        for rec in self.raw["usage"]:
            if not isinstance(rec["site"], str) or not rec["site"]:
                raise ConsignmentError(f"实耗 {rec['id']} 工厂标识非法")
            piece = self.get("pieces", rec["piece"])
            contract = self.get("contracts", piece["contract"])
            # 合同外站点不在装载时拒收：证据要留存，由裁决层挂起并转认领
            occurred, reported = parse_time(rec["occurred"]), parse_time(rec["reported"])
            if reported < occurred:
                raise ConsignmentError(f"实耗 {rec['id']} 回报早于业务发生")
            key = (contract["id"], occurred.strftime("%Y-%m-%d"))
            if key in seen_seq and seen_seq[key] == rec["seq"]:
                raise ConsignmentError(f"合同 {key[0]} 当日 seq 重复: {rec['seq']}")
            seen_seq[key] = rec["seq"]

    def _validate_returns(self) -> None:
        for rec in self.raw["returns"]:
            piece = self.get("pieces", rec["piece"])
            if rec["type"] not in RETURN_TYPES:
                raise ConsignmentError(f"退回 {rec['id']} 类型非法")
            occurred, reported = parse_time(rec["occurred"]), parse_time(rec["reported"])
            if reported < occurred:
                raise ConsignmentError(f"退回 {rec['id']} 回报早于业务发生")
            if rec["type"] == "unused":
                if rec["usage"] is not None:
                    raise ConsignmentError(f"未消耗退回 {rec['id']} 不应关联实耗")
            else:
                if rec["usage"] is None:
                    raise ConsignmentError(f"抵扣退回 {rec['id']} 缺少原实耗引用")
                usage = self.get("usage", rec["usage"])
                if usage["piece"] != rec["piece"]:
                    raise ConsignmentError(f"退回 {rec['id']} 与原实耗不是同一片")
                if parse_time(usage["occurred"]) >= occurred:
                    raise ConsignmentError(f"退回 {rec['id']} 早于原实耗发生")

    def _validate_receipts(self) -> None:
        invoices = set()
        for rec in self.raw["receipts"]:
            if rec["phase"] not in RECEIPT_PHASES:
                raise ConsignmentError(f"回执 {rec['id']} 阶段非法")
            if rec["invoice"] in invoices:
                raise ConsignmentError(f"账单号 {rec['invoice']} 重复")
            invoices.add(rec["invoice"])
            happened, arrived = parse_time(rec["happened"]), parse_time(rec["arrived"])
            if arrived < happened:
                raise ConsignmentError(f"回执 {rec['id']} 到达早于受理")
            contracts = {self.contract_of_piece(
                self.get("usage", u)["piece"])["id"] for u in rec["usages"]}
            if len(contracts) != 1:
                raise ConsignmentError(f"回执 {rec['id']} 跨了多个合同")
            if not rec["usages"] or len(rec["usages"]) != len(set(rec["usages"])):
                raise ConsignmentError(f"回执 {rec['id']} 明细非法")

    # -- 时点可见性：到达/回报时间决定对接方何时能提供该记录 --------------
    def visible(self, as_of: datetime) -> "ExchangeData":
        raw = copy.deepcopy(self.raw)
        raw["movements"] = [r for r in raw["movements"]
                            if parse_time(r["arrived"]) <= as_of]
        raw["usage"] = [r for r in raw["usage"]
                        if parse_time(r["reported"]) <= as_of]
        raw["returns"] = [r for r in raw["returns"]
                          if parse_time(r["reported"]) <= as_of]
        raw["receipts"] = [r for r in raw["receipts"]
                           if parse_time(r["arrived"]) <= as_of]
        return ExchangeData(raw)


# ---------------------------------------------------------------------------
# 片级代管台账
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Custody:
    piece: str
    site: str | None           # 当前合法代管工厂；在途/遗失时仍记最后责任工厂
    status: str                # at_site | in_transit | lost
    package: str
    to_site: str | None = None
    evidence: tuple[str, ...] = ()


class CustodyLedger:
    """按业务时间回放移动记录，给出任意时点的代管地点与开封状态。"""

    def __init__(self, data: ExchangeData):
        self.data = data

    def state_at(self, piece_id: str, instant: datetime) -> Custody:
        piece = self.data.get("pieces", piece_id)
        state = Custody(piece_id, piece["site"], "at_site", piece["package"],
                        evidence=())
        for mov in self.data.movements_of(piece_id):
            if parse_time(mov["happened"]) > instant:
                break
            if mov["state"] == "dispatched":
                state = Custody(piece_id, mov["sender"], "in_transit",
                                state.package, mov["receiver"],
                                state.evidence + (mov["id"],))
            elif mov["state"] == "reported_lost":
                state = Custody(piece_id, mov["sender"], "lost",
                                state.package, mov["receiver"],
                                state.evidence + (mov["id"],))
            else:  # accepted
                state = Custody(piece_id, mov["receiver"], "at_site",
                                state.package, None,
                                state.evidence + (mov["id"],))
        return state


# ---------------------------------------------------------------------------
# 实耗裁决
# ---------------------------------------------------------------------------
@dataclass
class UsageDecision:
    usage: str
    piece: str
    site: str
    occurred: datetime
    period: str
    settleable: bool
    reason: str | None = None
    evidence: list[str] = field(default_factory=list)


@dataclass
class Claim:
    id: str
    piece: str
    site: str
    usage: str
    reason: str
    opened: datetime
    evidence: list[str]
    status: str = "open"        # 认领入口，暂停自动出账期间一直开放


class UsageAdjudicator:
    """只有合法经手该片的工厂才能确认实耗；越权实耗转认领而非重回库存。"""

    def __init__(self, data: ExchangeData, ledger: CustodyLedger | None = None):
        self.data = data
        self.ledger = ledger or CustodyLedger(data)
        self.decisions: dict[str, UsageDecision] = {}
        self.claims: list[Claim] = []
        self._adjudicate()

    def _adjudicate(self) -> None:
        ordered = sorted(self.data.raw["usage"],
                         key=lambda u: (parse_time(u["occurred"]), u["seq"]))
        consumed_pieces: set[str] = set()
        for rec in ordered:
            piece_id = rec["piece"]
            occurred = parse_time(rec["occurred"])
            evidence = [rec["id"]]
            decision = UsageDecision(
                usage=rec["id"], piece=piece_id, site=rec["site"],
                occurred=occurred, period=occurred.strftime("%Y-%m"),
                settleable=False, evidence=evidence)

            contract = self.data.contract_of_piece(piece_id)
            order = self.data.order_of_piece(piece_id)
            custody = self.ledger.state_at(piece_id, occurred)
            evidence.extend(custody.evidence)
            evidence.append(order["id"])

            if piece_id in consumed_pieces:
                decision.reason = R_DUPLICATE
            elif custody.status == "lost":
                # 遗失期间不能由任何工厂确认使用（即使该工厂在合同站点内）
                decision.reason = R_LOST
            elif rec["site"] not in contract["sites"]:
                decision.reason = R_UNAUTHORIZED_SITE
            elif custody.status != "at_site" or custody.site != rec["site"]:
                decision.reason = R_NO_CUSTODY
            else:
                receiver_move = next(
                    (m for m in self.data.movements_of(piece_id)
                     if m["state"] == "accepted"
                     and parse_time(m["happened"]) <= occurred
                     and m["receiver"] == rec["site"]), None)
                if receiver_move is not None:
                    allowed = (rec["site"] == order["intended_site"]
                               or rec["site"] == order["fallback"])
                    if not allowed:
                        decision.reason = R_UNAUTHORIZED_TRANSFER
                # 无移动记录时，片初始就在该合同站点（如 P09 在 SHU），合法经手
            if decision.reason is None:
                decision.settleable = True
                consumed_pieces.add(piece_id)
            else:
                self.claims.append(Claim(
                    id=f"CLM-{rec['id']}", piece=piece_id, site=rec["site"],
                    usage=rec["id"], reason=decision.reason,
                    opened=occurred, evidence=evidence))
            self.decisions[rec["id"]] = decision

    def settleable(self) -> list[UsageDecision]:
        return [d for d in self.decisions.values() if d.settleable]

    def claim_for_piece(self, piece_id: str) -> Claim | None:
        return next((c for c in self.claims if c.piece == piece_id), None)


# ---------------------------------------------------------------------------
# 阶梯计价
# ---------------------------------------------------------------------------
@dataclass
class UsageLine:
    usage: str
    piece: str
    site: str
    seq: int
    position: int
    unit_price: int
    amount: int
    period: str


@dataclass
class CreditLine:
    return_id: str
    usage: str
    piece: str
    amount: int
    period: str           # 一律抵减原实耗所在账期


@dataclass
class PeriodBill:
    contract: str
    buyer: str
    period: str
    lines: list[UsageLine]
    credits: list[CreditLine]
    gross_qty: int
    gross_amount: int
    credit_amount: int
    net_qty: int
    net_amount: int
    tier_price: int


def tier_price(tiers: list[list[int]], position: int) -> int:
    price = tiers[0][1]
    for start, unit in tiers:
        if start <= position:
            price = unit
        else:
            break
    return price


class PricingEngine:
    """按合同归集两个工厂的确认净用量，依现场顺序 seq 计价。"""

    def __init__(self, data: ExchangeData, adjudicator: UsageAdjudicator):
        self.data = data
        self.adj = adjudicator
        self.bills: dict[tuple[str, str], PeriodBill] = {}
        self._price_all()

    def _price_all(self) -> None:
        by_contract: dict[str, list[UsageDecision]] = {}
        for decision in self.adj.settleable():
            contract_id = self.data.contract_of_piece(decision.piece)["id"]
            by_contract.setdefault(contract_id, []).append(decision)
        for contract_id, decisions in by_contract.items():
            contract = self.data.get("contracts", contract_id)
            decisions.sort(key=lambda d: (d.occurred.date(),
                                          self.data.get("usage", d.usage)["seq"]))
            periods = sorted({d.period for d in decisions})
            for period in periods:
                self.bills[(contract_id, period)] = self._price_period(
                    contract, period, [d for d in decisions if d.period == period])

    def _price_period(self, contract: dict, period: str,
                      decisions: list[UsageDecision]) -> PeriodBill:
        credits: list[CreditLine] = []
        for ret in self.data.raw["returns"]:
            if ret["type"] != "credit_return" or not contract["return_credit"]:
                continue
            if self.data.contract_of_piece(ret["piece"])["id"] != contract["id"]:
                continue
            usage_id = ret["usage"]
            decision = self.adj.decisions.get(usage_id)
            if decision is None or not decision.settleable \
                    or decision.period != period:
                continue
            credits.append(CreditLine(
                return_id=ret["id"], usage=usage_id, piece=ret["piece"],
                amount=0, period=period))  # 金额在单价确定后回填
        credited_usage = {c.usage for c in credits}

        lines: list[UsageLine] = []
        for position, decision in enumerate(decisions, start=1):
            rec = self.data.get("usage", decision.usage)
            lines.append(UsageLine(
                usage=decision.usage, piece=decision.piece,
                site=decision.site, seq=rec["seq"], position=position,
                unit_price=0, amount=0, period=period))

        if contract["mode"] == "marginal":
            for line in lines:
                line.unit_price = tier_price(contract["tiers"], line.position)
                line.amount = line.unit_price
        else:  # retro：档位由全期确认净用量决定，对净存量全量追补
            net_qty = len(lines) - len(credited_usage)
            uniform = tier_price(contract["tiers"], net_qty)
            for line in lines:
                line.unit_price = uniform
                line.amount = uniform

        line_by_usage = {line.usage: line for line in lines}
        for credit in credits:
            credit.amount = line_by_usage[credit.usage].amount

        gross_amount = sum(line.amount for line in lines)
        credit_amount = sum(c.amount for c in credits)
        net_qty = len(lines) - len(credits)
        return PeriodBill(
            contract=contract["id"], buyer=contract["buyer"], period=period,
            lines=lines, credits=credits, gross_qty=len(lines),
            gross_amount=gross_amount, credit_amount=credit_amount,
            net_qty=net_qty, net_amount=gross_amount - credit_amount,
            tier_price=lines[0].unit_price if lines else 0)

    def bill(self, contract_id: str, period: str) -> PeriodBill | None:
        return self.bills.get((contract_id, period))

    def line(self, usage_id: str) -> UsageLine | None:
        for bill in self.bills.values():
            for line in bill.lines:
                if line.usage == usage_id:
                    return line
        return None

    def invoice_amount(self, receipt: dict) -> int:
        """按出账时回执所列明细重算原账单金额（原应付保留）。"""
        contract = self.data.contract_of_piece(
            self.data.get("usage", receipt["usages"][0])["piece"])
        if contract["mode"] == "marginal":
            return sum(self.line(u).amount for u in receipt["usages"])
        # retro：出账时按当时已确认的数量整体落档
        qty = len(receipt["usages"])
        return qty * tier_price(contract["tiers"], qty)


# ---------------------------------------------------------------------------
# 账单调节
# ---------------------------------------------------------------------------
@dataclass
class InvoiceRecord:
    receipt_id: str
    invoice: str
    contract: str
    buyer: str
    period: str
    usage_ids: list[str]
    phase: str
    amount: int
    outstanding: int
    happened: datetime
    arrived: datetime


@dataclass
class Difference:
    kind: str
    buyer: str
    contract: str
    period: str
    ref_id: str
    piece: str | None
    amount: int                  # 带符号：正为补付，负为抵减
    invoice: str | None
    detail: str


@dataclass
class BlockedItem:
    ref_id: str
    piece: str | None
    reason: str
    invoice_seen: str | None
    detail: str


@dataclass
class ScopeReport:
    as_of: datetime
    differences: list[Difference]
    blocked: list[BlockedItem]
    determinable_invoices: list[str]
    determinable_usages: list[str]
    next_payable_delta: dict[str, int]


class BillingService:
    """对照出账系统回执，给出下一账期可确定的差额与不可重发范围。"""

    def __init__(self, data: ExchangeData, adjudicator: UsageAdjudicator,
                 pricing: PricingEngine, full_data: ExchangeData | None = None):
        self.data = data
        # 失联判断需要全量资料：当前时点不可见的回执仍能暴露“哪些明细受理状态不明”
        self.full_data = full_data or data
        self.adj = adjudicator
        self.pricing = pricing

    def invoices(self) -> list[InvoiceRecord]:
        rows = []
        for rec in self.data.raw["receipts"]:
            contract = self.data.contract_of_piece(
                self.data.get("usage", rec["usages"][0])["piece"])
            amount = self.pricing.invoice_amount(rec)
            rows.append(InvoiceRecord(
                receipt_id=rec["id"], invoice=rec["invoice"],
                contract=contract["id"], buyer=contract["buyer"],
                period=rec["period"], usage_ids=list(rec["usages"]),
                phase=rec["phase"], amount=amount,
                outstanding=amount if rec["phase"] == "accepted" else 0,
                happened=parse_time(rec["happened"]),
                arrived=parse_time(rec["arrived"])))
        return sorted(rows, key=lambda r: r.happened)

    def arrears(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for inv in self.invoices():
            if inv.outstanding:
                out[inv.buyer] = out.get(inv.buyer, 0) + inv.outstanding
        return out

    def next_bill_differences(self, as_of: datetime = FAR_FUTURE) -> ScopeReport:
        visible_receipts = [r for r in self.data.raw["receipts"]
                            if parse_time(r["arrived"]) <= as_of]
        visible_ids = {r["id"] for r in visible_receipts}
        usage_to_receipt: dict[str, dict] = {}
        for rec in visible_receipts:
            for usage_id in rec["usages"]:
                usage_to_receipt[usage_id] = rec
        # 全量资料里出现过、但当前时点查不到受理状态的回执
        unseen_usages = {u for r in self.full_data.raw["receipts"]
                         if r["id"] not in visible_ids
                         for u in r["usages"]}

        differences: list[Difference] = []
        blocked: list[BlockedItem] = []
        determinable_invoices = [r["invoice"] for r in visible_receipts]
        determinable_usages = list(usage_to_receipt)

        for bill in self.pricing.bills.values():
            contract = self.data.get("contracts", bill.contract)
            # 1) 迟报实耗：尚未出现在任何可知账单上
            for line in bill.lines:
                receipt = usage_to_receipt.get(line.usage)
                if receipt is not None:
                    continue
                if line.usage in unseen_usages:
                    blocked.append(BlockedItem(
                        ref_id=line.usage, piece=line.piece,
                        reason=BLOCK_UNKNOWN, invoice_seen=None,
                        detail="回执受理状态无法确认，原应付不得重发"))
                    continue
                differences.append(Difference(
                    kind=D_LATE, buyer=bill.buyer, contract=bill.contract,
                    period=bill.period, ref_id=line.usage, piece=line.piece,
                    amount=line.amount, invoice=None,
                    detail=f"迟报实耗按业务日期归账 {bill.period}"))
                determinable_usages.append(line.usage)

            # 2) 可抵退回：抵减原件所在账期
            for credit in bill.credits:
                receipt = usage_to_receipt.get(credit.usage)
                if receipt is None:
                    blocked.append(BlockedItem(
                        ref_id=credit.return_id, piece=credit.piece,
                        reason=BLOCK_UNKNOWN,
                        invoice_seen=None,
                        detail="原账单受理状态不明，抵扣暂不能确定"))
                    continue
                differences.append(Difference(
                    kind=D_CREDIT, buyer=bill.buyer, contract=bill.contract,
                    period=bill.period, ref_id=credit.return_id,
                    piece=credit.piece, amount=-credit.amount,
                    invoice=receipt["invoice"],
                    detail="抵扣退回冲减原件所在账期"))

            # 3) 重定阶梯：retro 下新资料改变档位，对已受理明细追补差价
            if contract["mode"] != "retro":
                continue
            for receipt in visible_receipts:
                if receipt["period"] != bill.period:
                    continue
                if self.data.contract_of_piece(
                        self.data.get("usage", receipt["usages"][0])["piece"])["id"] \
                        != bill.contract:
                    continue
                old_price = tier_price(contract["tiers"], len(receipt["usages"]))
                if old_price == bill.tier_price:
                    continue
                credited = {c.usage for c in bill.credits}
                for usage_id in receipt["usages"]:
                    if usage_id in credited:
                        continue
                    line = self.pricing.line(usage_id)
                    delta = bill.tier_price - old_price
                    differences.append(Difference(
                        kind=D_RETIER, buyer=bill.buyer, contract=bill.contract,
                        period=bill.period, ref_id=usage_id,
                        piece=line.piece if line else None, amount=delta,
                        invoice=receipt["invoice"],
                        detail=f"阶梯重定 {old_price} -> {bill.tier_price}"))
                    determinable_usages.append(usage_id)

        delta: dict[str, int] = {}
        for diff in differences:
            delta[diff.buyer] = delta.get(diff.buyer, 0) + diff.amount
        return ScopeReport(
            as_of=as_of, differences=differences, blocked=blocked,
            determinable_invoices=sorted(set(determinable_invoices)),
            determinable_usages=sorted(set(determinable_usages)),
            next_payable_delta=dict(sorted(delta.items())))


# ---------------------------------------------------------------------------
# 岗位视图与恢复上传
# ---------------------------------------------------------------------------
@dataclass
class PieceRow:
    piece: str
    contract: str
    buyer: str
    order: str
    site: str | None
    status: str
    package: str
    usage_id: str | None
    usage_state: str | None
    return_id: str | None
    return_type: str | None
    invoice: str | None
    billed_amount: int
    credit_amount: int
    claim_id: str | None
    returnable: bool
    transferable: bool
    available_to_issue: bool


class ConsignmentService:
    """对外门面：代管台账、实耗裁决、计价、账单调节与岗位视图。"""

    def __init__(self, data: ExchangeData, as_of: datetime = FAR_FUTURE):
        self.as_of = as_of
        self.full_data = data
        self.data = data.visible(as_of) if as_of != FAR_FUTURE else data
        self.ledger = CustodyLedger(self.data)
        self.adjudicator = UsageAdjudicator(self.data, self.ledger)
        self.pricing = PricingEngine(self.data, self.adjudicator)
        self.billing = BillingService(self.data, self.adjudicator,
                                      self.pricing, full_data=data)

    # -- 库管：实物还能退回或转交，被拒绝的片不得重回可领用清单 ----------
    def warehouse_view(self) -> list[dict]:
        rows = []
        unused_returns = {r["piece"]: r for r in self.data.raw["returns"]
                          if r["type"] == "unused"}
        for piece in self.data.raw["pieces"]:
            pid = piece["id"]
            decision = next((d for d in self.adjudicator.decisions.values()
                             if d.piece == pid), None)
            claim = self.adjudicator.claim_for_piece(pid)
            custody = self.ledger.state_at(pid, self.as_of)
            unused_return = unused_returns.get(pid)
            if decision is not None and decision.settleable:
                status = "consumed"
            elif claim is not None:
                status = f"suspended:{claim.reason}"
            elif unused_return is not None:
                status = "returned_unused"
            else:
                status = custody.status
            order = self.data.order_of_piece(pid)
            on_hand = status == "at_site"
            returnable = on_hand
            transferable = on_hand and order["fallback"] is not None
            rows.append({
                "piece": pid,
                "contract": piece["contract"],
                "site": custody.site,
                "status": status,
                "package": custody.package,
                "returnable": returnable,
                "transferable": transferable,
                "available_to_issue": on_hand,
                "reasons": self._warehouse_reasons(pid, status, custody, order),
            })
        return sorted(rows, key=lambda r: r["piece"])

    @staticmethod
    def _warehouse_reasons(pid, status, custody, order) -> list[str]:
        reasons = []
        if custody.package == "open":
            reasons.append("已开封")
        if status == "in_transit":
            reasons.append(f"在途，目的地 {custody.to_site}")
        if status.startswith("suspended"):
            reasons.append("越权/遗失实耗，挂认领，不得重新领用")
        if status == "returned_unused":
            reasons.append("未消耗退回，库存已恢复")
        if status == "at_site" and order["fallback"] is None:
            reasons.append("委托未指定备用工厂，不可转厂")
        return reasons

    # -- 采购：退货判定、实际使用、账单金额逐片对上 ----------------------
    def procurement_view(self) -> list[PieceRow]:
        rows = []
        usage_by_piece = {u["piece"]: u for u in self.data.raw["usage"]}
        returns_by_piece = {r["piece"]: r for r in self.data.raw["returns"]}
        invoice_by_usage = {u: r for r in self.data.raw["receipts"]
                            for u in r["usages"]}
        for piece in self.data.raw["pieces"]:
            pid = piece["id"]
            usage = usage_by_piece.get(pid)
            decision = self.adjudicator.decisions.get(usage["id"]) if usage else None
            ret = returns_by_piece.get(pid)
            claim = self.adjudicator.claim_for_piece(pid)
            line = self.pricing.line(usage["id"]) if usage else None
            receipt = invoice_by_usage.get(usage["id"]) if usage else None
            credit = next((c for bill in self.pricing.bills.values()
                           for c in bill.credits if c.piece == pid), None)
            custody = self.ledger.state_at(pid, self.as_of)
            billed = self._billed_amount(receipt, usage["id"]) if receipt and usage else 0
            if decision and decision.settleable:
                status = "consumed"
            elif claim:
                status = f"suspended:{claim.reason}"
            elif ret and ret["type"] == "unused":
                status = "returned_unused"
            else:
                status = custody.status
            order = self.data.order_of_piece(pid)
            on_hand = status == "at_site"
            rows.append(PieceRow(
                piece=pid, contract=piece["contract"],
                buyer=self.data.get("contracts", piece["contract"])["buyer"],
                order=order["id"], site=custody.site, status=status,
                package=custody.package,
                usage_id=usage["id"] if usage else None,
                usage_state=("settleable" if decision and decision.settleable
                             else claim.reason if claim else None),
                return_id=ret["id"] if ret else None,
                return_type=ret["type"] if ret else None,
                invoice=receipt["invoice"] if receipt else None,
                billed_amount=billed,
                credit_amount=credit.amount if credit else 0,
                claim_id=claim.id if claim else None,
                returnable=on_hand,
                transferable=on_hand and order["fallback"] is not None,
                available_to_issue=on_hand))
        return sorted(rows, key=lambda r: r.piece)

    def _billed_amount(self, receipt: dict, usage_id: str) -> int:
        contract = self.data.contract_of_piece(
            self.data.get("usage", usage_id)["piece"])
        if contract["mode"] == "marginal":
            return self.pricing.line(usage_id).amount
        return tier_price(contract["tiers"], len(receipt["usages"]))

    def invoices_view(self) -> list[dict]:
        result = []
        for inv in self.billing.invoices():
            result.append({
                "receipt": inv.receipt_id, "invoice": inv.invoice,
                "buyer": inv.buyer, "contract": inv.contract,
                "period": inv.period, "phase": inv.phase,
                "usages": inv.usage_ids, "amount": inv.amount,
                "outstanding": inv.outstanding,
                "closed": inv.phase == "settled",
            })
        return result

    def claims_view(self) -> list[dict]:
        return [asdict(claim) | {"opened": claim.opened.isoformat()}
                for claim in self.adjudicator.claims]

    def scope_report(self, as_of: datetime | None = None) -> ScopeReport:
        return self.billing.next_bill_differences(as_of or self.as_of)

    # -- 生产恢复后上传用量：走同一套处理，说明对下一张应付单的影响 ------
    def apply_recovered_usage(self, new_usages: list[dict],
                              now: datetime) -> dict:
        raw = copy.deepcopy(self.data.raw)
        existing = {u["id"] for u in raw["usage"]}
        for rec in new_usages:
            if rec["id"] in existing:
                raise ConsignmentError(f"实耗 {rec['id']} 已存在，不能重复上传")
            if parse_time(rec["reported"]) > now:
                raise ConsignmentError(f"实耗 {rec['id']} 回报时间晚于上传时点")
            raw["usage"].append(rec)
        recovered = ConsignmentService(ExchangeData(raw), as_of=now)
        before = self.billing.next_bill_differences(now).next_payable_delta
        after = recovered.scope_report(now)
        new_ids = {rec["id"] for rec in new_usages}
        impacts = [d for d in after.differences if d.ref_id in new_ids]
        claims = [c for c in recovered.claims_view()
                  if c["usage"] in new_ids]
        return {
            "uploaded": sorted(new_ids),
            "impacts": [asdict(d) for d in impacts],
            "claims": claims,
            "next_payable_before": before,
            "next_payable_after": after.next_payable_delta,
            "changes_next_payable": after.next_payable_delta != before,
        }


def to_jsonable(obj):
    """把 dataclass/ datetime 结构转成可 JSON 序列化的普通对象。"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "__dataclass_fields__"):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
