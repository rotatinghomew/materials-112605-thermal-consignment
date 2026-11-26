"""岗位视图与生产恢复上传。"""
import unittest

from consignment import ConsignmentService
from tests.helpers import at, load_fixture


class WarehouseViewTests(unittest.TestCase):
    def setUp(self):
        self.rows = {r["piece"]: r
                     for r in ConsignmentService(load_fixture()).warehouse_view()}

    def test_only_physical_on_hand_pieces_are_returnable(self):
        returnable = {pid for pid, r in self.rows.items() if r["returnable"]}
        # P05 开封但未消耗；P06 已退回；其余已消耗/挂起
        self.assertEqual(returnable, {"P05"})

    def test_rejected_piece_is_not_listed_as_available(self):
        self.assertFalse(self.rows["P07"]["available_to_issue"])
        self.assertIn("认领", " ".join(self.rows["P07"]["reasons"]))

    def test_order_without_fallback_is_not_transferable(self):
        self.assertFalse(self.rows["P05"]["transferable"])
        self.assertIn("不可转厂", " ".join(self.rows["P05"]["reasons"]))

    def test_unused_return_restores_inventory_without_billing(self):
        self.assertEqual(self.rows["P06"]["status"], "returned_unused")
        self.assertFalse(self.rows["P06"]["available_to_issue"])

    def test_open_package_state_is_visible(self):
        self.assertEqual(self.rows["P05"]["package"], "open")
        self.assertIn("已开封", " ".join(self.rows["P05"]["reasons"]))


class ProcurementViewTests(unittest.TestCase):
    def setUp(self):
        svc = ConsignmentService(load_fixture())
        self.rows = {r.piece: r for r in svc.procurement_view()}

    def test_piece_level_tie_out_of_return_usage_and_invoice(self):
        p02 = self.rows["P02"]
        self.assertEqual(p02.usage_id, "U-2")
        self.assertEqual(p02.return_type, "credit_return")
        self.assertEqual(p02.invoice, "I-01")
        self.assertEqual(p02.billed_amount, 7600)
        self.assertEqual(p02.credit_amount, 7600)  # 逐片净额为 0

    def test_late_reported_piece_still_ties_to_usage(self):
        p03 = self.rows["P03"]
        self.assertEqual(p03.usage_id, "U-3")
        self.assertEqual(p03.usage_state, "settleable")
        self.assertIsNone(p03.invoice)  # 尚未进入任何账单

    def test_suspended_piece_carries_claim_reference(self):
        p07 = self.rows["P07"]
        self.assertEqual(p07.claim_id, "CLM-U-5")
        self.assertEqual(p07.billed_amount, 0)


class RecoveredUsageTests(unittest.TestCase):
    def test_recovered_usage_uses_same_pipeline_and_states_next_payable(self):
        svc = ConsignmentService(load_fixture())
        upload = [{
            "id": "U-9", "piece": "P05", "site": "SHU", "seq": 9,
            "occurred": "2027-02-06T09:00:00+08:00",
            "reported": "2027-02-10T09:00:00+08:00"}]
        result = svc.apply_recovered_usage(
            upload, at("2027-02-11T09:00:00+08:00"))
        self.assertEqual(result["uploaded"], ["U-9"])
        self.assertEqual(result["impacts"][0]["kind"], "late_report")
        self.assertEqual(result["impacts"][0]["period"], "2027-02")
        self.assertTrue(result["changes_next_payable"])
        self.assertEqual(result["next_payable_after"]["SU-1"],
                         result["next_payable_before"].get("SU-1", 0) + 8200)

    def test_recovered_usage_that_violates_custody_opens_claim(self):
        svc = ConsignmentService(load_fixture())
        upload = [{
            "id": "U-9", "piece": "P07", "site": "SHU", "seq": 9,
            "occurred": "2027-02-06T09:00:00+08:00",
            "reported": "2027-02-10T09:00:00+08:00"}]
        result = svc.apply_recovered_usage(
            upload, at("2027-02-11T09:00:00+08:00"))
        self.assertEqual(result["impacts"], [])
        self.assertEqual(result["claims"][0]["piece"], "P07")
        self.assertFalse(result["changes_next_payable"])

    def test_duplicate_recovered_upload_is_rejected(self):
        svc = ConsignmentService(load_fixture())
        upload = [{
            "id": "U-1", "piece": "P01", "site": "QIN", "seq": 1,
            "occurred": "2027-02-06T09:00:00+08:00",
            "reported": "2027-02-10T09:00:00+08:00"}]
        with self.assertRaises(ValueError):
            svc.apply_recovered_usage(upload,
                                      at("2027-02-11T09:00:00+08:00"))


if __name__ == "__main__":
    unittest.main()
