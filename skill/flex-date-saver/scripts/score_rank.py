#!/usr/bin/env python3
"""④ 计算总价、可玩时段、风险标签并排序，输出 plan.json。

用法：python3 score_rank.py --request request.normalized.json --quotes quotes.json --out plan.json [--goal save|play|balance]
只改目标时直接重跑本脚本即可，不会重新查价。

规则（开发指南 2.1 / 2.4 / 2.6）：
- 可玩时段：上午 08-12、下午 12-17、晚上 18-24，完整落在 [落地+60分钟, 返程起飞-120分钟] 内才计 1 个
- 特种兵：凌晨 2 点后落地的那一晚不订酒店
- 总价 =（去程+回程，含邻近城市火车）× 出行人数 + 酒店每晚起价 × 晚数 × 房间数
- 排序：save 总价↑；play 时段↓；balance 总价÷时段↑；前 3 名出发日期两两不同
"""
import argparse
from datetime import timedelta

from common import at, d, daterange, fmt_dt, load_config, parse_dt, read_json, today, write_json, weekday_cn

GOAL_CN = {"save": "省钱优先", "play": "多玩优先", "balance": "兼顾"}


def count_slots(dest_arrive, dest_leave, cfg):
    arrive_ok = dest_arrive
    slots = {}
    for day in daterange(arrive_ok.date(), dest_leave.date()):
        got = [name for name, (s, e) in cfg["slots"].items() if at(day, s) >= arrive_ok and at(day, e) <= dest_leave]
        if got:
            slots[str(day)] = got
    return slots


def evaluate(q, req, cfg):
    """对一个有报价的候选，枚举 出发地×去程×回程 组合，返回全部组合的评估结果。"""
    persons = req["travelers"]["adults"] + (req["travelers"].get("children") or 0)
    rooms = req["rooms"]
    sf = req["goal"]["special_forces"]
    hotel = q["hotel"]
    combos = []
    for opt in q["options"]:
        for o in opt["out"]:
            for r in opt["ret"]:
                fl_arr = parse_dt(o["flight"]["segments"][-1]["arr"])
                arrive = fl_arr + timedelta(minutes=cfg["arrive_to_hotel_min"]["air"])
                leave = parse_dt(r["dest_leave"])
                if leave <= arrive:
                    continue
                slots = count_slots(arrive, leave, cfg)
                n_slots = sum(len(v) for v in slots.values())
                hotel_nights = q["nights"]
                no_hotel = None
                late = cfg["special_forces"]["no_hotel_if_arrive_after"]
                if sf and fl_arr.date() > d(q["out_date"]) and fl_arr >= at(fl_arr.date(), late):
                    hotel_nights -= 1
                    no_hotel = q["out_date"]
                transport = round(o["price"] + r["price"], 2)
                hotel_cost = round(hotel["price_per_night"] * hotel_nights * rooms, 2)
                total = round(transport * persons + hotel_cost, 2)
                flags = list(dict.fromkeys(["去程" + f for f in o["flags"]] + ["回程" + f for f in r["flags"]]))
                flags.append("酒店为参考起价")
                if q.get("estimated_holiday"):
                    flags.append("跨预估假期")
                if req["travelers"].get("children"):
                    flags.append("儿童按成人价估算")
                if no_hotel:
                    flags.append(f"{no_hotel} 当晚凌晨落地不订酒店")
                combos.append({
                    "candidate_id": q["id"], "dest": q["dest"], "origin": opt["origin"],
                    "out_date": q["out_date"], "out_weekday": q["out_weekday"],
                    "ret_date": q["ret_date"], "ret_weekday": q["ret_weekday"],
                    "nights": q["nights"], "hotel_nights": hotel_nights,
                    "out_mode": q["out"]["label"], "ret_mode": q["ret"]["label"],
                    "leave_dates": q["leave_dates"], "leave_days": q["leave_days"],
                    "slots": slots, "slot_count": n_slots,
                    "dest_arrive": fmt_dt(arrive), "dest_leave": fmt_dt(leave),
                    "price": {"out": o["price"], "ret": r["price"], "transport_per_person": transport,
                              "persons": persons, "transport_total": round(transport * persons, 2),
                              "hotel_per_night": hotel["price_per_night"], "hotel_nights": hotel_nights,
                              "rooms": rooms, "hotel_total": hotel_cost, "total": total},
                    "out": o, "ret": r, "hotel": hotel, "hotel_alts": q.get("hotel_alts") or [],
                    "flags": flags,
                    "estimated_holiday": q.get("estimated_holiday", False),
                })
    return combos


