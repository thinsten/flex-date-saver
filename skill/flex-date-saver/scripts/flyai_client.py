"""flyai-cli 调用封装：串行限流、按参数缓存、退避重试、错误分类、回放模式。

实测约束（docs/api-notes.md）：并发 5 路触发频控，2 路并发约 20 次后触发风控 → 只能串行、间隔 ≥1.5s。
"""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path


class FlyaiError(Exception):
    pass


class RateLimiter:
    """跨进程限流 + 随机节奏 + 风控熔断。

    飞猪说明：QPS 上限 2；此外安全侧按「行为」风控，某一时间段频繁、规律的调用会被判为疑似脚本（HTTP 451）。
    实测 QPS 合规但 17 秒内连续 24 次仍被封。因此本限流器的目标是「像一个人在查」而不是「卡着上限跑」：
    - 硬上限：任意 1.2 秒 ≤2 次（QPS2 余量 20%）、60 秒 ≤20 次、10 分钟 ≤40 次
    - 实测依据：平均 1.5 QPS 连续第 24 次被封；随机 2.5~4 秒（约 19 次/分钟）连续 32 次未触发；约 12 分钟累计约 69 次被封
    - 平滑：两次请求之间随机间隔 min_gap~max_gap 秒（不规律、无突发，不会先连发再停）
    - 熔断：任一进程遇到风控，写入 10 分钟冷却期；冷却期内所有进程直接跳过，不再继续请求加重封控
    多个进程（本机或同一沙箱内多次规划）共享同一个状态文件。"""

    def __init__(self, rules=((2, 1.2), (10, 60), (40, 600)), min_gap=3.0, max_gap=7.0, cooldown_s=600, path=None):
        import random
        self.rand = random.Random()
        self.rules = [tuple(r) for r in rules]
        self.keep = max(w for _, w in self.rules)
        self.min_gap, self.max_gap, self.cooldown_s = min_gap, max_gap, cooldown_s
        self.path = Path(path or os.environ.get("FDS_RATE_FILE", "/tmp/flex-date-saver-flyai-rate.json"))

    def _locked(self, fn):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                st = json.loads(f.read() or "{}")
            except json.JSONDecodeError:
                st = {}
            if isinstance(st, list):
                st = {"ts": st}
            out, changed = fn(st)
            if changed:
                f.seek(0)
                f.truncate()
                f.write(json.dumps(st))
            return out

    def blocked_until(self):
        return self._locked(lambda st: (st.get("blocked_until", 0), False))

    def block(self):
        def fn(st):
            st["blocked_until"] = time.time() + self.cooldown_s
            return None, True
        self._locked(fn)

    def acquire(self):
        """拿到许可返回 True；处于风控冷却期返回 False（调用方应跳过）。"""
        while True:
            def fn(st):
                now = time.time()
                if st.get("blocked_until", 0) > now:
                    return ("blocked", 0), False
                ts = sorted(t for t in st.get("ts", []) if now - t < self.keep)
                wait = 0.0
                if ts:
                    gap = st.get("next_gap", self.min_gap)
                    wait = max(wait, gap - (now - ts[-1]))
                for mx, w in self.rules:
                    inw = [t for t in ts if now - t < w]
                    if len(inw) >= mx:
                        wait = max(wait, w - (now - inw[-mx]) + self.rand.uniform(0.5, 2.0))
                if wait > 0:
                    return ("wait", wait), False
                ts.append(now)
                st["ts"] = ts
                st["next_gap"] = round(self.rand.uniform(self.min_gap, self.max_gap), 2)
                return ("ok", 0), True
            kind, wait = self._locked(fn)
            if kind == "ok":
                return True
            if kind == "blocked":
                return False
            time.sleep(min(wait, 30))


