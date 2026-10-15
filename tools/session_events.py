#!/usr/bin/env python3
"""查看百炼会话事件（工具调用与回复），用于远程验收。会遮蔽 sk- 开头的密钥。

用法：python3 tools/session_events.py <session_id> [--wait] [--send "消息"]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_bailian import Api, load_env  # noqa: E402

MASK = re.compile(r"sk-[A-Za-z0-9._-]{8,}")


def mask(s):
    return MASK.sub("sk-***", s)


def text_of(ev):
    out = []
    for c in ev.get("content") or []:
        if isinstance(c, dict):
            for k in ("text", "input", "output", "arguments", "content"):
                v = c.get(k)
                if v:
                    out.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
    for k in ("name", "input", "output", "arguments"):
        v = ev.get(k)
        if v:
            out.append(f"{k}=" + (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)))
    return mask(" | ".join(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--wait", action="store_true", help="等待会话回到 idle")
    ap.add_argument("--send")
    ap.add_argument("--max", type=int, default=1500, help="每条事件最多显示字符数")
    a = ap.parse_args()
    api = Api(load_env())
    if a.send:
        api.req("POST", f"/sessions/{a.session}/events",
                {"input": [{"role": "user", "type": "message", "content": [{"type": "text", "text": a.send}]}]})
        time.sleep(5)
    if a.wait:
        for _ in range(120):
            s = api.req("GET", f"/sessions/{a.session}")
            if s.get("status") == "idle":
                break
            time.sleep(5)
        print(f"会话状态：{s.get('status')} stop_reason={s.get('stop_reason')}")
    evs, page = [], None
    for _ in range(20):
        d = api.req("GET", f"/sessions/{a.session}/events?limit=100" + (f"&page={page}" if page else ""))
        evs += d.get("data", [])
        page = d.get("next_page")
        if not page:
            break
    evs.sort(key=lambda e: e.get("created_at") or e.get("createdAt") or "")
    for e in evs:
        t = e.get("type")
        role = e.get("role", "")
        body = text_of(e)
        if not body:
            continue
        print(f"[{(e.get('created_at') or '')[11:19]}] {t} {role}: {body[:a.max]}")


if __name__ == "__main__":
    main()
