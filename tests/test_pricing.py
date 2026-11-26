"""阶梯计价：marginal/retro 两种体例、跨厂归集、现场顺序与迟报归账。"""
import copy
import unittest

from consignment import ConsignmentService, ExchangeData, tier_price
from tests.helpers import load_fixture, raw_fixture


class TierPriceTests(unittest.TestCase):
    TIERS = [[1, 8200], [3, 7600], [5, 7000]]

    def test_tier_lookup(self):
        self.assertEqual(tier_price(self.TIERS, 1), 8200)
        self.assertEqual(tier_price(self.TIERS, 2), 8200)
        self.assertEqual(tier_price(self.TIERS, 3), 7600)
        self.assertEqual(tier_price(self.TIERS, 6), 7000)


class FixturePricingTests(unittest.TestCase):
    def setUp(self):
        self.svc = ConsignmentService(load_fixture())

    def test_retro_applies_final_tier_to_all_net_units(self):
        bill = self.svc.pricing.bill("C-31", "2027-01")
        # 4 条确认实耗，1 条 credit_return，净用量 3 -> 7600 档，全量追补
        self.assertEqual(bill.gross_qty, 4)
        self.assertEqual(bill.net_qty, 3)
        self.assertEqual(bill.tier_price, 7600)
        self.assertEqual({line.unit_price for line in bill.lines}, {7600})
        self.assertEqual(bill.gross_amount, 4 * 7600)
        self.assertEqual(bill.credit_amount, 7600)
        self.assertEqual(bill.net_amount, 3 * 7600)

    def test_marginal_prices_only_units_above_each_tier(self):
        bill = self.svc.pricing.bill("C-32", "2027-01")
        # U-5 被裁决挂起，只剩 U-6/U-7/U-8 三条
        self.assertEqual([line.usage for line in bill.lines],
                         ["U-6", "U-7", "U-8"])
        self.assertEqual([line.unit_price for line in bill.lines],
                         [9100, 9100, 8300])
        self.assertEqual(bill.net_amount, 26500)
        self.assertEqual(bill.credit_amount, 0)

    def test_pricing_aggregates_two_sites_under_one_contract(self):
        bill = self.svc.pricing.bill("C-32", "2027-01")
        sites = {line.site for line in bill.lines}
        self.assertEqual(sites, {"HU", "SHU"})

    def test_pricing_follows_on_site_seq_order(self):
        bill = self.svc.pricing.bill("C-32", "2027-01")
        self.assertEqual([line.seq for line in bill.lines], [6, 7, 8])

    def test_late_report_is_booked_to_business_period(self):
        # U-3/U-8 在 2 月才回报，但业务日期都在 1 月
        for usage_id in ("U-3", "U-8"):
            line = self.svc.pricing.line(usage_id)
            self.assertEqual(line.period, "2027-01")

    def test_credit_return_offsets_original_period(self):
        # RT-2 发生在 2 月，但抵减 U-2 所在的 1 月账期
        bill = self.svc.pricing.bill("C-31", "2027-01")
        credit = next(c for c in bill.credits if c.return_id == "RT-2")
        self.assertEqual(credit.period, "2027-01")
        self.assertEqual(credit.usage, "U-2")
        self.assertEqual(credit.amount, 7600)

    def test_credit_return_disabled_by_contract(self):
        data = copy.deepcopy(load_fixture().raw)
        data["returns"].append({
            "id": "RT-X", "piece": "P08", "type": "credit_return",
            "usage": "U-6", "occurred": "2027-02-05T10:00:00+08:00",
            "reported": "2027-02-05T10:05:00+08:00"})
        svc = ConsignmentService(ExchangeData(data))
        bill = svc.pricing.bill("C-32", "2027-01")
        self.assertFalse(any(c.return_id == "RT-X" for c in bill.credits))


class RetroReTierTests(unittest.TestCase):
    def _retro_data(self, usage_count: int) -> ExchangeData:
        raw = raw_fixture()
        # 构造一个独立 retro 合同，阶梯 1->100, 3->80, 5->60
        raw["contracts"] = [{
            "id": "C-R", "buyer": "SU-R", "sites": ["A", "B"],
            "mode": "retro", "tiers": [[1, 100], [3, 80], [5, 60]],
            "return_credit": True}]
        raw["pieces"] = [
            {"id": f"R{i}", "contract": "C-R",
             "site": "A" if i % 2 else "B", "package": "sealed"}
            for i in range(1, usage_count + 1)]
        raw["orders"] = [{
            "id": "SO-R", "contract": "C-R", "intended_site": "A",
            "fallback": "B",
            "pieces": [f"R{i}" for i in range(1, usage_count + 1)],
            "ship": "2027-01-10T08:00:00+08:00"}]
        raw["movements"] = []
        raw["usage"] = [{
            "id": f"RU-{i}", "piece": f"R{i}",
            "site": "A" if i % 2 else "B", "seq": i,
            "occurred": f"2027-01-20T09:{10 + i:02d}:00+08:00",
            "reported": f"2027-01-20T09:{20 + i:02d}:00+08:00"}
            for i in range(1, usage_count + 1)]
        raw["returns"] = []
        raw["receipts"] = []
        return ExchangeData(raw)

    def test_retro_tier_moves_when_more_usages_arrive(self):
        svc2 = ConsignmentService(self._retro_data(2))
        self.assertEqual(svc2.pricing.bill("C-R", "2027-01").tier_price, 100)
        svc5 = ConsignmentService(self._retro_data(5))
        bill = svc5.pricing.bill("C-R", "2027-01")
        self.assertEqual(bill.tier_price, 60)
        self.assertEqual(bill.net_amount, 5 * 60)


if __name__ == "__main__":
    unittest.main()