class FlyaiClient:
    def __init__(self, cache_dir, max_calls=30, min_interval_s=0.0, replay=False, flyai_bin=None, timeout=90,
                 rate_rules=((2, 1.2), (20, 60), (40, 600)), gap_s=(2.5, 4.0), cooldown_s=600, cache_ttl_s=1800):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_calls = max_calls
        self.min_interval_s = min_interval_s
        self.replay = replay
        self.bin = flyai_bin or os.environ.get("FLYAI_BIN", "flyai")
        self.timeout = timeout
        self.live_calls = 0
        self.cache_hits = 0
        self.skipped = 0
        self.errors = []          # [{"code", "message", "args"}]
        self.stopped = None       # 致命错误码：risk_control / key_invalid / cli_missing
        self.trial_mode = False
        self.system_messages = set()
        self.recorded = set()
        self._last = 0.0
        self.limiter = RateLimiter(rate_rules, gap_s[0], gap_s[1], cooldown_s)
        self.cache_ttl_s = cache_ttl_s  # 实时模式下 30 分钟内的同一查询直接用缓存，减少重复调用；回放模式不看时效

    @staticmethod
    def key(args):
        return hashlib.sha1(json.dumps(args, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]

    def _err(self, code, msg, args):
        if not any(e["code"] == code for e in self.errors) or code in ("no_data",):
            self.errors.append({"code": code, "message": msg, "args": args})

    def call(self, args):
        """返回 data dict（含 itemList）或 None（无结果 / 未执行）。永不抛异常。"""
        path = self.cache_dir / f"{self.key(args)}.json"
        if path.exists():
            rec = json.loads(path.read_text(encoding="utf-8"))
            fresh = self.replay or (time.time() - path.stat().st_mtime) < self.cache_ttl_s
            if fresh:
                self.cache_hits += 1
                return self._extract(rec, args, from_cache=True)
        if self.replay:
            self.skipped += 1
            self._err("replay_miss", "回放模式下缓存未命中，跳过该查询", args)
            return None
        if self.stopped:
            self.skipped += 1
            return None
        if self.live_calls >= self.max_calls:
            self.skipped += 1
            self._err("budget_exceeded", f"已达单次规划调用上限 {self.max_calls} 次，其余查询跳过", args)
            return None

        rec = None
        for attempt in range(5):
            wait = self.min_interval_s - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            if not self.limiter.acquire():  # 每次真实请求（含重试）都先拿许可；风控冷却期内直接停止
                self.stopped = "risk_control"
                self._err("risk_control", "飞猪接口处于风控冷却期（约 10 分钟），已暂停查询，请稍后重试", args)
                return None
            t0 = time.time()
            try:
                p = subprocess.run([self.bin, *args], capture_output=True, text=True, timeout=self.timeout)
            except FileNotFoundError:
                self.stopped = "cli_missing"
                self._err("cli_missing", f"找不到 flyai 命令（{self.bin}），请在运行环境预装 @fly-ai/flyai-cli@1.0.16", args)
                return None
            except subprocess.TimeoutExpired:
                self._last = time.time()
                if attempt < 1:
                    continue
                self._err("timeout", "查询超时", args)
                return None
            self._last = time.time()
            self.live_calls += 1
            err = p.stderr
            if "Rate limit" in err:
                time.sleep(1.5 * (attempt + 1))
                continue
            if "risk control" in err:
                self.limiter.block()  # 通知其他进程一起冷却，避免继续请求加重封控
                self.stopped = "risk_control"
                self._err("risk_control", "飞猪接口触发风控（短时间调用过多），已停止后续查询，请稍后重试", args)
                return None
            if "Invalid API key" in err or "MCP HTTP 401" in err:
                self.stopped = "key_invalid"
                self._err("key_invalid", "飞猪接口密钥无效（401），请检查 FLYAI_API_KEY", args)
                return None
            try:
                body = json.loads(p.stdout) if p.stdout.strip() else None
            except json.JSONDecodeError:
                body = None
            rec = {"args": args, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "elapsed_s": round(time.time() - t0, 2), "returncode": p.returncode, "stdout": body,
                   "stderr_tail": "\n".join(l for l in err.splitlines() if "NODE_TLS" not in l and "trace-warn" not in l)[-300:]}
            break
        if rec is None:
            self._err("rate_limited", "多次重试仍被频控", args)
            return None
        if isinstance(rec.get("stdout"), dict):  # 只缓存成功解析的返回（含 data:null 的「无结果」）
            path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return self._extract(rec, args, from_cache=False)

    def _extract(self, rec, args, from_cache):
        body = rec.get("stdout")
        if not isinstance(body, dict):
            self._err("bad_response", (rec.get("stderr_tail") or "返回无法解析")[-120:], args)
            return None
        sm = body.get("systemMessage") or ""
        if sm:
            self.system_messages.add(sm)
        if rec.get("recorded_at"):
            self.recorded.add(rec["recorded_at"][:16])
        if "体验模式" in sm:
            self.trial_mode = True
            self._err("trial_mode", "飞猪接口处于体验模式（Key 未生效），报价不可作为正式结果", args)
        data = body.get("data")
        if not isinstance(data, dict):
            return None
        data = dict(data)
        data["_system_message"] = sm
        data["_recorded_at"] = rec.get("recorded_at")
        return data

    def summary(self):
        return {"live_calls": self.live_calls, "cache_hits": self.cache_hits, "skipped": self.skipped,
                "max_calls": self.max_calls, "stopped": self.stopped, "trial_mode": self.trial_mode,
                "replay": self.replay}
