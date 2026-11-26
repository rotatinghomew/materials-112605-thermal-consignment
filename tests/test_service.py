"""端到端：履约查询服务与对接方回执回放通过回环地址联调。"""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from partner import handler_for
from service import make_handler
from tests.helpers import load_fixture, raw_fixture


def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class ServiceEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_fixture()
        cls.server, _ = _serve(make_handler(cls.data))
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        raw = raw_fixture()
        cls.partner, _ = _serve(handler_for(raw["receipts"], False, None, False))
        cls.partner_base = f"http://127.0.0.1:{cls.partner.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.partner.shutdown()

    def test_health(self):
        status, payload = _get(f"{self.base}/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")

    def test_warehouse_endpoint_excludes_suspended_from_issue(self):
        _, payload = _get(f"{self.base}/v1/warehouse")
        rows = {r["piece"]: r for r in payload["pieces"]}
        self.assertTrue(rows["P05"]["available_to_issue"])
        self.assertFalse(rows["P07"]["available_to_issue"])

    def test_procurement_endpoint_ties_out_piece_amounts(self):
        _, payload = _get(f"{self.base}/v1/procurement")
        p02 = next(r for r in payload["pieces"] if r["piece"] == "P02")
        self.assertEqual(p02["billed_amount"], 7600)
        self.assertEqual(p02["credit_amount"], 7600)

    def test_scope_endpoint_reports_differences_and_arrears(self):
        _, payload = _get(f"{self.base}/v1/scope")
        self.assertEqual(payload["next_payable_delta"],
                         {"SU-1": 0, "SU-2": 8300})
        _, invoices = _get(f"{self.base}/v1/invoices")
        self.assertEqual(invoices["arrears"], {"SU-1": 22800})

    def test_claims_endpoint_lists_evidence(self):
        _, payload = _get(f"{self.base}/v1/claims")
        self.assertEqual(payload["claims"][0]["id"], "CLM-U-5")

    def test_cutoff_query_blocks_unseen_invoice_usages(self):
        _, payload = _get(
            f"{self.base}/v1/scope?at=2027-02-03T12%3A00%3A00%2B08%3A00")
        self.assertEqual({b["ref_id"] for b in payload["blocked"]},
                         {"U-1", "U-2", "U-4"})
        self.assertEqual(payload["next_payable_delta"], {})

    def test_bad_time_is_rejected(self):
        status, payload = _get(f"{self.base}/v1/scope?at=not-a-time")
        self.assertEqual(status, 400)
        self.assertIn("at", payload["message"])

    def test_recovered_usage_post_states_next_payable_change(self):
        payload = {
            "at": "2027-02-11T09:00:00+08:00",
            "usages": [{
                "id": "U-9", "piece": "P05", "site": "SHU", "seq": 9,
                "occurred": "2027-02-06T09:00:00+08:00",
                "reported": "2027-02-10T09:00:00+08:00"}]}
        status, body = _post(f"{self.base}/v1/recovered-usage", payload)
        self.assertEqual(status, 202)
        self.assertTrue(body["changes_next_payable"])

    def test_partner_receipt_playback_visibility_matches_arrival(self):
        # 对接方只按 arrived 提供回执：2/3 中午只能看到 PAY-2
        _, before = _get(
            f"{self.partner_base}/v1/receipts?at=2027-02-03T12%3A00%3A00%2B08%3A00")
        self.assertEqual({r["id"] for r in before["receipts"]}, {"PAY-2"})
        _, after = _get(
            f"{self.partner_base}/v1/receipts?at=2027-02-05T00%3A00%3A00%2B08%3A00")
        self.assertEqual({r["id"] for r in after["receipts"]},
                         {"PAY-1", "PAY-2"})


if __name__ == "__main__":
    unittest.main()
