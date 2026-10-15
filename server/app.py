#!/usr/bin/env python3
"""flex-date-saver 网页服务（Python 3.8+ 标准库，无第三方依赖）。

- 访问控制：统一访问密码（环境变量 FDS_PASSWORD），登录后发 HttpOnly 会话 cookie
- 一句话填表：调百炼兼容接口（FDS_LLM_MODEL，默认 qwen-flash）把自然语言转成需求单草稿，前端回填表单由用户确认
- 规划：单线程队列逐个执行 skill 的 run_plan.py（保证飞猪 QPS ≤2），结果存 data/jobs/<id>/
- 切换目标：直接用已存的 quotes.json 重排，不重新查价
- 历史与分享：最近规划列表；分享链接 /share/<id>?k=<签名> 免登录只读

环境变量（放 /etc/flex-date-saver.env，不进 git）：
  FDS_PASSWORD  FDS_SECRET  FLYAI_API_KEY  DASHSCOPE_API_KEY  DASHSCOPE_BASE_URL
  可选：FDS_PORT(8092) FDS_HOST(127.0.0.1) FDS_DATA_DIR FLYAI_BIN FDS_LLM_MODEL FDS_DAILY_PLAN_LIMIT(80) FDS_DAILY_PARSE_LIMIT(300)
"""
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skill" / "flex-date-saver"
SCRIPTS = SKILL / "scripts"
WEB = ROOT / "web" / "index.html"
sys.path.insert(0, str(SCRIPTS))

from score_rank import rank  # noqa: E402
from validate_request import validate  # noqa: E402

DATA = Path(os.environ.get("FDS_DATA_DIR", ROOT / "data"))
JOBS = DATA / "jobs"
PASSWORD = os.environ.get("FDS_PASSWORD", "")
SECRET = (os.environ.get("FDS_SECRET") or secrets.token_hex(32)).encode()
LLM_BASE = os.environ.get("DASHSCOPE_BASE_URL", "").rstrip("/")
LLM_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
LLM_MODEL = os.environ.get("FDS_LLM_MODEL", "qwen3.7-plus")
DAILY_PLAN = int(os.environ.get("FDS_DAILY_PLAN_LIMIT", "80"))
DAILY_PARSE = int(os.environ.get("FDS_DAILY_PARSE_LIMIT", "300"))
SESSION_DAYS = 30
JOB_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-f0-9]{6}$")

JOBS.mkdir(parents=True, exist_ok=True)
(DATA / "cache").mkdir(parents=True, exist_ok=True)
(DATA / "home").mkdir(parents=True, exist_ok=True)

jobs_lock = threading.Lock()
job_queue = queue.Queue()
running = {"id": None, "since": None}
counters = {"day": "", "plan": 0, "parse": 0}
fail_login = {}  # ip -> [timestamps]


def log(*a):
    print(time.strftime("%F %T"), *a, flush=True)


# ------------------------------------------------------------------ 工具
def sign(s):
    return hmac.new(SECRET, s.encode(), hashlib.sha256).hexdigest()[:32]


def make_session():
    exp = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{exp}.{sign('s.' + exp)}"


def check_session(tok):
    try:
        exp, sig = tok.split(".", 1)
        return hmac.compare_digest(sig, sign("s." + exp)) and int(exp) > time.time()
    except Exception:
        return False


def share_key(job_id):
    return sign("share." + job_id)[:16]


def bump(kind):
    today = str(date.today())
    with jobs_lock:
        if counters["day"] != today:
            counters.update(day=today, plan=0, parse=0)
        limit = DAILY_PLAN if kind == "plan" else DAILY_PARSE
        if counters[kind] >= limit:
            return False
        counters[kind] += 1
        return True


