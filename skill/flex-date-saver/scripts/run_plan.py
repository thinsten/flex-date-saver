#!/usr/bin/env python3
"""总入口：①校验 → ②候选 → ③查价 → ④排序，输出 plan.json。

用法：
  python3 scripts/run_plan.py --request request.json --out plan.json [--workdir fds_run] [--cache-dir cache] [--replay]
  只改目标：python3 scripts/score_rank.py --request fds_run/request.normalized.json --quotes fds_run/quotes.json --out plan.json --goal play

退出码：0 成功（含无可行方案）；2 需求单有缺项/冲突（见 plan.json 的 validation）；3 查询被致命错误中止（风控/密钥/缺 CLI）
"""
import argparse
import json
import sys
import time
from pathlib import Path

from common import load_config, read_json, write_json
from fetch_quotes import fetch
from flyai_client import FlyaiClient
from plan_candidates import generate
from score_rank import rank
from validate_request import parse_request_text, validate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workdir", default="fds_run")
    ap.add_argument("--cache-dir", default=None, help="默认 <workdir>/cache")
    ap.add_argument("--replay", action="store_true", help="只读缓存，不联网")
    ap.add_argument("--goal", choices=["save", "play", "balance"])
    a = ap.parse_args()
    wd = Path(a.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    raw = parse_request_text(Path(a.request).read_text(encoding="utf-8"))
    v = validate(raw)
    write_json(wd / "request.normalized.json", v)
    if not v["ok"]:
        write_json(a.out, {"ok": False, "validation": {k: v[k] for k in ("missing", "conflicts", "warnings", "applied")}})
        print(json.dumps({"ok": False, "missing": v["missing"], "conflicts": v["conflicts"]}, ensure_ascii=False, indent=1))
        sys.exit(2)
    req = v["request"]
    if a.goal:
        req["goal"]["priority"] = a.goal

    cands, stats = generate(req)
    write_json(wd / "candidates.json", {"stats": stats, "candidates": cands})

    cfg = load_config("defaults")
    client = FlyaiClient(a.cache_dir or (wd / "cache"), max_calls=cfg["limits"]["max_calls"],
                         min_interval_s=cfg["limits"]["min_interval_s"], replay=a.replay,
                         rate_rules=cfg["limits"]["rate_rules"], gap_s=cfg["limits"]["gap_s"],
                         cooldown_s=cfg["limits"]["cooldown_s"], cache_ttl_s=cfg["limits"]["cache_ttl_s"],
                         err_ttl_s=cfg["limits"].get("err_ttl_s", 60))
    quotes = fetch(req, cands, client)
    write_json(wd / "quotes.json", quotes)

    plan = rank(req, quotes)
    plan["ok"] = True
    plan["validation"] = {k: v[k] for k in ("warnings", "applied")}
    plan["candidate_stats"] = stats
    plan["meta"]["elapsed_s"] = round(time.time() - t0, 1)
    write_json(a.out, plan)

    m = plan["meta"]
    print(f"目标：{plan['goal_cn']}｜候选 {stats['candidates']} 个，查询 {m.get('selected')} 个，可行 {m['evaluated_candidates']} 个｜"
          f"实时调用 {m['live_calls']} 次，缓存 {m['cache_hits']} 次，用时 {m['elapsed_s']}s"
          f"{'｜回放数据' if plan['replay'] else ''}")
    for p in plan["plans"]:
        print(f"  {p['rank']}. {p['dest']} {p['out_date']}({p['out_weekday']})→{p['ret_date']}({p['ret_weekday']}) "
              f"请假{p['leave_days']}天 可玩{p['slot_count']}时段 总价¥{p['price']['total']:.0f} 比最贵省¥{p['save_vs_max']:.0f}")
    for h in plan["unavailable_hints"]:
        print("  提示：" + h)
    if plan["errors"]:
        print("  错误：" + "；".join(f"{e['code']} {e['message']}" for e in plan["errors"]))
    if client.stopped:
        sys.exit(3)


if __name__ == "__main__":
    main()
