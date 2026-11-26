"""测试夹具辅助：加载与深拷贝改写。"""
import copy
import json
from pathlib import Path

from partner import parse_time
from consignment import ExchangeData

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "exchange.json"


def load_fixture() -> ExchangeData:
    return ExchangeData(json.loads(FIXTURE.read_text(encoding="utf-8")))


def raw_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def mutate(data: ExchangeData, collection: str, record_id: str, **changes):
    raw = copy.deepcopy(data.raw)
    for rec in raw[collection]:
        if rec["id"] == record_id:
            rec.update(changes)
            break
    return ExchangeData(raw)


def at(text: str):
    return parse_time(text)
