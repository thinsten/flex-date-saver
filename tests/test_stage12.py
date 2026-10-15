"""阶段 1-2 验收：需求单校验、节假日与候选行程。运行：python3 -m unittest discover -s tests -v"""
import os
import sys
import unittest
from pathlib import Path

os.environ["FDS_TODAY"] = "2026-10-15"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skill" / "flex-date-saver" / "scripts"))

from plan_candidates import generate  # noqa: E402
from validate_request import parse_request_text, validate  # noqa: E402

BASE = {
    "home_city": "苏州", "destinations": ["三亚"],
    "window": {"start": "2026-11-16", "end": "2026-11-30"},
    "nights": {"min": 2, "max": 2}, "travelers": {"adults": 2},
}


def req(**kw):
    r = {k: (dict(v) if isinstance(v, dict) else v) for k, v in BASE.items()}
    for k, v in kw.items():
        r[k] = v
    return r


def norm(allow_infeasible=False, **kw):
    res = validate(req(**kw))
    codes = [c["code"] for c in res["conflicts"]]
    if allow_infeasible:
        assert not res["missing"] and set(codes) <= {"no_feasible_dates"}, (res["missing"], res["conflicts"])
    else:
        assert res["ok"], (res["missing"], res["conflicts"])
    return res["request"]


def by_id(cands):
    return {c["id"]: c for c in cands}


class TestValidate(unittest.TestCase):
    def test_1_minimal_defaults(self):
        res = validate(req())
        self.assertTrue(res["ok"], res)
        r = res["request"]
        self.assertEqual(r["work"], {"off_time": "18:00", "start_time": "09:00"})
        self.assertEqual(r["return_home_by"], "23:00")
        self.assertEqual(r["goal"]["priority"], "balance")
        self.assertFalse(r["transport"]["redeye_out"])
        self.assertEqual(r["transport"]["max_layover_min"], 180)
        self.assertEqual(r["hotel"]["stars"], [4])
        self.assertEqual(r["rooms"], 1)
        self.assertGreater(res["candidate_count"], 0)

    def test_2_three_destinations(self):
        res = validate(req(destinations=["三亚", "厦门", "昆明"]))
        self.assertIn("too_many_destinations", [c["code"] for c in res["conflicts"]])

    def test_3_nights_reversed(self):
        res = validate(req(nights={"min": 5, "max": 3}))
        self.assertIn("nights_reversed", [c["code"] for c in res["conflicts"]])

    def test_4_leave_all_outside_window(self):
        res = validate(req(leave={"allowed_dates": ["2026-12-04", "2026-12-07"], "max_days": 2}))
        self.assertIn("leave_all_invalid", [c["code"] for c in res["conflicts"]])

    def test_5_special_forces_opens_redeye(self):
        r = norm(goal={"special_forces": True})
        self.assertTrue(r["transport"]["redeye_out"] and r["transport"]["redeye_back"])

    def test_6_missing_fields(self):
        res = validate({"home_city": "苏州"})
        fields = {m["field"] for m in res["missing"]}
        self.assertEqual(fields, {"destinations", "window", "nights", "travelers.adults"})
        self.assertFalse(res["ok"])

    def test_7_window_in_past(self):
        res = validate(req(window={"start": "2026-10-10", "end": "2026-10-20"}))
        self.assertIn("window_in_past", [c["code"] for c in res["conflicts"]])

    def test_8_parse_form_text(self):
        text = '【规划需求单 v1】\n```json\n{"home_city":"苏州","destinations":["三亚"]}\n```'
        self.assertEqual(parse_request_text(text)["home_city"], "苏州")

    def test_9_leave_on_weekend_ignored(self):
        res = validate(req(leave={"allowed_dates": ["2026-11-21", "2026-11-23"], "max_days": 2}))
        self.assertEqual(res["request"]["leave"]["allowed_dates"], ["2026-11-23"])
        self.assertEqual(res["request"]["leave"]["max_days"], 1)
        self.assertIn("leave_on_holiday", [w["code"] for w in res["warnings"]])


