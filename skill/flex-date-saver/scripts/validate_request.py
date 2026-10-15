#!/usr/bin/env python3
"""① 校验规划需求单：补默认值、检查缺项与冲突、预检是否存在可行日期组合。

用法：python3 validate_request.py --request request.json --out request.normalized.json
输入可以是纯 JSON，也可以是表单复制出来的「【规划需求单 v1】+ json 代码块」文本。
输出：{"ok", "request", "missing", "conflicts", "warnings", "applied", "candidate_count"}
- missing / conflicts 非空时 ok=false，智能体须一次性向用户追问（最多一轮）
- warnings 不阻断，复述确认时要告知用户
"""
import argparse
import copy
import json
import math
import re
import sys
from datetime import timedelta
from pathlib import Path

from common import HolidayCalendar, d, daterange, load_config, today, write_json

HM = re.compile(r"^([01]\d|2[0-4]):[0-5]\d$")


def parse_request_text(text):
    """兼容表单复制文本：取第一个 {...} JSON 块。"""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    elif not text.startswith("{"):
        i, j = text.find("{"), text.rfind("}")
        if i < 0 or j < 0:
            raise ValueError("没有找到 JSON 内容")
        text = text[i:j + 1]
    return json.loads(text)


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        elif v is not None or k not in out:
            out[k] = v
    return out