def sort_key(goal):
    if goal == "save":
        return lambda c: (c["price"]["total"], -c["slot_count"], c["leave_days"], c["candidate_id"])
    if goal == "play":
        return lambda c: (-c["slot_count"], c["price"]["total"], c["leave_days"], c["candidate_id"])
    return lambda c: (c["price"]["total"] / c["slot_count"] if c["slot_count"] else float("inf"),
                      c["leave_days"], c["price"]["total"], c["candidate_id"])


def _leg_brief(leg):
    f = leg["flight"]["segments"]
    s = f"{f[0]['no']} {f[0]['dep'][5:]} {f[0]['from']}→{f[-1]['to']} {f[-1]['arr'][11:]}"
    if len(f) > 1:
        s = " / ".join(f"{x['no']} {x['dep'][5:]} {x['from']}→{x['to']}" for x in f)
    return s


UNAVAILABLE_CN = {"no_out": "去程找不到满足约束的航班/衔接", "no_ret": "回程找不到能按时到家的航班/衔接",
                  "no_hotel": "没有符合条件的酒店", "budget": "单次查询额度用完，未查完"}


def unavailable_hints(quotes, req, quotes_doc=None):
    hints = []
    doc = quotes_doc or {}
    tr = req["transport"]
    st = [q for q in quotes if q["status"] != "ok"]
    after_work_fail = [q for q in st if q["status"] == "no_out" and q["out"]["mode"] == "after_work"]
    if after_work_fail and not tr["redeye_out"]:
        hints.append(f"有 {len(after_work_fail)} 个「下班后出发」的日期赶不上 22 点前起飞的航班，接受去程红眼或特种兵可解锁")
    if any(q["status"] == "no_ret" for q in st):
        hints.append("部分返程按当前「最晚到家时间」赶不上，放宽到家时间或接受回程红眼可增加选择")
    if any(q["status"] == "no_hotel" for q in st):
        hints.append("部分日期没有符合星级/价格带/区域的酒店，可放宽酒店条件")
    if not tr["transfer_ok"]:
        hints.append("当前不接受中转，接受中转可能更便宜")
    ps, nq = len(doc.get("pattern_skipped") or []), len(doc.get("not_queried") or [])
    if ps:
        hints.append(f"同类出发/返程方式连续两次无结果，已跳过其余 {ps} 个同类日期组合以节省查询")
    if nq:
        hints.append(f"为控制查询量（单次 ≤30 次），另有 {nq} 个日期组合未查询；缩小时间窗口或固定晚数可覆盖更全")
    return hints


