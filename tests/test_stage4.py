"""阶段 4 验收：可玩时段、特种兵酒店晚数、三种目标排序、多样性、两目的地、回放一致性。"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

os.environ["FDS_TODAY"] = "2026-10-15"
ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skill" / "flex-date-saver" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from common import load_config  # noqa: E402
from score_rank import count_slots, rank  # noqa: E402
from validate_request import validate  # noqa: E402

CFG = load_config("defaults")


def leg(no, dep, arr, price, dest_time_key, dest_time, flags=()):
    seg = {"no": no, "dep": dep, "arr": arr, "from": "A", "to": "B", "from_full": "A", "to_full": "B", "seat": "经济舱"}
    x = {"price": price, "flight": {"kind": "flight", "price": price, "url": f"https://x/{no}", "direct": True,
                                    "layovers_min": [], "segments": [seg]}, "train": None, "flags": list(flags)}
    x[dest_time_key] = dest_time
    return x


def quote(cid_date, nights, ret_date, outs, rets, hotel_price=300, dest="三亚", leave=0, est=False):
    return {"id": f"{dest}|{cid_date}|{nights}n|x|y", "dest": dest, "out_date": cid_date, "out_weekday": "周五",
            "ret_date": ret_date, "ret_weekday": "周一", "nights": nights,
            "out": {"mode": "after_work", "label": "下班后出发"}, "ret": {"mode": "day_off", "label": "请假返程"},
            "leave_dates": [], "leave_days": leave, "estimated_holiday": est, "status": "ok",
            "options": [{"origin": {"city": "上海", "kind": "home"}, "out": outs, "ret": rets}],
            "hotel": {"name": "H", "price_per_night": hotel_price, "url": "https://h"}, "hotel_alts": []}


def req(**over):
    raw = {"home_city": "上海", "destinations": ["三亚"], "window": {"start": "2026-11-16", "end": "2026-12-20"},
           "nights": {"min": 2, "max": 3}, "travelers": {"adults": 2}, "leave": {"allowed_dates": [], "max_days": 0}}
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(raw.get(k), dict):
            raw[k] = {**raw[k], **v}
        else:
            raw[k] = v
    r = validate(raw)
    return r["request"]


class TestSlots(unittest.TestCase):
    def test_1_arrive_11_and_leave_1730(self):
        s = count_slots(datetime(2026, 11, 21, 12, 0), datetime(2026, 11, 23, 17, 30), CFG)
        self.assertEqual(s["2026-11-21"], ["下午", "晚上"])
        self.assertEqual(s["2026-11-22"], ["上午", "下午", "晚上"])
        self.assertEqual(s["2026-11-23"], ["上午", "下午"])

    def test_2_1700_to_1800_gap(self):
        # 17:30 离开：下午(12-17) 完整算，晚上(18-24) 不算
        s = count_slots(datetime(2026, 11, 21, 8, 0), datetime(2026, 11, 21, 17, 30), CFG)
        self.assertEqual(s["2026-11-21"], ["上午", "下午"])


class TestRank(unittest.TestCase):
    def test_3_special_forces_no_hotel_night(self):
        q = quote("2026-11-20", 3, "2026-11-23",
                  [leg("R1", "2026-11-20 23:30", "2026-11-21 02:30", 500, "dest_arrive", "2026-11-21 02:30", ["红眼"])],
                  [leg("B1", "2026-11-23 20:00", "2026-11-23 23:00", 400, "dest_leave", "2026-11-23 18:00")])
        p = rank(req(goal={"special_forces": True}), {"quotes": [q]})["plans"][0]
        self.assertEqual(p["hotel_nights"], 2)
        self.assertEqual(p["price"]["total"], (500 + 400) * 2 + 300 * 2)
        self.assertTrue(any("不订酒店" in f for f in p["flags"]))
        p2 = rank(req(goal={"special_forces": False}), {"quotes": [q]})["plans"][0]
        self.assertEqual(p2["hotel_nights"], 3)

    def _three(self):
        # A：最便宜但玩得少；B：最贵玩得最多；C：中间
        a = quote("2026-11-20", 2, "2026-11-22",
                  [leg("A1", "2026-11-20 20:00", "2026-11-20 23:30", 600, "dest_arrive", "2026-11-20 23:30")],
                  [leg("A2", "2026-11-22 08:00", "2026-11-22 11:00", 300, "dest_leave", "2026-11-22 06:00")], 200)
        b = quote("2026-11-27", 3, "2026-11-30",
                  [leg("B1", "2026-11-27 07:00", "2026-11-27 10:00", 1200, "dest_arrive", "2026-11-27 10:00")],
                  [leg("B2", "2026-11-30 21:00", "2026-11-30 23:59", 900, "dest_leave", "2026-11-30 19:00")], 400, leave=1)
        c = quote("2026-12-04", 2, "2026-12-06",
                  [leg("C1", "2026-12-04 07:00", "2026-12-04 10:00", 700, "dest_arrive", "2026-12-04 10:00")],
                  [leg("C2", "2026-12-06 20:00", "2026-12-06 23:00", 500, "dest_leave", "2026-12-06 18:00")], 300)
        return {"quotes": [a, b, c]}

    def test_4_goal_orders(self):
        doc = self._three()
        r = req()
        save = [p["out_date"] for p in rank(r, doc, "save")["plans"]]
        play = [p["out_date"] for p in rank(r, doc, "play")["plans"]]
        bal = rank(r, doc, "balance")["plans"]
        self.assertEqual(save[0], "2026-11-20")
        self.assertEqual(play[0], "2026-11-27")
        ratios = [p["price"]["total"] / p["slot_count"] for p in bal]
        self.assertEqual(ratios, sorted(ratios))

    def test_5_distinct_dates_and_savings(self):
        doc = self._three()
        doc["quotes"].append(quote("2026-11-20", 3, "2026-11-23",
                                   [leg("D1", "2026-11-20 20:00", "2026-11-20 23:30", 610, "dest_arrive", "2026-11-20 23:30")],
                                   [leg("D2", "2026-11-23 08:00", "2026-11-23 11:00", 300, "dest_leave", "2026-11-23 06:00")], 200))
        plans = rank(req(), doc, "save")["plans"]
        dates = [p["out_date"] for p in plans]
        self.assertEqual(len(dates), len(set(dates)), "前 3 名出发日期须两两不同")
        self.assertGreater(plans[0]["save_vs_max"], 0)
        self.assertIn("reason_facts", plans[0])

    def test_6_two_destinations(self):
        doc = self._three()
        doc["quotes"][2]["dest"] = "厦门"
        doc["quotes"][2]["id"] = "厦门|2026-12-04|2n|x|y"
        out = rank(req(destinations=["三亚", "厦门"]), doc, "save")
        self.assertEqual(set(out["per_destination_best"]), {"三亚", "厦门"})
        self.assertEqual(out["per_destination_best"]["厦门"]["out_date"], "2026-12-04")

    def test_7_budget_filter_play(self):
        doc = self._three()
        plans = rank(req(goal={"budget_total": 3000}), doc, "play")["plans"]
        self.assertTrue(all(p["price"]["total"] <= 3000 for p in plans))


class TestReplayConsistency(unittest.TestCase):
    def test_8_three_replay_runs_identical(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(ROOT / "docs" / "demo-cache" / "flyai", tmp / "cache")
            digests = []
            for i in range(3):
                out = tmp / f"plan{i}.json"
                subprocess.run([sys.executable, str(SCRIPTS / "run_plan.py"), "--request",
                                str(ROOT / "tests" / "requests" / "sample_suzhou_sanya.json"), "--out", str(out),
                                "--workdir", str(tmp / f"wd{i}"), "--cache-dir", str(tmp / "cache"), "--replay"],
                               check=True, capture_output=True, env={**os.environ, "FDS_TODAY": "2026-10-15"})
                plan = json.loads(out.read_text())
                plan["meta"].pop("elapsed_s", None)
                digests.append(hashlib.sha1(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
                self.assertTrue(plan["plans"], "样例回放应至少有 1 个方案")
            self.assertEqual(len(set(digests)), 1, digests)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
