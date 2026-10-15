#!/usr/bin/env python3
"""② 节假日与请假计算，生成候选行程。

用法：python3 plan_candidates.py --request request.normalized.json --out candidates.json

规则（开发指南 2.2 / 2.4）：
- 去程日是工作日：可「下班后出发」（不请假，最早离家=下班时间），若该日在可请假日期内，也可「请假白天出发」（离家=07:00）
- 去程日非工作日：离家=07:00，不请假
- 中间的工作日：全部要请假，且必须都在可请假日期内
- 返程日是工作日：在可请假日期内才可「请假返程」；特种兵额外允许「早班直接上班」（到家≤上班时间，不请假）
- 返程日非工作日：到家≤最晚到家时间；特种兵放宽到次日 02:00
- 总请假天数 ≤ max_days
本脚本只算日期与时刻边界，不调用接口。具体车站的通勤、安检缓冲在 fetch_quotes 里叠加。
"""
import argparse
from datetime import timedelta

from common import HolidayCalendar, at, d, daterange, fmt_dt, read_json, weekday_cn, write_json


def _out_options(day, cal, req, allowed):
    w = req["work"]
    if cal.is_workday(day):
        opts = [{"mode": "after_work", "label": "下班后出发", "leave": [],
                 "leave_home_after": at(day, w["off_time"])}]
        if day in allowed:
            opts.append({"mode": "day_off", "label": "请假白天出发", "leave": [day],
                         "leave_home_after": at(day, req["day_off_leave_home"])})
        return opts
    return [{"mode": "free_day", "label": "假日出发", "leave": [],
             "leave_home_after": at(day, req["day_off_leave_home"])}]


def _ret_options(day, cal, req, allowed):
    sf = req["goal"]["special_forces"]
    late = at(day + timedelta(days=1), req["special_forces"]["late_return_until"])
    normal = at(day, req["return_home_by"])
    if cal.is_workday(day):
        opts = []
        if day in allowed:
            opts.append({"mode": "day_off", "label": "请假返程", "leave": [day],
                         "arrive_home_by": late if sf else normal})
        if sf:
            opts.append({"mode": "straight_to_work", "label": "早班直接上班", "leave": [],
                         "arrive_home_by": at(day, req["work"]["start_time"])})
        return opts
    return [{"mode": "free_day", "label": "假日返程", "leave": [],
             "arrive_home_by": late if sf else normal}]


def generate(req, cal=None):
    """返回 (candidates, stats)。candidates 中时间均为 'YYYY-MM-DD HH:MM' 字符串。"""
    cal = cal or HolidayCalendar()
    win_s, win_e = d(req["window"]["start"]), d(req["window"]["end"])
    n_min, n_max = req["nights"]["min"], req["nights"]["max"]
    allowed = {d(x) for x in req["leave"]["allowed_dates"]}
    max_days = req["leave"]["max_days"]
    origins = [{"city": req["home_city"], "kind": "home"}]
    if req["transport"]["nearby_origin_ok"]:
        origins += [{"city": c, "kind": "nearby"} for c in req["transport"]["nearby_cities"]]

    cands, stats = [], {"enumerated": 0, "rejected_mid_leave": 0, "rejected_ret": 0, "rejected_leave_cap": 0}
    for dest in req["destinations"]:
        for out_day in daterange(win_s, win_e):
            for n in range(n_min, n_max + 1):
                ret_day = out_day + timedelta(days=n)
                if ret_day > win_e:
                    continue
                stats["enumerated"] += 1
                mid = [x for x in daterange(out_day + timedelta(days=1), ret_day - timedelta(days=1)) if cal.is_workday(x)]
                if any(x not in allowed for x in mid):
                    stats["rejected_mid_leave"] += 1
                    continue
                rets = _ret_options(ret_day, cal, req, allowed)
                if not rets:
                    stats["rejected_ret"] += 1
                    continue
                span = list(daterange(out_day, ret_day))
                estimated = any(cal.is_estimated(x) for x in span)
                uncovered = any(not cal.covered(x) for x in span)
                for o in _out_options(out_day, cal, req, allowed):
                    for r in rets:
                        leave = sorted(set(o["leave"] + mid + r["leave"]))
                        if len(leave) > max_days:
                            stats["rejected_leave_cap"] += 1
                            continue
                        cands.append({
                            "id": f"{dest}|{out_day}|{n}n|{o['mode']}|{r['mode']}",
                            "dest": dest,
                            "out_date": str(out_day), "out_weekday": weekday_cn(out_day),
                            "ret_date": str(ret_day), "ret_weekday": weekday_cn(ret_day),
                            "nights": n,
                            "out": {"mode": o["mode"], "label": o["label"], "leave_home_after": fmt_dt(o["leave_home_after"])},
                            "ret": {"mode": r["mode"], "label": r["label"], "arrive_home_by": fmt_dt(r["arrive_home_by"])},
                            "leave_dates": [str(x) for x in leave],
                            "leave_days": len(leave),
                            "day_labels": {str(x): cal.label(x) for x in span},
                            "estimated_holiday": estimated,
                            "outside_holiday_coverage": uncovered,
                            "origins": origins,
                        })
    cands.sort(key=lambda c: (c["dest"], c["out_date"], c["nights"], c["leave_days"], c["out"]["mode"], c["ret"]["mode"]))
    stats["candidates"] = len(cands)
    return cands, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    req = read_json(a.request)
    cands, stats = generate(req)
    write_json(a.out, {"stats": stats, "candidates": cands})
    print(f"候选行程 {stats['candidates']} 条（枚举 {stats['enumerated']}，"
          f"中间日无法请假剔除 {stats['rejected_mid_leave']}，返程日无法安排剔除 {stats['rejected_ret']}，"
          f"超请假上限剔除 {stats['rejected_leave_cap']}）")


if __name__ == "__main__":
    main()
