"""账单调节：原应付保留、差额归因、失联不重发与封账/欠款并查。"""
import copy
import unittest

from consignment import (
    BLOCK_UNKNOWN, D_CREDIT, D_LATE, D_RETIER, ConsignmentService,
    ExchangeData,
)
from tests.helpers import at, load_fixture


class BillingFixtureTests(unittest.TestCase):
    def setUp(self):
        self.svc = ConsignmentService(load_fixture())

    def test_original_invoice_payable_is_retained(self):
        invoices = {i.invoice: i for i in self.svc.billing.invoices()}
        self.assertEqual(invoices["I-01"].amount, 22800)
        self.assertEqual(invoices["I-01"].outstanding, 22800)
        self.assertEqual(invoices["I-02"].amount, 18200)
        self.assertEqual(invoices["I-02"].outstanding, 0)  # settled
        self.assertEqual(self.svc.billing.arrears(), {"SU-1": 22800})

    def test_full_visibility_differences_are_attributed(self):
        report = self.svc.scope_report()
        kinds = {(d.kind, d.ref_id): d for d in report.differences}
        self.assertEqual(kinds[(D_LATE, "U-3")].amount, 7600)
        self.assertEqual(kinds[(D_LATE, "U-3")].buyer, "SU-1")
        self.assertEqual(kinds[(D_CREDIT, "RT-2")].amount, -7600)
        self.assertEqual(kinds[(D_CREDIT, "RT-2")].invoice, "I-01")
        self.assertEqual(kinds[(D_LATE, "U-8")].amount, 8300)
        # SU-1 的迟报与抵扣相抵，下一张应付单净额不变；SU-2 增加 8300
        self.assertEqual(report.next_payable_delta,
                         {"SU-1": 0, "SU-2": 8300})

    def test_suspended_usage_never_enters_billing(self):
        report = self.svc.scope_report()
        refs = {d.ref_id for d in report.differences}
        self.assertNotIn("U-5", refs)
        bill = self.svc.pricing.bill("C-32", "2027-01")
        self.assertNotIn("U-5", [line.usage for line in bill.lines])

    def test_closed_and_outstanding_records_are_both_queryable(self):
        view = {i["invoice"]: i for i in self.svc.invoices_view()}
        self.assertTrue(view["I-02"]["closed"])
        self.assertFalse(view["I-01"]["closed"])
        self.assertEqual(view["I-01"]["outstanding"], 22800)

    def test_unknown_acceptance_blocks_reissue_instead_of_resending(self):
        # 2/3 中午：PAY-1 尚未到达，I-01 上的明细受理状态不明
        svc = ConsignmentService(load_fixture(),
                                 as_of=at("2027-02-03T12:00:00+08:00"))
        report = svc.scope_report()
        blocked_refs = {b.ref_id for b in report.blocked}
        self.assertEqual(blocked_refs, {"U-1", "U-2", "U-4"})
        self.assertTrue(all(b.reason == BLOCK_UNKNOWN for b in report.blocked))
        # 同一片的原应付不得重发：没有任何 SU-1 应付差额
        self.assertEqual(report.next_payable_delta, {})
        self.assertEqual(report.determinable_invoices, ["I-02"])
        self.assertEqual(sorted(report.determinable_usages),
                         ["U-6", "U-7"])

    def test_determinable_scope_excludes_pending_reports(self):
        # 2/1 中午：只有现场即报的实耗可见，迟报全部缺失
        svc = ConsignmentService(load_fixture(),
                                 as_of=at("2027-02-01T12:00:00+08:00"))
        report = svc.scope_report()
        # 尚无任何回执可见，发票范围为空；不凭回报缺失臆造账单
        self.assertEqual(report.determinable_invoices, [])


class ReTierBillingTests(unittest.TestCase):
    def _data(self):
        raw = load_fixture().raw
        raw = copy.deepcopy(raw)
        contract = next(c for c in raw["contracts"] if c["id"] == "C-31")
        # 出账时只确认了 2 条 -> 8200 档；后续补到 4 条 -> 7600 档
        raw["usage"] = [u for u in raw["usage"]
                        if u["id"] in ("U-1", "U-2", "U-3", "U-4")]
        raw["returns"] = []
        raw["receipts"] = [{
            "id": "PAY-X", "invoice": "I-X", "period": "2027-01",
            "usages": ["U-1", "U-2"], "phase": "accepted",
            "happened": "2027-02-01T09:00:00+08:00",
            "arrived": "2027-02-01T10:00:00+08:00"}]
        return ExchangeData(raw), contract

    def test_retro_re_tier_emits_delta_for_already_accepted_units(self):
        data, _ = self._data()
        svc = ConsignmentService(data)
        report = svc.scope_report()
        retier = [d for d in report.differences if d.kind == D_RETIER]
        self.assertEqual({d.ref_id for d in retier}, {"U-1", "U-2"})
        self.assertTrue(all(d.amount == -600 for d in retier))
        late = [d for d in report.differences if d.kind == D_LATE]
        self.assertEqual({d.ref_id for d in late}, {"U-3", "U-4"})
        self.assertTrue(all(d.amount == 7600 for d in late))
        # 买方汇总：-1200 追补差价 + 15200 迟报 = 14000
        self.assertEqual(report.next_payable_delta, {"SU-1": 14000})

    def test_marginal_contract_never_emits_re_tier(self):
        data, contract = self._data()
        contract["mode"] = "marginal"
        svc = ConsignmentService(ExchangeData(data.raw))
        report = svc.scope_report()
        self.assertFalse(any(d.kind == D_RETIER for d in report.differences))


if __name__ == "__main__":
    unittest.main()
