#!/usr/bin/env python3
"""在百炼上跑一条对话用例：新建会话（或复用）→ 依次发送消息、每条等智能体回到 idle → 把完整对话与工具调用写入 docs/test-runs/<name>.md。

用法：
  python3 tools/bailian_case.py <name> --msg "第一条" --msg "第二条" [--session sesn_xxx] [--bad-key]
  消息以 @ 开头表示读文件，如 --msg @tests/requests/sample_two_dest.json（会自动包成【规划需求单 v1】）
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_bailian import Api, load_env, state  # noqa: E402
from session_events import mask, text_of  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "test-runs"


def new_session(api, st, env, bad_key, title):
    key = "sk-invalid-for-test-000000000000" if bad_key else env["FLYAI_API_KEY"]
    for i in range(30):
        try:
            return api.req("POST", "/sessions", {"agent": st["agent"]["id"], "environment_id": st["environment"]["id"],
                                                 "title": title, "environment_variables": {"FLYAI_API_KEY": key}})["id"]
        except SystemExit as e:
            if "still being prepared" in str(e) and i < 29:
                time.sleep(20)
                continue
            raise


def wait_idle(api, sid, timeout=900):
    t0 = time.time()
    time.sleep(4)
    while time.time() - t0 < timeout:
        s = api.req("GET", f"/sessions/{sid}")
        if s.get("status") == "idle":
            return s
        if s.get("status") == "terminated":
            raise SystemExit("会话已终止")
        time.sleep(5)
    raise SystemExit("等待超时")


def events(api, sid):
    evs, page = [], None
    for _ in range(30):
        d = api.req("GET", f"/sessions/{sid}/events?limit=100" + (f"&page={page}" if page else ""))
        evs += d.get("data", [])
        page = d.get("next_page")
        if not page:
            break
    return sorted(evs, key=lambda e: e.get("created_at") or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--msg", action="append", required=True)
    ap.add_argument("--session")
    ap.add_argument("--bad-key", action="store_true")
    a = ap.parse_args()
    env = load_env()
    api, st = Api(env), state()
    sid = a.session or new_session(api, st, env, a.bad_key, f"用例 {a.name}")
    start = time.strftime("%Y-%m-%dT%H:%M")
    print(f"会话 {sid}", flush=True)
    before = {e.get("id") for e in events(api, sid)} if a.session else set()
    t_all = time.time()
    for m in a.msg:
        if m.startswith("@"):
            m = "【规划需求单 v1】\n```json\n" + (ROOT / m[1:]).read_text() + "\n```"
        api.req("POST", f"/sessions/{sid}/events",
                {"input": [{"role": "user", "type": "message", "content": [{"type": "text", "text": m}]}]})
        t0 = time.time()
        wait_idle(api, sid)
        print(f"  已回复（{time.time() - t0:.0f}s）", flush=True)
    evs = [e for e in events(api, sid) if e.get("id") not in before]
    OUT.mkdir(parents=True, exist_ok=True)
    lines = [f"# 用例 {a.name}", "", f"- 会话：`{sid}`", f"- 开始：{start}，总耗时 {time.time() - t_all:.0f}s",
             f"- 智能体：`{st['agent']['id']}`", ""]
    tools = []
    for e in evs:
        t, role, body = e.get("type"), e.get("role", ""), text_of(e)
        if not body:
            continue
        if t == "message":
            lines += [f"## {'用户' if role == 'user' else '智能体'}", "", body, ""]
        else:
            tools.append(f"- `{t}` {body[:300]}")
    if tools:
        lines += ["## 工具调用（节选）", ""] + tools[:60] + [""]
    path = OUT / f"{a.name}.md"
    path.write_text(mask("\n".join(lines)))
    last = [e for e in evs if e.get("type") == "message" and e.get("role") == "assistant"]
    print(f"已写入 {path.relative_to(ROOT)}；最后一条回复前 300 字：\n{text_of(last[-1])[:300] if last else '（无）'}")


if __name__ == "__main__":
    main()
