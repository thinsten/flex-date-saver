"""目的地无民航机场的接驳回归（2026-10-19 实测：接口把「本溪」静默匹配到沈阳桃仙机场）。
用 tests/fixtures/feeder 的真实录制回放：验证自动补机场↔目的地火车、到达时间按火车到站、
总价含火车票、接不上火车时退回纯航班并提示。全部离线，不联网。"""
import json
import os
import sys
import unittest
from pathlib import Path

os.environ["FDS_TODAY"] = "2026-10-19"
os.environ["FDS_RATE_FILE"] = "/tmp/fds-test-rate.json"
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "skill" / "flex-date-saver" / "scripts"))

from common import load_config, parse_dt  # noqa: E402
from fetch_quotes import fetch  # noqa: E402
from flyai_client import FlyaiClient  # noqa: E402
from plan_candidates import generate  # noqa: E402
from score_rank import rank  # noqa: E402
from validate_request import validate  # noqa: E402

FEEDER = ROOT / "tests" / "fixtures" / "feeder"
REQ = ROOT / "tests" / "requests" / "sample_benxi_feeder.json"


class TestNoAirportFeeder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        req = validate(json.loads(REQ.read_text()))["request"]
        cls.req = req
        cands = generate(req)[0]
        cls.quoter_client = FlyaiClient(FEEDER, replay=True)
        cls.quotes = fetch(req, cands, cls.quoter_client)
        cls.plan = rank(req, cls.quotes)

    def test_1_out_leg_has_feeder_train(self):
        """去程自动补「落地沈阳 → 火车进本溪」，到达时间按本溪站到站算。"""
        combos = [p for p in self.plan["plans"] if p["out"].get("train_after")]
        self.assertTrue(combos, "应至少有一个方案带接驳火车")
        for p in combos:
            t = p["out"]["train"]
            self.assertEqual(t["segments"][-1]["to_full"], "本溪站")
            self.assertGreater(parse_dt(p["dest_arrive"]), parse_dt(t["segments"][-1]["arr"]))
            self.assertIn("无民航机场", "".join(p["flags"]))

    def test_2_price_includes_feeder(self):
        """总价拆分里 out 段 = 机票 + 火车票。"""
        for p in self.plan["plans"]:
            if not p["out"].get("train_after"):
                continue
            air = p["out"]["flight"]["price"]
            rai = p["out"]["train"]["price"]
            self.assertAlmostEqual(p["price"]["out"], air + rai, places=2)

    def test_3_slots_use_real_arrival(self):
        """可玩时段按火车到站起算：红眼落地沈阳的 12-05 上午不算（火车 07:31 到、08:11 才安顿好）。"""
        p = next(x for x in self.plan["plans"] if x["out_date"] == "2026-12-04" and x["out"].get("train_after"))
        self.assertNotIn("上午", p["slots"].get("2026-12-05", []), "火车 07:31 到但加酒店通勤后 08:11，上午 8 点档不完整")
        self.assertIn("下午", p["slots"]["2026-12-05"])

    def test_4_return_without_connection_falls_back(self):
        """回程 06:50 航班赶不上本溪早火车 → 保留纯航班段并提示地面交通自行安排。"""
        for p in self.plan["plans"]:
            self.assertIsNone(p["ret"].get("train"))
            self.assertTrue(any("未衔接上" in f or "地面交通" in f for f in p["flags"]),
                            "回程应有未衔接提示")

    def test_5_hotel_keyed_by_train_station(self):
        """第一晚酒店按接驳火车站（本溪站）定位，不是按桃仙机场。"""
        for q in self.quotes["quotes"]:
            for key in q.get("hotels_by_arrival") or {}:
                self.assertNotIn("桃仙", key, "酒店位置基准不应是邻近城市的机场")

    def test_6_normal_destination_untouched(self):
        """有机场的目的地（三亚）不受影响：无接驳段、无「无民航机场」标签。"""
        req = validate(json.loads((ROOT / "tests" / "requests" / "sample_suzhou_sanya.json").read_text()))["request"]
        cands = generate(req)[0]
        quotes = fetch(req, cands, FlyaiClient(ROOT / "docs" / "demo-cache" / "flyai", replay=True))
        plan = rank(req, quotes)
        self.assertTrue(plan["plans"])
        for p in plan["plans"]:
            self.assertNotIn("无民航机场", "".join(p["flags"]))
            self.assertFalse(p["out"].get("train_after"))

    def test_7_hub_km_with_amap_key(self):
        """服务器场景：配了 AMAP_KEY 时公里数标签要出现且不能崩（2026-10-19 曾因把 place 的 dict 整个喂给
        loc_of 触发 ValueError: too many values to unpack，本机无 Key 走了 None 分支没测到）。"""
        import fetch_quotes as fq
        req = dict(self.req)
        q = fq.Quoter(req, FlyaiClient(FEEDER, replay=True), load_config("defaults"))

        class FakeAmap:
            ok = True
            def place(self, kw, city):
                return {"location": "123.45678,41.76543", "city": city, "district": "浑南区"}
        q.amap = FakeAmap()
        km = q._hub_km("桃仙国际机场", "沈阳", "本溪")
        self.assertIsInstance(km, int)
        q.amap.place = lambda kw, city: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertIsNone(q._hub_km("坏机场", "沈阳", "本溪"))  # 高德异常也不能炸主流程



if __name__ == "__main__":
    unittest.main()
