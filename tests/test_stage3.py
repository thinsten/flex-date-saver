"""阶段 3 验收：查价过滤、缓存、预算、错误处理。全部基于 tests/fixtures/real 与 docs/demo-cache/flyai 的真实录制，不联网。"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

os.environ["FDS_TODAY"] = "2026-10-15"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skill" / "flex-date-saver" / "scripts"))

from common import load_config, parse_dt  # noqa: E402
from fetch_quotes import Quoter, fetch, parse_journey  # noqa: E402
from flyai_client import FlyaiClient  # noqa: E402
from plan_candidates import generate  # noqa: E402
from validate_request import validate  # noqa: E402

REAL = ROOT / "tests" / "fixtures" / "real"
DEMO = ROOT / "docs" / "demo-cache" / "flyai"
SAMPLE = ROOT / "tests" / "requests" / "sample_suzhou_sanya.json"


def real(name):
    return json.loads((REAL / f"{name}.json").read_text())["stdout"]["data"]


class FakeClient:
    """按命令名返回固定的真实录制数据。"""
    def __init__(self, mapping):
        self.mapping, self.calls, self.stopped, self.live_calls, self.max_calls = mapping, [], None, 0, 99
        self.errors = []

    def call(self, args):
        self.calls.append(args)
        for match, data in self.mapping:
            if match(args):
                return data
        return None

    def summary(self):
        return {"live_calls": 0, "cache_hits": 0, "skipped": 0, "max_calls": 99, "stopped": None,
                "trial_mode": False, "replay": True}


def sample_req(**over):
    raw = json.loads(SAMPLE.read_text())
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k].update(v)
        else:
            raw[k] = v
    res = validate(raw)
    assert not res["missing"], res
    return res["request"]


class TestStage3(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(DEMO, self.tmp / "cache")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_1_second_run_all_cache(self):
        req = sample_req()
        cands = generate(req)[0]
        cfg = load_config("defaults")
        c1 = FlyaiClient(self.tmp / "cache", replay=True)
        r1 = fetch(req, cands, c1)
        self.assertEqual(c1.live_calls, 0)
        self.assertGreater(c1.cache_hits, 5)
        self.assertEqual([e for e in r1["errors"] if e["code"] == "replay_miss"], [], "演示缓存应覆盖样例的全部查询")
        ok = [q for q in r1["quotes"] if q["status"] == "ok"]
        self.assertEqual(len(ok), 1)
        self.assertEqual(ok[0]["id"], "三亚|2026-11-20|3n|day_off|day_off")
        self.assertTrue(cfg)

    def test_2_no_route_does_not_crash(self):
        req = sample_req()
        cands = generate(req)[0]
        r = fetch(req, cands, FlyaiClient(self.tmp / "cache", replay=True))
        self.assertTrue(any(w.startswith("苏州→三亚 在") for w in r["warnings"]))
        reasons = {q["id"]: q["status"] for q in r["quotes"]}
        self.assertEqual(reasons["三亚|2026-11-20|3n|after_work|day_off"], "no_out", "苏州下班后赶不上 22 点前航班")

    def test_3_transfer_filter(self):
        data = real("f_transfer")
        mapping = [(lambda a: a[0] == "search-flight", data)]
        q = Quoter(sample_req(transport={"transfer_ok": True, "max_layover_min": 180}), FakeClient(mapping), load_config("defaults"))
        fl = q._flights("上海", "三亚", "2026-11-20")
        self.assertTrue(all(all(l <= 180 for l in f["layovers"]) for f in fl))
        raw = [parse_journey(i, "flight") for i in data["itemList"]]
        self.assertTrue(any(max(j["layovers"] or [0]) > 180 for j in raw), "原始数据里确有超 3 小时中转")
        q2 = Quoter(sample_req(transport={"transfer_ok": False}), FakeClient(mapping), load_config("defaults"))
        self.assertTrue(all(f["direct"] for f in q2._flights("上海", "三亚", "2026-11-20")))

    def test_4_nearby_connection_rule(self):
        req = sample_req()
        cands = generate(req)[0]
        r = fetch(req, cands, FlyaiClient(self.tmp / "cache", replay=True))
        tr = load_config("transfer_estimates")["station_to_airport"]
        for q in r["quotes"]:
            for o in q["options"]:
                for leg in o["out"]:
                    t, f = leg["train"], leg["flight"]
                    if not t:
                        continue
                    st, ap = t["segments"][-1]["to"], f["segments"][0]["from"]
                    tf = next((v for k, v in tr.items() if k.split("|")[0] == st and k.split("|")[1] in ap), 60)
                    self.assertLessEqual(parse_dt(t["segments"][-1]["arr"]) + timedelta(minutes=tf + 90),
                                         parse_dt(f["segments"][0]["dep"]))
                    self.assertIn("火车余票需自行确认", leg["flags"])

    def test_5_hotel_area_and_star_filter(self):
        data = real("h_star5_3n")
        mapping = [(lambda a: a[0] == "search-hotel", data)]
        req = sample_req(hotel={"stars": [5], "area": "亚龙湾", "price_max": None})
        q = Quoter(req, FakeClient(mapping), load_config("defaults"))
        cand = {"dest": "三亚", "out_date": "2026-11-20", "ret_date": "2026-11-23"}
        hs = q.hotels(cand)
        # 真实数据里没有亚龙湾酒店（关键词不生效）→ 放宽到全市并给出提示
        self.assertTrue(hs)
        self.assertTrue(any("亚龙湾" in w for w in q.warnings))
        self.assertEqual([h["price_per_night"] for h in hs], sorted(h["price_per_night"] for h in hs))
        req2 = sample_req(hotel={"stars": [5], "area": "三亚湾", "price_max": None})
        hs2 = Quoter(req2, FakeClient(mapping), load_config("defaults")).hotels(cand)
        self.assertTrue(hs2 and all("三亚湾" in (h["name"] + (h["address"] or "") + (h["near"] or "")) for h in hs2))

    def test_6_chain_by_name(self):
        data = real("h_brand")
        mapping = [(lambda a: a[0] == "search-hotel", data)]
        req = sample_req(hotel={"stars": [], "chain": "brand:全季", "price_max": None})
        hs = Quoter(req, FakeClient(mapping), load_config("defaults")).hotels(
            {"dest": "三亚", "out_date": "2026-11-20", "ret_date": "2026-11-23"})
        self.assertTrue(hs and all("全季" in h["name"] for h in hs), "brandName 为 None 时靠名称识别全季")

    def test_7_budget_and_replay_miss(self):
        req = sample_req()
        cands = generate(req)[0]
        empty = self.tmp / "empty"
        c = FlyaiClient(empty, replay=True)
        r = fetch(req, cands, c)
        self.assertEqual(c.live_calls, 0)
        self.assertTrue(any(e["code"] == "replay_miss" for e in r["errors"]))
        self.assertTrue(all(q["status"] != "ok" for q in r["quotes"]))

    def test_8_fatal_error_stops(self):
        c = FlyaiClient(self.tmp / "x", replay=False, flyai_bin="/nonexistent/flyai")
        self.assertIsNone(c.call(["search-flight", "--origin", "上海"]))
        self.assertEqual(c.stopped, "cli_missing")
        self.assertIsNone(c.call(["search-flight", "--origin", "北京"]))
        self.assertEqual(c.skipped, 1)


if __name__ == "__main__":
    unittest.main()