def read_json(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(p, obj):
    p = Path(p)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def job_dir(jid):
    return JOBS / jid


def job_meta(jid):
    return read_json(job_dir(jid) / "meta.json", {})


def set_meta(jid, **kw):
    with jobs_lock:
        m = job_meta(jid)
        m.update(kw)
        write_json(job_dir(jid) / "meta.json", m)
        return m


def title_of(req):
    dests = " / ".join(req.get("destinations") or [])
    w = req.get("window") or {}
    n = req.get("nights") or {}
    nights = f"{n.get('min')}晚" if n.get("min") == n.get("max") else f"{n.get('min')}~{n.get('max')}晚"
    return f"{req.get('home_city', '')} → {dests} · {w.get('start', '')[5:]}~{w.get('end', '')[5:]} · {nights}"


def plan_brief(plan):
    ps = (plan or {}).get("plans") or []
    if not ps:
        return "没有可行方案"
    p = ps[0]
    return f"首选 {p['dest']} {p['out_date'][5:]}→{p['ret_date'][5:]} ¥{p['price']['total']:.0f} · 请假{p['leave_days']}天 · {p['slot_count']}个时段"


# ------------------------------------------------------------------ 规划队列
def queue_position(jid):
    ids = [x for x in list(job_queue.queue)]
    return ids.index(jid) + 1 if jid in ids else 0


def worker():
    env = dict(os.environ)
    env["HOME"] = str(DATA / "home")
    env.setdefault("FDS_RATE_FILE", str(DATA / "flyai-rate.json"))
    env.pop("FDS_TODAY", None)
    while True:
        jid = job_queue.get()
        d = job_dir(jid)
        running.update(id=jid, since=time.time())
        set_meta(jid, status="running", started_at=time.time())
        log("plan start", jid)
        try:
            p = subprocess.run([sys.executable, str(SCRIPTS / "run_plan.py"), "--request", str(d / "request.json"),
                                "--out", str(d / "plan.json"), "--workdir", str(d / "work"),
                                "--cache-dir", str(DATA / "cache")],
                               cwd=str(SKILL), env=env, capture_output=True, text=True, timeout=900)
            plan = read_json(d / "plan.json")
            (d / "run.log").write_text((p.stdout or "") + "\n--- stderr ---\n" + (p.stderr or "")[-4000:], encoding="utf-8")
            if plan and plan.get("ok"):
                set_meta(jid, status="done", finished_at=time.time(), brief=plan_brief(plan),
                         goal=plan.get("goal"), exit_code=p.returncode)
            elif plan and plan.get("ok") is False:
                set_meta(jid, status="invalid", finished_at=time.time(), brief="需求单有缺项或冲突")
            else:
                set_meta(jid, status="error", finished_at=time.time(), brief="规划失败", error=(p.stderr or "")[-500:])
            log("plan end", jid, p.returncode)
        except subprocess.TimeoutExpired:
            set_meta(jid, status="error", finished_at=time.time(), brief="查询超时（15 分钟）")
        except Exception:
            set_meta(jid, status="error", finished_at=time.time(), brief="规划失败", error=traceback.format_exc()[-500:])
        finally:
            running.update(id=None, since=None)
            job_queue.task_done()


def recover_jobs():
    for d in JOBS.iterdir():
        m = read_json(d / "meta.json", {})
        if m.get("status") in ("queued", "running"):
            m.update(status="error", brief="服务重启中断，请重新规划")
            write_json(d / "meta.json", m)


# ------------------------------------------------------------------ 一句话填表
LLM_SYSTEM = """你把用户的出行需求转成「规划需求单」JSON。今天是 {today}（{weekday}）。只输出一个 JSON 对象，不要任何解释。
最重要的规则：只输出用户明确说了的字段，没提到的字段和子字段一律不要输出（表单会用默认值）。例如用户没说上班时间就不要输出 start_time，没说中转就不要输出 transfer_ok，没说房间数就不要输出 rooms。宁可少填，不要猜。字段：
- home_city: 常住/出发城市（字符串）
- destinations: 目的地数组，最多 2 个
- window: {{"start":"YYYY-MM-DD","end":"YYYY-MM-DD"}} 出行时间窗口。「12月」=当年或下一个 12 月整月；「下个月」按今天推算；窗口开始不能早于明天
- nights: {{"min":整数,"max":整数}} 玩几晚；「3天2晚」=2晚；「玩3天」=2晚；「两三晚」=min2 max3
- travelers: {{"adults":整数,"children":整数}}；「我和女朋友」=2 成人；「一家三口」=2 成人 1 儿童
- rooms: 整数
- work: {{"off_time":"HH:MM","start_time":"HH:MM"}} 下班/上班时间
- stations: [{{"name":"机场或车站全称","type":"air|train","commute_min":整数}}] 只在用户说了到某机场/车站要多久时填，名称用用户说的那个（「硕放」=苏南硕放机场，「虹桥」=上海虹桥机场，「东站」=杭州东站）；用户只说「到机场 50 分钟」时 name 填「机场」
- leave: {{"max_days":整数,"weekdays":["周五","周一"],"dates":["YYYY-MM-DD"]}}；「最多请1天假」=max_days 1；用户说「周五/周一可以请假」填 weekdays；说了具体日期填 dates；只说天数没说哪天时 weekdays 填 ["周一","周二","周三","周四","周五"]；完全没提请假就不输出 leave
- return_home_by: "HH:MM" 返程最晚到家
- transport: {{"redeye_out":bool,"redeye_back":bool,"transfer_ok":bool,"max_layover_min":整数,"nearby_origin_ok":bool,"nearby_cities":[城市],"max_train_min":整数,"cabin":"经济舱|公务舱|头等舱","train_seat":["二等座"]}}
- hotel: {{"mode":"filter|specific","stars":[1-5],"types":["酒店|民宿|客栈"],"chain":"any|chain_only|brand:品牌名","area":"","price_min":数或null,"price_max":数或null,"specific_name":null}}；「五星」=stars[5]；「四五星」=[4,5]；「住全季」=chain "brand:全季"
- goal: {{"priority":"save|play|balance","special_forces":bool,"budget_total":数或null}}；「最省钱」=save；「多玩几天/玩得多」=play；「特种兵」=special_forces true
另外输出 "_notes": 字符串数组，写你做的推断或用户说得含糊的地方（例如「“12月”按 2026-12-01~12-31 理解」），没有就空数组。"""


def llm_parse(text):
    if not (LLM_BASE and LLM_KEY):
        raise RuntimeError("服务器未配置大模型接口")
    today = date.today()
    sys_prompt = LLM_SYSTEM.format(today=today.isoformat(), weekday="周" + "一二三四五六日"[today.weekday()])
    body = {"model": LLM_MODEL, "temperature": 0.1, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": text[:1000]}]}
    req = urllib.request.Request(LLM_BASE + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"大模型接口报错 {e.code}: {e.read().decode()[:200]}")
    content = d["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    obj = json.loads(m.group(0) if m else content)
    notes = obj.pop("_notes", []) or []
    lv = obj.get("leave")
    if isinstance(lv, dict) and ("weekdays" in lv or "dates" in lv):  # 星期几 → 窗口内具体工作日（由代码展开，不让模型数日历）
        from common import HolidayCalendar, daterange
        cal, w = HolidayCalendar(), obj.get("window") or {}
        days = set(d for d in lv.get("dates") or [])
        wk = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}
        want = {wk[x] for x in lv.get("weekdays") or [] if x in wk}
        if want and w.get("start") and w.get("end"):
            days |= {str(d) for d in daterange(w["start"], w["end"]) if d.weekday() in want and cal.is_workday(d)}
        obj["leave"] = {"allowed_dates": sorted(days), "max_days": lv.get("max_days", len(days))}
    for st in obj.get("stations") or []:  # 「机场」泛指 → 交给前端匹配常住城市的第一个机场
        if st.get("name") in ("机场", "火车站", "高铁站"):
            st["generic"] = st.get("type") or ("air" if st["name"] == "机场" else "train")
    return obj, notes


# ------------------------------------------------------------------ HTTP
class H(BaseHTTPRequestHandler):
    server_version = "fds/1.0"

    def log_message(self, fmt, *a):
        pass

    # --- 输出
    def send(self, code, obj=None, body=None, ctype="application/json; charset=utf-8", headers=None):
        data = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def ok(self, **kw):
        self.send(200, {"ok": True, **kw})

    def err(self, code, error, **kw):
        self.send(code, {"ok": False, "error": error, **kw})

    def ip(self):
        return (self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or self.client_address[0])

    def authed(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "fds_session" and check_session(v):
                return True
        return False

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 256 * 1024:
            raise ValueError("请求太大")
        return json.loads(self.rfile.read(n).decode() or "{}") if n else {}

    def page(self, inject):
        html = WEB.read_text(encoding="utf-8").replace("<!--FDS_SERVER-->", f"<script>{inject}</script>", 1)
        self.send(200, body=html.encode(), ctype="text/html; charset=utf-8")

    # --- 路由
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        try:
            if path == "/healthz":
                return self.ok(queue=job_queue.qsize(), running=running["id"] is not None,
                               flyai_bin=os.path.exists(os.environ.get("FLYAI_BIN", "/nonexistent")))
            if path in ("/", "/index.html"):
                return self.page("window.FDS_SERVER=true;")
            m = re.match(r"^/share/([0-9a-f-]+)$", path)
            if m:
                jid, k = m.group(1), (qs.get("k") or [""])[0]
                if not (JOB_ID.match(jid) and hmac.compare_digest(k, share_key(jid))):
                    return self.send(404, body="链接无效".encode(), ctype="text/plain; charset=utf-8")
                return self.page(f"window.FDS_SERVER=true;window.FDS_SHARE={json.dumps({'id': jid, 'k': k})};")
            m = re.match(r"^/api/share/([0-9a-f-]+)$", path)
            if m:
                jid, k = m.group(1), (qs.get("k") or [""])[0]
                if not (JOB_ID.match(jid) and hmac.compare_digest(k, share_key(jid))):
                    return self.err(404, "not_found")
                return self.ok(**self.job_payload(jid, with_request=False))
            if path == "/api/me":
                return self.ok(authed=self.authed())
            if not self.authed():
                return self.err(401, "need_login")
            if path == "/api/history":
                items = []
                for d in sorted(JOBS.iterdir(), reverse=True)[:40]:
                    m = read_json(d / "meta.json", {})
                    if m:
                        items.append({"id": d.name, "title": m.get("title"), "status": m.get("status"),
                                      "brief": m.get("brief"), "created_at": m.get("created_at")})
                return self.ok(items=items)
            m = re.match(r"^/api/job/([0-9a-f-]+)$", path)
            if m and JOB_ID.match(m.group(1)) and job_dir(m.group(1)).exists():
                return self.ok(**self.job_payload(m.group(1)))
            return self.err(404, "not_found")
        except Exception:
            log("GET error", path, traceback.format_exc())
            return self.err(500, "server_error")

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            b = self.body()
        except Exception as e:
            return self.err(400, "bad_request", message=str(e))
        try:
            if path == "/api/login":
                ip, now = self.ip(), time.time()
                fails = [t for t in fail_login.get(ip, []) if now - t < 900]
                if len(fails) >= 8:
                    return self.err(429, "too_many_attempts", message="密码错误次数太多，15 分钟后再试")
                if PASSWORD and hmac.compare_digest(str(b.get("password", "")), PASSWORD):
                    fail_login.pop(ip, None)
                    cookie = f"fds_session={make_session()}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_DAYS * 86400}"
                    return self.send(200, {"ok": True}, headers={"Set-Cookie": cookie})
                fail_login[ip] = fails + [now]
                return self.err(401, "wrong_password", message="密码不对")
            if not self.authed():
                return self.err(401, "need_login")
            if path == "/api/parse":
                text = str(b.get("text", "")).strip()
                if not text:
                    return self.err(400, "empty", message="先说说你的需求")
                if not bump("parse"):
                    return self.err(429, "daily_limit", message="今天的 AI 填表次数用完了，请直接填表")
                obj, notes = llm_parse(text)
                return self.ok(request=obj, notes=notes)
            if path == "/api/plan":
                req = b.get("request") or {}
                v = validate(req)
                if not v["ok"]:
                    return self.err(400, "invalid", missing=v["missing"], conflicts=v["conflicts"], warnings=v["warnings"])
                if not bump("plan"):
                    return self.err(429, "daily_limit", message=f"今天的规划次数（{DAILY_PLAN}）用完了，明天再来")
                jid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
                d = job_dir(jid)
                d.mkdir(parents=True)
                write_json(d / "request.json", req)
                set_meta(jid, status="queued", created_at=time.time(), title=title_of(v["request"]), ip=self.ip())
                job_queue.put(jid)
                return self.ok(id=jid, position=queue_position(jid), warnings=v["warnings"], applied=v["applied"])
            m = re.match(r"^/api/job/([0-9a-f-]+)/goal$", path)
            if m and JOB_ID.match(m.group(1)):
                jid, goal = m.group(1), b.get("goal")
                d = job_dir(jid)
                if goal not in ("save", "play", "balance"):
                    return self.err(400, "bad_goal")
                norm, quotes, old = (read_json(d / "work" / "request.normalized.json"),
                                     read_json(d / "work" / "quotes.json"), read_json(d / "plan.json"))
                if not (norm and quotes and old):
                    return self.err(409, "not_ready")
                req = norm.get("request", norm)
                plan = rank(req, quotes, goal)
                for k in ("ok", "validation", "candidate_stats"):
                    if k in old:
                        plan[k] = old[k]
                plan["meta"]["elapsed_s"] = old.get("meta", {}).get("elapsed_s")
                write_json(d / "plan.json", plan)
                set_meta(jid, goal=goal, brief=plan_brief(plan))
                return self.ok(**self.job_payload(jid))
            return self.err(404, "not_found")
        except RuntimeError as e:
            return self.err(502, "upstream", message=str(e))
        except Exception:
            log("POST error", path, traceback.format_exc())
            return self.err(500, "server_error")

    def job_payload(self, jid, with_request=True):
        d = job_dir(jid)
        m = job_meta(jid)
        out = {"id": jid, "status": m.get("status"), "title": m.get("title"), "brief": m.get("brief"),
               "created_at": m.get("created_at"), "position": queue_position(jid),
               "elapsed": round(time.time() - m["started_at"]) if m.get("status") == "running" and m.get("started_at") else None,
               "share_url": f"/share/{jid}?k={share_key(jid)}"}
        if with_request:
            out["request"] = read_json(d / "request.json")
        if m.get("status") in ("done", "invalid"):
            out["plan"] = read_json(d / "plan.json")
        if m.get("status") == "error":
            out["error"] = m.get("brief")
        return out


def main():
    if not PASSWORD:
        log("警告：未设置 FDS_PASSWORD，所有人都无法登录")
    recover_jobs()
    threading.Thread(target=worker, daemon=True).start()
    host, port = os.environ.get("FDS_HOST", "127.0.0.1"), int(os.environ.get("FDS_PORT", "8092"))
    log(f"flex-date-saver web on {host}:{port}, data={DATA}, model={LLM_MODEL}")
    ThreadingHTTPServer((host, port), H).serve_forever()


if __name__ == "__main__":
    main()