def validate(raw, cal=None):
    cal = cal or HolidayCalendar.for_request(raw)
    dft = load_config("defaults")
    lim = dft["limits"]
    missing, conflicts, warnings, applied = [], [], [], []

    def conflict(code, msg, suggestions=None):
        conflicts.append({"code": code, "message": msg, "suggestions": suggestions or []})

    def warn(code, msg):
        warnings.append({"code": code, "message": msg})

    req = {
        "version": "1",
        "work": dft["work"], "return_home_by": dft["return_home_by"],
        "day_off_leave_home": dft["day_off_leave_home"],
        "transport": dft["transport"], "hotel": dft["hotel"], "goal": dft["goal"],
        "special_forces": dft["special_forces"],
        "leave": {"allowed_dates": [], "max_days": 0},
        "travelers": {"adults": None, "children": 0},
        "stations": [],
    }
    req = deep_merge(req, raw)

    # ---------- 必填 ----------
    if not (req.get("home_city") or "").strip():
        missing.append({"field": "home_city", "message": "常住城市（A1）"})
    dests = [x.strip() for x in (req.get("destinations") or []) if isinstance(x, str) and x.strip()]
    dests = list(dict.fromkeys(dests))
    req["destinations"] = dests
    if not dests:
        missing.append({"field": "destinations", "message": "目的地（A2，最多 2 个）"})
    win = req.get("window") or {}
    if not (win.get("start") and win.get("end")):
        missing.append({"field": "window", "message": "最早出发日期和最晚到达日期（A3）"})
    nights = req.get("nights")
    if isinstance(nights, int):
        req["nights"] = nights = {"min": nights, "max": nights}
    if isinstance(nights, list):
        nights = {"options": nights}
    if isinstance(nights, dict) and nights.get("options"):
        opts = sorted({int(x) for x in nights["options"] if str(x).isdigit() and int(x) >= 1})
        req["nights"] = nights = {"min": opts[0], "max": opts[-1], "options": opts} if opts else {}
    if not (isinstance(nights, dict) and nights.get("min") and nights.get("max")):
        missing.append({"field": "nights", "message": "玩几晚（A4，可多选）"})
    adults = (req.get("travelers") or {}).get("adults")
    if not adults:
        missing.append({"field": "travelers.adults", "message": "成人人数（A5）"})
    if req["hotel"].get("mode") == "specific" and not req["hotel"].get("specific_name"):
        missing.append({"field": "hotel.specific_name", "message": "指定酒店的名称（D5）"})

    # ---------- 规范化与冲突 ----------
    home = (req.get("home_city") or "").strip()
    req["home_city"] = home
    if len(dests) > lim["max_destinations"]:
        conflict("too_many_destinations", f"目的地最多 {lim['max_destinations']} 个，你填了 {len(dests)} 个。",
                 [f"保留其中 {lim['max_destinations']} 个"])
    if home and home in dests:
        conflict("dest_is_home", f"目的地「{home}」和常住城市相同。", ["换一个目的地"])

    win_ok = False
    if win.get("start") and win.get("end"):
        try:
            ws, we = d(win["start"]), d(win["end"])
            win_ok = True
        except ValueError:
            conflict("bad_window", "时间窗口日期格式不对，应为 YYYY-MM-DD。")
        if win_ok:
            if we < ws:
                conflict("window_reversed", "最晚到达日期早于最早出发日期。", ["对调两个日期"])
                win_ok = False
            elif ws <= today():
                conflict("window_in_past", f"最早出发日期需在明天（{today() + timedelta(days=1)}）或之后。",
                         ["把最早出发日期改到明天之后"])
                win_ok = False
            elif (we - ws).days + 1 > lim["max_window_days"]:
                conflict("window_too_long", f"出发到到达的日期范围最长 {lim['max_window_days']} 天，当前 {(we - ws).days + 1} 天。",
                         ["缩短到 2 个月以内", "拆成两次分别规划"])
                win_ok = False

    if isinstance(nights, dict) and nights.get("min") and nights.get("max"):
        if nights["min"] < 1 or nights["max"] < 1:
            conflict("bad_nights", "晚数至少为 1。")
        elif nights["min"] > nights["max"]:
            conflict("nights_reversed", f"晚数下限 {nights['min']} 大于上限 {nights['max']}。",
                     [f"改为 {nights['max']}~{nights['min']} 晚"])
        elif nights["max"] > lim["max_nights"]:
            conflict("too_many_nights", f"最多支持玩 {lim['max_nights']} 晚。")
        elif win_ok and nights["min"] > (we - ws).days:
            conflict("nights_exceed_window", f"日期范围只有 {(we - ws).days + 1} 天，放不下 {nights['min']} 晚。",
                     ["扩大时间窗口", "减少晚数"])

    if adults is not None and (not isinstance(adults, int) or adults < 1):
        conflict("bad_adults", "成人人数至少为 1。")
    children = req["travelers"].get("children") or 0
    req["travelers"]["children"] = children
    if children:
        warn("children_price", "儿童票价暂按成人价估算，实际以预订页为准。")
    if not req.get("rooms"):
        req["rooms"] = max(1, math.ceil((adults or 1) / 2))
        applied.append(f"房间数按 2 人一间补为 {req['rooms']} 间")

    for path, val in [("work.off_time", req["work"]["off_time"]), ("work.start_time", req["work"]["start_time"]),
                      ("return_home_by", req["return_home_by"])]:
        if not HM.match(str(val)):
            conflict("bad_time", f"{path} 时间格式应为 HH:MM，收到「{val}」。")

    # 车站与通勤
    stations = []
    for s in req.get("stations") or []:
        if not s.get("name"):
            continue
        t = s.get("type") if s.get("type") in ("air", "train") else ("air" if "机场" in s["name"] else "train")
        cm = s.get("commute_min")
        if cm is None:
            cm = dft["default_commute_min"][t]
            warn("commute_default", f"{s['name']} 未填通勤时间，按 {cm} 分钟估算。")
        st = {"name": s["name"], "type": t, "commute_min": int(cm)}
        if s.get("city"):
            st["city"] = re.sub(r"(市|地区|自治州|盟)$", "", s["city"].strip())
        if s.get("dist_km") is not None:
            st["dist_km"] = s["dist_km"]
        stations.append(st)
    stations.sort(key=lambda x: (x["type"], x["commute_min"]))  # 同类型按通勤从短到长（邻近城市火车取最快的那个站）
    req["stations"] = stations
    req["home_city"] = re.sub(r"(市|地区|自治州|盟)$", "", req["home_city"]) if req["home_city"] else req["home_city"]
    if home and not any(s["type"] == "air" for s in stations):
        warn("no_airport_commute", f"未填到{home}机场的通勤时间，按 {dft['default_commute_min']['air']} 分钟估算。")

    # 交通
    tr = req["transport"]
    if isinstance(tr.get("train_seat"), str):
        tr["train_seat"] = [tr["train_seat"]]
    tr["nearby_cities"] = [c for c in dict.fromkeys(tr.get("nearby_cities") or []) if c and c != home and c not in dests]
    if tr.get("nearby_origin_ok") and not tr["nearby_cities"]:
        tr["nearby_origin_ok"] = False
        warn("nearby_empty", "勾选了「可从附近城市出发」但没选城市，本次只从常住城市出发。")
    if tr.get("max_layover_min", 0) < 0 or tr.get("max_train_min", 0) < 0:
        conflict("bad_transport", "中转等待或火车时长上限不能为负数。")
    if req["goal"].get("special_forces"):
        if not (tr["redeye_out"] and tr["redeye_back"]):
            applied.append("特种兵模式：已放开去程、回程红眼航班")
        tr["redeye_out"] = tr["redeye_back"] = True

    # 酒店
    h = req["hotel"]
    if h.get("price_min") is not None and h.get("price_max") is not None and h["price_min"] > h["price_max"]:
        conflict("hotel_price_reversed", f"酒店价格下限 {h['price_min']} 高于上限 {h['price_max']}。",
                 ["对调上下限", "去掉下限"])
    if any(s not in (1, 2, 3, 4, 5) for s in h.get("stars") or []):
        conflict("bad_stars", "星级只能是 1~5。")

    g = req["goal"]
    if g.get("priority") not in ("save", "play", "balance"):
        conflict("bad_goal", "目标只能是省钱优先 / 多玩优先 / 兼顾。")
    if g.get("budget_total") is not None and g["budget_total"] <= 0:
        conflict("bad_budget", "总预算需大于 0，或不设上限。")

    # 自定义上班日（单休/调休）
    cw = []
    for x in req.get("custom_workdays") or []:
        try:
            cw.append(str(d(x)))
        except (ValueError, TypeError):
            pass
    req["custom_workdays"] = sorted(set(cw))

    # 请假
    lv = req["leave"]
    if lv.get("max_days") is None or lv["max_days"] < 0:
        lv["max_days"] = 0
    allowed = sorted({str(d(x)) for x in lv.get("allowed_dates") or []})
    if win_ok:
        outside = [x for x in allowed if not (ws <= d(x) <= we)]
        if outside:
            warn("leave_outside_window", f"可请假日期 {', '.join(outside)} 不在出行日期范围内，已忽略。")
        non_work = [x for x in allowed if (ws <= d(x) <= we) and not cal.is_workday(x)]
        if non_work:
            warn("leave_on_holiday", f"{', '.join(non_work)} 本来就不用上班，无需请假，已忽略。")
        kept = [x for x in allowed if x not in outside and x not in non_work]
        if lv["max_days"] > 0 and allowed and not kept:
            conflict("leave_all_invalid", "勾选的可请假日期都不在出行日期范围内（或都是休息日），等于不能请假。",
                     ["在日期范围内重新勾选可请假的工作日", "把最多请假天数改为 0，只用周末和假期"])
        allowed = kept
        if lv["max_days"] > len(allowed):
            if allowed or lv["max_days"]:
                applied.append(f"可请假日期只有 {len(allowed)} 天，请假上限按 {len(allowed)} 天计算")
            lv["max_days"] = len(allowed)
        if any(cal.is_estimated(x) for x in daterange(ws, we)):
            warn("estimated_holiday", cal.cfg.get("note", "部分假期为预估。"))
        if any(not cal.covered(x) for x in daterange(ws, we)):
            warn("holiday_coverage", f"节假日表只覆盖到 {cal.cov_end}，之后的日期按普通周末/工作日计算。")
    lv["allowed_dates"] = allowed

    result = {"ok": False, "request": req, "missing": missing, "conflicts": conflicts,
              "warnings": warnings, "applied": applied, "candidate_count": 0}

    # ---------- 可行性预检 ----------
    if not missing and not conflicts:
        from plan_candidates import generate
        cands, stats = generate(req, cal)
        result["candidate_count"] = len(cands)
        if not cands:
            conflict("no_feasible_dates",
                     "在你的请假约束下，窗口内找不到能安排的出发/返程日期组合。",
                     ["多勾选几个可请假的工作日或提高请假上限", "缩短晚数", "扩大出行日期范围以包含更多周末或假期",
                      "接受特种兵（允许早班直接上班）"])
    result["ok"] = not missing and not conflicts
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        raw = parse_request_text(Path(a.request).read_text(encoding="utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "conflicts": [{"code": "bad_json", "message": f"需求单无法解析：{e}"}]},
                         ensure_ascii=False))
        sys.exit(2)
    res = validate(raw)
    write_json(a.out, res)
    print(json.dumps({k: res[k] for k in ("ok", "missing", "conflicts", "warnings", "applied", "candidate_count")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