def rank(req, quotes_doc, goal=None, top_n=3):
    cfg = load_config("defaults")
    goal = goal or req["goal"]["priority"]
    budget = req["goal"].get("budget_total")
    quotes = quotes_doc["quotes"]
    best_per_cand, over_budget, min_total = [], 0, None
    key = sort_key(goal)
    for q in quotes:
        if q["status"] != "ok":
            continue
        combos = evaluate(q, req, cfg)
        if combos:
            m = min(c["price"]["total"] for c in combos)
            min_total = m if min_total is None else min(min_total, m)
        if budget:
            within = [c for c in combos if c["price"]["total"] <= budget]
            over_budget += len(combos) > 0 and not within
            combos = within
        if combos:
            best_per_cand.append(sorted(combos, key=key)[0])
    best_per_cand.sort(key=key)

    plans, used_dates = [], set()
    for c in best_per_cand:
        k = (c["dest"], c["out_date"])
        if k in used_dates:
            continue
        used_dates.add(k)
        plans.append(c)
        if len(plans) >= top_n:
            break
    if len(plans) < top_n:  # 不同日期不够时，用同日其他组合补足
        for c in best_per_cand:
            if c not in plans:
                plans.append(c)
            if len(plans) >= top_n:
                break

    max_total = max([c["price"]["total"] for c in best_per_cand], default=None)
    for i, p in enumerate(plans, 1):
        p["rank"] = i
        p["save_vs_max"] = round(max_total - p["price"]["total"], 2) if max_total is not None else 0
        p["brief"] = {"out": _leg_brief(p["out"]), "ret": _leg_brief(p["ret"])}
    # 推荐依据：与下一名的差异，供智能体写一句话理由（禁止自行编造）
    for i, p in enumerate(plans):
        other = plans[i + 1] if i + 1 < len(plans) else (plans[i - 1] if i > 0 else None)
        if other:
            p["reason_facts"] = {
                "compare_with_rank": other["rank"],
                "price_diff": round(other["price"]["total"] - p["price"]["total"], 2),
                "slot_diff": p["slot_count"] - other["slot_count"],
                "leave_diff": other["leave_days"] - p["leave_days"],
            }

    per_dest = {}
    if len(req["destinations"]) > 1:
        for dest in req["destinations"]:
            ds = [c for c in best_per_cand if c["dest"] == dest]
            per_dest[dest] = ({"candidate_id": ds[0]["candidate_id"], "out_date": ds[0]["out_date"],
                               "ret_date": ds[0]["ret_date"], "total": ds[0]["price"]["total"],
                               "slot_count": ds[0]["slot_count"], "leave_days": ds[0]["leave_days"]} if ds else None)

    notes = ["价格以飞猪预订页为准；酒店为每晚参考起价；火车余票需自行确认"]
    if any(p["estimated_holiday"] for p in plans):
        notes.append("2027 年放假安排为预估，以国务院公布为准")
    sys_msgs = sorted({x for x in quotes_doc.get("system_messages", []) if x})
    unavailable = [{"candidate_id": q["id"], "status": q["status"], "reason": UNAVAILABLE_CN.get(q["status"], q["status"])}
                   for q in quotes if q["status"] != "ok"]
    meta = quotes_doc.get("meta", {})
    return {
        "generated_for": str(today()), "goal": goal, "goal_cn": GOAL_CN[goal],
        "replay": bool(meta.get("replay")),
        "recorded_at": quotes_doc.get("recorded_at"),
        "errors": quotes_doc.get("errors", []),
        "warnings": quotes_doc.get("warnings", []),
        "meta": {**meta, "evaluated_candidates": len(best_per_cand), "over_budget_candidates": over_budget},
        "plans": plans,
        "per_destination_best": per_dest,
        "unavailable": unavailable,
        "unavailable_hints": ([f"预算 {budget:.0f} 元低于最低可行总价约 {min_total:.0f} 元：可提高预算、降低酒店星级或价格带、放开中转/红眼"]
                              if budget and min_total and not plans and min_total > budget else [])
                             + unavailable_hints(quotes, req, quotes_doc),
        "min_feasible_total": min_total,
        "not_queried": quotes_doc.get("not_queried", []),
        "notes": notes,
        "system_messages": sys_msgs,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    ap.add_argument("--quotes", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--goal", choices=["save", "play", "balance"])
    a = ap.parse_args()
    req = read_json(a.request)
    req = req.get("request", req)
    plan = rank(req, read_json(a.quotes), a.goal)
    write_json(a.out, plan)
    print(f"目标：{plan['goal_cn']}；可行方案 {plan['meta']['evaluated_candidates']} 个，输出前 {len(plan['plans'])} 个")
    for p in plan["plans"]:
        print(f"  {p['rank']}. {p['dest']} {p['out_date']}{weekday_cn(p['out_date'])}→{p['ret_date']}{weekday_cn(p['ret_date'])} "
              f"请假{p['leave_days']}天 可玩{p['slot_count']}个时段 总价¥{p['price']['total']:.0f} 比最贵省¥{p['save_vs_max']:.0f}")


if __name__ == "__main__":
    main()
