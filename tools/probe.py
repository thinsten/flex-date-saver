#!/usr/bin/env python3
"""阶段0 接口核验工具：并发调用 flyai-cli，把真实原始返回录制到 tests/fixtures/real/<name>.json。

用法：python3 tools/probe.py <probe_set.json> [--force]
probe_set.json 形如 {"name": ["search-flight", "--origin", "上海", ...], ...}
录制文件包含 args / recorded_at / returncode / stdout(JSON) / stderr 末尾，绝不写入任何密钥。
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "fixtures" / "real"


def run(name, args, force):
    path = OUT / f"{name}.json"
    if path.exists() and not force:
        return name, "cached", json.loads(path.read_text())
    t0 = time.time()
    flyai = os.environ.get("FLYAI_BIN", "flyai")
    for attempt in range(4):  # 实测并发 5 会触发 Rate limit exceeded，退避重试
        p = subprocess.run([flyai, *args], capture_output=True, text=True, timeout=90)
        if "Rate limit" not in p.stderr:
            break
        time.sleep(3 * (attempt + 1))
    try:
        body = json.loads(p.stdout) if p.stdout.strip() else None
    except json.JSONDecodeError:
        body = {"_raw": p.stdout[:2000]}
    err = "\n".join(l for l in p.stderr.splitlines() if "NODE_TLS" not in l and "trace-warnings" not in l)
    rec = {
        "args": args,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_s": round(time.time() - t0, 2),
        "attempts": attempt + 1,
        "returncode": p.returncode,
        "stdout": body,
        "stderr_tail": err[-500:],
    }
    path.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return name, "live", rec


def main():
    probes = json.loads(Path(sys.argv[1]).read_text())
    force = "--force" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(int(os.environ.get("PROBE_WORKERS", "2"))) as ex:
        for name, mode, rec in ex.map(lambda kv: run(kv[0], kv[1], force), probes.items()):
            body = rec.get("stdout") or {}
            data = body.get("data") if isinstance(body, dict) else None
            n = len(data.get("itemList") or []) if isinstance(data, dict) else ("None" if data is None else type(data).__name__)
            sm = (body.get("systemMessage") or "") if isinstance(body, dict) else ""
            print(f"{name:<28} {mode:<6} rc={rec['returncode']} items={n} {rec.get('elapsed_s','')}s"
                  f"{'  [体验模式!]' if '体验模式' in sm else ''}{'  ERR:'+rec['stderr_tail'][-80:] if rec['returncode'] else ''}")


if __name__ == "__main__":
    main()