class TestCandidates(unittest.TestCase):
    def test_1_friday_after_work_no_leave(self):
        # 周五 11-20 出发、周一 11-23 返回（周一可请假）
        r = norm(nights={"min": 3, "max": 3}, leave={"allowed_dates": ["2026-11-23"], "max_days": 1})
        c = by_id(generate(r)[0])["三亚|2026-11-20|3n|after_work|day_off"]
        self.assertEqual(c["out"]["leave_home_after"], "2026-11-20 18:00")
        self.assertEqual(c["leave_dates"], ["2026-11-23"])
        self.assertEqual(c["ret"]["arrive_home_by"], "2026-11-23 23:00")

    def test_2_monday_return_needs_allowed(self):
        r = norm(allow_infeasible=True, nights={"min": 3, "max": 3}, leave={"allowed_dates": [], "max_days": 0})
        ids = by_id(generate(r)[0])
        self.assertFalse(any(k.startswith("三亚|2026-11-20|3n") for k in ids), "周一不可请假时不应出现周五→周一")

    def test_3_special_forces_straight_to_work(self):
        r = norm(nights={"min": 3, "max": 3}, leave={"allowed_dates": [], "max_days": 0}, goal={"special_forces": True})
        c = by_id(generate(r)[0])["三亚|2026-11-20|3n|after_work|straight_to_work"]
        self.assertEqual(c["leave_days"], 0)
        self.assertEqual(c["ret"]["arrive_home_by"], "2026-11-23 09:00")

    def test_4_spring_festival_estimated(self):
        r = norm(window={"start": "2027-02-03", "end": "2027-02-14"}, nights={"min": 4, "max": 4})
        cands = generate(r)[0]
        c = by_id(cands)["三亚|2027-02-05|4n|free_day|free_day"]
        self.assertEqual(c["leave_days"], 0)
        self.assertTrue(c["estimated_holiday"])
        self.assertEqual(c["day_labels"]["2027-02-05"], "春节(预估)")

    def test_5_zero_leave_only_weekends(self):
        r = norm(leave={"allowed_dates": [], "max_days": 0}, nights={"min": 1, "max": 2})
        cands = generate(r)[0]
        self.assertTrue(cands)
        self.assertTrue(all(c["leave_days"] == 0 for c in cands))
        # 周五晚出发周日回、周六出发周日回都应存在
        ids = by_id(cands)
        self.assertIn("三亚|2026-11-20|2n|after_work|free_day", ids)
        self.assertIn("三亚|2026-11-21|1n|free_day|free_day", ids)

    def test_6_mid_workday_must_be_allowed(self):
        # 周四出发、周一返回：周五是中间工作日
        r = norm(allow_infeasible=True, nights={"min": 4, "max": 4}, leave={"allowed_dates": ["2026-11-23"], "max_days": 2})
        self.assertNotIn("三亚|2026-11-19|4n|after_work|day_off", by_id(generate(r)[0]))
        r = norm(nights={"min": 4, "max": 4},
                 leave={"allowed_dates": ["2026-11-20", "2026-11-23"], "max_days": 2})
        c = by_id(generate(r)[0])["三亚|2026-11-19|4n|after_work|day_off"]
        self.assertEqual(c["leave_dates"], ["2026-11-20", "2026-11-23"])

    def test_7_leave_cap(self):
        r = norm(nights={"min": 4, "max": 4},
                 leave={"allowed_dates": ["2026-11-19", "2026-11-20", "2026-11-23"], "max_days": 2})
        ids = by_id(generate(r)[0])
        self.assertIn("三亚|2026-11-19|4n|after_work|day_off", ids)
        self.assertNotIn("三亚|2026-11-19|4n|day_off|day_off", ids, "请 3 天超过上限 2")

    def test_8_nearby_origins(self):
        r = norm(transport={"nearby_origin_ok": True, "nearby_cities": ["上海", "苏州"]})
        c = generate(r)[0][0]
        self.assertEqual([o["city"] for o in c["origins"]], ["苏州", "上海"])

    def test_10_three_nights_zero_leave_infeasible(self):
        # 只有周末可用时，3 晚必然要占一个工作日 → 应提示无可行组合
        res = validate(req(nights={"min": 3, "max": 3}))
        self.assertIn("no_feasible_dates", [c["code"] for c in res["conflicts"]])

    def test_9_no_feasible(self):
        res = validate(req(window={"start": "2026-11-23", "end": "2026-11-27"}, nights={"min": 2, "max": 2}))
        self.assertIn("no_feasible_dates", [c["code"] for c in res["conflicts"]])


if __name__ == "__main__":
    unittest.main()
