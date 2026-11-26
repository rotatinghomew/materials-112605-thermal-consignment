"""核对交换资料中的实体引用与原始回执顺序。"""
from datetime import datetime
import json
from pathlib import Path
import unittest

from partner import parse_time as time

ROOT = Path(__file__).resolve().parents[1]


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "fixtures" / "exchange.json").read_text(encoding="utf-8"))

    def test_record_identity(self):
        for name, collection in self.data.items():
            with self.subTest(collection=name):
                ids = [rec["id"] for rec in collection]
                self.assertTrue(ids)
                self.assertTrue(all(isinstance(i, str) and i for i in ids))
                self.assertEqual(len(ids), len(set(ids)))
                self.assertTrue(all(set(x) == set(collection[0]) for x in collection))

    def test_receipt_clocks(self):
        for rec in self.data["receipts"]:
            with self.subTest(receipt=rec["id"]):
                self.assertLessEqual(time(rec["happened"]), time(rec["arrived"]))
        with self.assertRaises(ValueError):
            time(datetime(2027, 1, 18).isoformat())

    def test_related_records(self):
        def row(collection, key):
            return next(rec for rec in self.data[collection] if rec["id"] == key)
        assert row("contracts","C-31")["mode"] != row("contracts","C-32")["mode"]
        assert row("movements","MOV-1")["piece"] == row("movements","MOV-2")["piece"] == row("usage","U-3")["piece"]
        assert time(row("movements","MOV-2")["happened"]) < time(row("usage","U-3")["occurred"])
        assert time(row("usage","U-3")["reported"]) < time(row("movements","MOV-2")["arrived"])
        assert row("returns","RT-2")["usage"] in row("receipts","PAY-1")["usages"]
        assert time(row("usage","U-3")["occurred"]).month != time(row("usage","U-3")["reported"]).month
