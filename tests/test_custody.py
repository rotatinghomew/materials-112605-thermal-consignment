"""片级代管台账与实耗裁决：代管地点、开封状态、转厂许可与认领入口。"""
import copy
import unittest

from partner import parse_time
from consignment import (
    ConsignmentError, ConsignmentService, CustodyLedger, ExchangeData,
    R_DUPLICATE, R_LOST, R_NO_CUSTODY, R_UNAUTHORIZED_SITE,
    R_UNAUTHORIZED_TRANSFER,
)

from tests.helpers import load_fixture, mutate, at


class CustodyAndAdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.data = load_fixture()
        self.svc = ConsignmentService(self.data)

    def test_movement_chain_tracks_custody(self):
        ledger = CustodyLedger(self.data)
        before = ledger.state_at("P03",
                                 at("2027-01-28T08:00:00+08:00"))
        self.assertEqual((before.site, before.status), ("QIN", "at_site"))

        in_transit = ledger.state_at("P03",
                                     parse_time("2027-01-28T12:00:00+08:00"))
        self.assertEqual(in_transit.status, "in_transit")
        self.assertEqual(in_transit.site, "QIN")
        self.assertEqual(in_transit.to_site, "SHU")

        accepted = ledger.state_at("P03",
                                   parse_time("2027-01-29T11:00:00+08:00"))
        self.assertEqual((accepted.site, accepted.status), ("SHU", "at_site"))
        self.assertIn("MOV-1", accepted.evidence)
        self.assertIn("MOV-2", accepted.evidence)

    def test_late_movement_receipt_does_not_change_business_time_custody(self):
        # MOV-2 的 arrived 在 2 月，但业务时间在 1 月；裁决只看 happened
        decision = self.svc.adjudicator.decisions["U-3"]
        self.assertTrue(decision.settleable)
        self.assertEqual(decision.site, "SHU")

    def test_usage_during_reported_loss_is_never_settleable(self):
        decision = self.svc.adjudicator.decisions["U-5"]
        self.assertFalse(decision.settleable)
        self.assertEqual(decision.reason, R_LOST)
        claim = self.svc.adjudicator.claim_for_piece("P07")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.usage, "U-5")
        # 证据链保留实耗、遗失报告和采购委托
        self.assertEqual(claim.evidence, ["U-5", "MOV-3", "SO-3"])

    def test_suspended_piece_is_not_available_to_issue(self):
        row = next(r for r in self.svc.warehouse_view() if r["piece"] == "P07")
        self.assertFalse(row["available_to_issue"])
        self.assertTrue(row["status"].startswith("suspended"))

    def test_piece_without_custody_cannot_confirm_usage(self):
        # P03 在 1/28 中午仍在途，此时 SHU 不能确认
        data = mutate(self.data, "usage", "U-3",
                      occurred="2027-01-28T12:00:00+08:00",
                      reported="2027-01-28T12:05:00+08:00")
        svc = ConsignmentService(data)
        self.assertEqual(svc.adjudicator.decisions["U-3"].reason, R_NO_CUSTODY)

    def test_transfer_requires_order_authorization(self):
        # SHU 在 C-31 合同站点内，但 SO-2 没有指定备用工厂，P04 转去 QIN 越权
        data = mutate(self.data, "usage", "U-4", site="QIN")
        svc = ConsignmentService(data)
        self.assertEqual(svc.adjudicator.decisions["U-4"].reason,
                         R_NO_CUSTODY)

    def test_site_outside_contract_is_rejected_even_with_custody(self):
        data = mutate(self.data, "usage", "U-1", site="XX")
        svc = ConsignmentService(data)
        self.assertEqual(svc.adjudicator.decisions["U-1"].reason,
                         R_UNAUTHORIZED_SITE)

    def test_lost_period_takes_precedence_over_site_check(self):
        # U-5 的 SHU 本就在合同站点内，但 P07 处于遗失期，仍判 lost
        decision = self.svc.adjudicator.decisions["U-5"]
        self.assertEqual(decision.reason, R_LOST)

    def test_unauthorized_transfer_opens_claim_with_evidence(self):
        data = copy.deepcopy(self.data.raw)
        # 合同引入第三个获准工厂 WU，但 SO-1 只许可 QIN/SHU
        contract = next(c for c in data["contracts"] if c["id"] == "C-31")
        contract["sites"].append("WU")
        # 让 P01 经合法移动到达未被 SO-1 许可的 WU
        data["movements"].append({
            "id": "MOV-X1", "piece": "P01", "sender": "QIN",
            "receiver": "WU", "state": "dispatched",
            "happened": "2027-01-25T09:00:00+08:00",
            "arrived": "2027-01-25T09:30:00+08:00"})
        data["movements"].append({
            "id": "MOV-X2", "piece": "P01", "sender": "QIN",
            "receiver": "WU", "state": "accepted",
            "happened": "2027-01-26T09:00:00+08:00",
            "arrived": "2027-01-26T09:30:00+08:00"})
        data["usage"] = [u for u in data["usage"] if u["id"] != "U-1"]
        data["usage"].append({
            "id": "U-1", "piece": "P01", "site": "WU", "seq": 1,
            "occurred": "2027-01-27T09:00:00+08:00",
            "reported": "2027-01-27T09:05:00+08:00"})
        svc = ConsignmentService(ExchangeData(data))
        decision = svc.adjudicator.decisions["U-1"]
        self.assertFalse(decision.settleable)
        self.assertEqual(decision.reason, R_UNAUTHORIZED_TRANSFER)
        self.assertTrue(svc.adjudicator.claim_for_piece("P01"))

    def test_same_piece_cannot_be_confirmed_by_two_factories(self):
        data = copy.deepcopy(self.data.raw)
        data["usage"].append({
            "id": "U-99", "piece": "P01", "site": "SHU", "seq": 99,
            "occurred": "2027-01-31T09:00:00+08:00",
            "reported": "2027-01-31T09:05:00+08:00"})
        svc = ConsignmentService(ExchangeData(data))
        self.assertTrue(svc.adjudicator.decisions["U-1"].settleable)
        duplicate = svc.adjudicator.decisions["U-99"]
        self.assertEqual(duplicate.reason, R_DUPLICATE)

    def test_invalid_references_and_clocks_are_rejected(self):
        data = copy.deepcopy(self.data.raw)
        data["usage"][0]["piece"] = "NOPE"
        with self.assertRaises(ConsignmentError):
            ExchangeData(data)
        data = copy.deepcopy(self.data.raw)
        data["movements"][0]["arrived"] = "2027-01-01T00:00:00+08:00"
        with self.assertRaises(ConsignmentError):
            ExchangeData(data)

    def test_transferred_piece_is_settled_exactly_once(self):
        # P03 经 QIN 发出、SHU 接手，月末总量若按两边各记一次就会双算
        settleable = [d for d in self.svc.adjudicator.settleable()
                      if d.piece == "P03"]
        self.assertEqual(len(settleable), 1)
        self.assertEqual(settleable[0].site, "SHU")
        line = self.svc.pricing.line("U-3")
        self.assertEqual(line.amount, 7600)

    def test_unused_return_never_creates_a_sale(self):
        rows = {r.piece: r for r in self.svc.procurement_view()}
        p06 = rows["P06"]
        self.assertIsNone(p06.usage_id)
        self.assertEqual(p06.return_type, "unused")
        self.assertEqual(p06.billed_amount, 0)
        self.assertEqual(p06.status, "returned_unused")
        bill = self.svc.pricing.bill("C-31", "2027-01")
        self.assertNotIn("P06", [line.piece for line in bill.lines])


if __name__ == "__main__":
    unittest.main()
