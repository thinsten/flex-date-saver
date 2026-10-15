#!/usr/bin/env python3
"""③ 串行限流查价：按候选行程查去程、回程、邻近城市火车、酒店，过滤后输出 quotes.json。

用法：python3 fetch_quotes.py --request request.normalized.json --candidates candidates.json \
        --out quotes.json [--cache-dir cache] [--replay]

要点（开发指南 2.3 / 2.5 / 2.7，及 docs/api-notes.md 的实测修订）：
- 只串行调用，间隔 ≥1.5s，单次规划 ≤30 次；风控/密钥失效立即停止
- 同一 (出发地, 目的地, 日期) 的航班查询全部候选共享，不带时段参数，在本地按时刻过滤
- 往返价 = 两段单程价相加（--back-date 正式 Key 下不可用）
- 火车座席参数不生效 → 本地按 seatClassName 过滤；余票一律标「余票需自行确认」
- 酒店价格为每晚起价；区域：行政区用 --dest-name，商圈/景区本地过滤；连锁按名称品牌词识别
- 候选太多超出预算时：先做 1~2 次日期区间查询拿到低价线索，再按目标排序截断
"""
import argparse
import re
from datetime import timedelta

from common import load_config, parse_dt, fmt_dt, read_json, write_json, d
from flyai_client import FlyaiClient

STAR_MAP = {"五星级": 5, "豪华型": 5, "四星级": 4, "高档型": 4, "三星级": 3, "舒适型": 3, "经济型": 2}


# ---------------------------------------------------------------- 解析
def _f(x):
    try:
        return float(str(x).replace("¥", "").replace("￥", "").replace(",", "").replace("起", "").strip())
    except (TypeError, ValueError):
        return None


def parse_journey(item, kind):
    """把接口 item 解析成统一的段结构；只取第一个 journey（单程）。"""
    js = item.get("journeys") or []
    if not js:
        return None
    segs = []
    for g in js[0].get("segments") or []:
        try:
            segs.append({
                "no": g.get("marketingTransportNo"),
                "dep": parse_dt(g["depDateTime"]), "arr": parse_dt(g["arrDateTime"]),
                "from": g.get("depStationShortName") or g.get("depStationName"),
                "to": g.get("arrStationShortName") or g.get("arrStationName"),
                "from_full": g.get("depStationName"), "to_full": g.get("arrStationName"),
                "seat": g.get("seatClassName"),
            })
        except (KeyError, ValueError):
            return None
    if not segs:
        return None
    price = _f(item.get("ticketPrice") if kind == "flight" else item.get("price"))
    if price is None:
        return None
    layovers = [int((segs[i + 1]["dep"] - segs[i]["arr"]).total_seconds() // 60) for i in range(len(segs) - 1)]
    return {"kind": kind, "segments": segs, "price": price, "url": item.get("jumpUrl"),
            "layovers": layovers, "direct": len(segs) == 1}


def _seg_json(s):
    return {k: (fmt_dt(v) if k in ("dep", "arr") else v) for k, v in s.items()}


def leg_json(j):
    return {"kind": j["kind"], "price": j["price"], "url": j["url"], "direct": j["direct"],
            "layovers_min": j["layovers"], "segments": [_seg_json(s) for s in j["segments"]]}


def is_redeye(j, cfg):
    dep, arr = j["segments"][0]["dep"], j["segments"][-1]["arr"]
    dep_m = dep.hour * 60 + dep.minute
    arr_m = arr.hour * 60 + arr.minute
    lo, hi = _hm(cfg["redeye"]["arr_from"]), _hm(cfg["redeye"]["arr_to"])
    return dep_m >= _hm(cfg["redeye"]["dep_from"]) or lo <= arr_m < hi or (arr.date() > dep.date() and arr_m < hi)


def _hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- 通勤与换乘
class Geo:
    def __init__(self, req, cfg):
        self.req, self.cfg = req, cfg
        self.transfer = load_config("transfer_estimates")

    def commute(self, short, typ):
        """返回 (分钟, 是否估算)。short 为接口车站/机场简称，如「虹桥」「苏州北」。"""
        for s in self.req["stations"]:
            if s["type"] != typ:
                continue
            name = s["name"].replace("站", "").replace("机场", "").replace("国际", "")
            if short and (short in s["name"] or name.endswith(short) or short in name):
                return s["commute_min"], False
        return self.cfg["default_commute_min"][typ], True

    def known_transfer(self, station, airport):
        for k, v in self.transfer["station_to_airport"].items():
            s, a = k.split("|")
            if station == s and a in (airport or ""):
                return v
        return None

    def station_to_airport(self, station, airport):
        v = self.known_transfer(station, airport)
        return v if v is not None else self.transfer["default_station_to_airport"]


# ---------------------------------------------------------------- 查询参数
def flight_args(origin, dest, day, req, hour_start=None, arr_hour_end=None):
    a = ["search-flight", "--origin", origin, "--destination", dest, "--dep-date", str(day), "--sort-type", "3"]
    if not req["transport"]["transfer_ok"]:
        a += ["--journey-type", "1"]
    if hour_start is not None:  # 下班后出发：只看可赶上的时段，否则「当天最便宜的 10 条」里可能没有晚班
        a += ["--dep-hour-start", str(max(0, min(23, hour_start)))]
    if arr_hour_end is not None:  # 返程：只看能按时到家的航班，避免「最便宜 10 条」全是赶不上的晚班
        a += ["--arr-hour-end", str(max(1, min(24, arr_hour_end)))]
    return a


def train_args(origin, dest, day, hour_start=None):
    a = ["search-train", "--origin", origin, "--destination", dest, "--dep-date", str(day), "--sort-type", "6"]
    if hour_start is not None:
        a += ["--dep-hour-start", str(max(0, min(23, hour_start)))]
    return a


def hotel_args(dest, ci, co, req):
    h = req["hotel"]
    area = (h.get("area") or "").strip()
    dest_name = f"{dest}{area}" if area and re.search(r"(区|县|市)$", area) else dest
    a = ["search-hotel", "--dest-name", dest_name, "--check-in-date", str(ci), "--check-out-date", str(co),
         "--sort", "price_asc"]
    if h.get("mode") == "specific" and h.get("specific_name"):
        a += ["--key-words", h["specific_name"]]
        return a
    chain = h.get("chain") or "any"
    if chain.startswith("brand:"):
        a += ["--key-words", chain.split(":", 1)[1]]
    if h.get("stars"):
        a += ["--hotel-stars", ",".join(str(s) for s in sorted(h["stars"]))]
    types = h.get("types") or []
    if len(types) == 1:
        a += ["--hotel-types", types[0]]
    if h.get("price_max"):
        a += ["--max-price", str(int(h["price_max"]))]
    return a


# ---------------------------------------------------------------- 过滤与选择
def pareto(legs, key_price, key_time, limit=3):
    """保留价格-时间的帕累托前沿（价格低 / 时间好），最多 limit 条；同价同时刻去重。"""
    legs = sorted(legs, key=lambda x: (key_price(x), key_time(x)))
    out, best_t = [], None
    for x in legs:
        t = key_time(x)
        if best_t is None or t < best_t:
            out.append(x)
            best_t = t
        if len(out) >= limit:
            break
    return out


class Quoter:
    def __init__(self, req, client, cfg):
        self.req, self.c, self.cfg = req, client, cfg
        self.geo = Geo(req, cfg)
        self.tr = req["transport"]
        self.seats = set(self.tr.get("train_seat") or ["二等座"])
        self.brands = load_config("chain_brands")["brands"]
        self.warnings = []
        self.null_days = {}     # (出发, 到达) -> 查询无结果的日期

    def _items(self, args):
        data = self.c.call(args)
        return (data or {}).get("itemList") or []

    def _flights(self, origin, dest, day, hour_start=None, arr_hour_end=None):
        data = self.c.call(flight_args(origin, dest, day, self.req, hour_start, arr_hour_end))
        if data is None and not self.c.stopped:
            # 实测 data:null 是「该日该线路无结果」（如硕放→三亚并非每天有航班，三亚→上海个别日期也会空），不能推断整条线路无航班
            self.null_days.setdefault((origin, dest), set()).add(str(day))
        out = []
        for it in (data or {}).get("itemList") or []:
            j = parse_journey(it, "flight")
            if not j:
                continue
            if not self.tr["transfer_ok"] and not j["direct"]:
                continue
            if any(l > self.tr["max_layover_min"] or l < 30 for l in j["layovers"]):
                continue
            out.append(j)
        return out

    def _trains(self, origin, dest, day, hour_start):
        out = []
        for it in self._items(train_args(origin, dest, day, hour_start)):
            j = parse_journey(it, "train")
            if not j or not j["direct"]:
                continue
            if any(s["seat"] not in self.seats for s in j["segments"]):
                continue
            dur = (j["segments"][-1]["arr"] - j["segments"][0]["dep"]).total_seconds() / 60
            if dur > self.tr["max_train_min"]:
                continue
            out.append(j)
        return out

    def _flags(self, j, legs_extra=()):
        fl = []
        if is_redeye(j, self.cfg):
            fl.append("红眼")
        arr = j["segments"][-1]["arr"]
        if arr.hour >= 23 or arr.date() > j["segments"][0]["dep"].date():
            fl.append("深夜落地")
        for l in j["layovers"]:
            fl.append(f"中转等待{l // 60}小时{l % 60}分" if l % 60 else f"中转等待{l // 60}小时")
        fl.extend(legs_extra)
        return fl

    # --- 去程
    def outbound(self, cand, origin):
        leave_home = parse_dt(cand["out"]["leave_home_after"])
        day = d(cand["out_date"])
        dest, home = cand["dest"], self.req["home_city"]
        buf = self.cfg["buffer_min"]
        legs = []
        evening = leave_home.hour >= 12
        if origin["kind"] == "home":
            hs = (leave_home + timedelta(minutes=60 + buf["air"])).hour if evening else None
            for f in self._flights(home, dest, day, hs):
                if not self.tr["redeye_out"] and is_redeye(f, self.cfg):
                    continue
                ap = f["segments"][0]["from"]
                cm, est = self.geo.commute(ap, "air")
                if f["segments"][0]["dep"] < leave_home + timedelta(minutes=cm + buf["air"]):
                    continue
                notes = ["通勤为估算"] if est else []
                legs.append({"flight": f, "train": None, "price": f["price"],
                             "leave_home": f["segments"][0]["dep"] - timedelta(minutes=cm + buf["air"]),
                             "dest_arrive": f["segments"][-1]["arr"], "flags": self._flags(f, notes)})
        else:
            hub = origin["city"]
            cm_t, est_t = self.geo.commute(None, "train")
            # 主站通勤：取用户填的第一个火车站
            tstations = [s for s in self.req["stations"] if s["type"] == "train"]
            if tstations:
                cm_t, est_t = tstations[0]["commute_min"], False
            earliest_train = leave_home + timedelta(minutes=cm_t + buf["train"])
            hs = (earliest_train + timedelta(minutes=30 + 40 + buf["air"])).hour if evening else None
            flights = [f for f in self._flights(hub, dest, day, hs)
                       if self.tr["redeye_out"] or not is_redeye(f, self.cfg)]
            if not flights:
                return []
            # 火车按航班倒推时段查询：只为最便宜的 2 个航班时段各查 1 次，覆盖起飞前 1.5 小时左右的车次
            # 代表航班：最便宜的 + 最早可赶上（多玩）的，各倒推一个火车查询时段
            # 沪苏这类密集线路 1 小时内就有 10+ 趟车，接口只回前 10 条（约半小时），所以要瞄准「起飞前留足换乘后再往前 1 小时」
            def train_start(f):
                latest_arr = f["segments"][0]["dep"] - timedelta(minutes=40 + buf["air"])
                start = max(earliest_train, latest_arr - timedelta(minutes=60))
                return start if start.date() == day and start <= latest_arr else None
            feasible = [f for f in flights if train_start(f)]
            reps = []
            if feasible:
                reps = [min(feasible, key=lambda x: (x["price"], x["segments"][-1]["arr"])),
                        min(feasible, key=lambda x: (x["segments"][-1]["arr"], x["price"]))]
            hours = []
            for f in reps:
                h = train_start(f).hour
                if h not in hours:
                    hours.append(h)
            trains = []
            for h in hours:
                trains += self._trains(home, hub, day, h)
            for f in flights:
                ok = []
                for t in trains:
                    if t["segments"][0]["dep"] < earliest_train:
                        continue
                    tf = self.geo.station_to_airport(t["segments"][-1]["to"], f["segments"][0]["from"])
                    if t["segments"][-1]["arr"] + timedelta(minutes=tf + buf["air"]) > f["segments"][0]["dep"]:
                        continue
                    ok.append((t, tf))
                if not ok:
                    continue
                known = [x for x in ok if self.geo.known_transfer(x[0]["segments"][-1]["to"], f["segments"][0]["from"]) is not None]
                ok = known or ok
                # 价差 15 元内优先最晚出发（少在机场干等）
                pmin = min(t["price"] for t, _ in ok)
                t, tf = max([x for x in ok if x[0]["price"] <= pmin + 15], key=lambda x: (x[0]["segments"][0]["dep"], -x[0]["price"]))
                notes = ["火车余票需自行确认", f"站到机场换乘约{tf}分钟(估算)"] + (["通勤为估算"] if est_t else [])
                legs.append({"flight": f, "train": t, "price": f["price"] + t["price"],
                             "leave_home": t["segments"][0]["dep"] - timedelta(minutes=cm_t + buf["train"]),
                             "dest_arrive": f["segments"][-1]["arr"], "flags": self._flags(f, notes)})
        return pareto(legs, lambda x: x["price"], lambda x: x["dest_arrive"])

    # --- 回程
    def inbound(self, cand, origin):
        deadline = parse_dt(cand["ret"]["arrive_home_by"])
        day = d(cand["ret_date"])
        dest, home = cand["dest"], self.req["home_city"]
        buf, lv = self.cfg["buffer_min"], self.cfg["leave_before_dep_min"]
        legs = []
        def arr_end(margin_min):
            t = deadline - timedelta(minutes=margin_min)
            return t.hour if t.date() == day and t.hour >= 6 else None
        if origin["kind"] == "home":
            for f in self._flights(dest, home, day, arr_hour_end=arr_end(min(s["commute_min"] for s in self.req["stations"] if s["type"] == "air") if any(s["type"] == "air" for s in self.req["stations"]) else self.cfg["default_commute_min"]["air"])):
                if not self.tr["redeye_back"] and is_redeye(f, self.cfg):
                    continue
                ap = f["segments"][-1]["to"]
                cm, est = self.geo.commute(ap, "air")
                home_at = f["segments"][-1]["arr"] + timedelta(minutes=cm)
                if home_at > deadline:
                    continue
                legs.append({"flight": f, "train": None, "price": f["price"], "home_arrive": home_at,
                             "dest_leave": f["segments"][0]["dep"] - timedelta(minutes=lv["air"]),
                             "flags": self._flags(f, ["通勤为估算"] if est else [])})
        else:
            hub = origin["city"]
            tstations = [s for s in self.req["stations"] if s["type"] == "train"]
            cm_t = tstations[0]["commute_min"] if tstations else self.cfg["default_commute_min"]["train"]
            flights = [f for f in self._flights(dest, hub, day, arr_hour_end=arr_end(cm_t + 20 + buf["train"] + 30))
                       if self.tr["redeye_back"] or not is_redeye(f, self.cfg)]
            # 只为最便宜的 2 个到达时段查火车，控制调用量
            def train_start(f):
                st = f["segments"][-1]["arr"] + timedelta(minutes=20 + buf["train"])
                if st.minute >= 20:  # 接口按整点查且只回前 10 条，向后取整到下一个整点更容易命中可衔接车次
                    st = st.replace(minute=0) + timedelta(hours=1)
                # 至少留出火车行程和到家通勤
                if st + timedelta(minutes=45 + cm_t) > deadline or st.date() != day:
                    return None
                return st
            feasible = [f for f in flights if train_start(f)]
            reps = []
            if feasible:
                reps = [min(feasible, key=lambda x: (x["price"], -x["segments"][0]["dep"].timestamp())),
                        max(feasible, key=lambda x: (x["segments"][0]["dep"], -x["price"]))]
            hours = []
            for f in reps:
                h = train_start(f).hour
                if h not in hours:
                    hours.append(h)
            trains = []
            for h in hours:
                trains += self._trains(hub, home, day, h)
            for f in flights:
                ok = []
                for t in trains:
                    tf = self.geo.station_to_airport(t["segments"][0]["from"], f["segments"][-1]["to"])
                    if t["segments"][0]["dep"] < f["segments"][-1]["arr"] + timedelta(minutes=tf + buf["train"]):
                        continue
                    if t["segments"][-1]["arr"] + timedelta(minutes=cm_t) > deadline:
                        continue
                    ok.append((t, tf))
                if not ok:
                    continue
                known = [x for x in ok if self.geo.known_transfer(x[0]["segments"][0]["from"], f["segments"][-1]["to"]) is not None]
                ok = known or ok
                pmin = min(t["price"] for t, _ in ok)
                t, tf = min([x for x in ok if x[0]["price"] <= pmin + 15], key=lambda x: (x[0]["segments"][0]["dep"], x[0]["price"]))
                legs.append({"flight": f, "train": t, "price": f["price"] + t["price"],
                             "home_arrive": t["segments"][-1]["arr"] + timedelta(minutes=cm_t),
                             "dest_leave": f["segments"][0]["dep"] - timedelta(minutes=lv["air"]),
                             "flags": self._flags(f, ["火车余票需自行确认", f"机场到站换乘约{tf}分钟(估算)"])})
        return pareto(legs, lambda x: x["price"], lambda x: -x["dest_leave"].timestamp())

    # --- 酒店
    def hotels(self, cand):
        h = self.req["hotel"]
        items = self._items(hotel_args(cand["dest"], cand["out_date"], cand["ret_date"], self.req))
        out = []
        for it in items:
            p = _f(it.get("price"))
            if p is None:
                continue
            star = STAR_MAP.get((it.get("star") or "").strip())
            name = it.get("name") or ""
            if h.get("mode") == "specific":
                if h["specific_name"] not in name and name not in h["specific_name"]:
                    continue
            else:
                if h.get("stars") and star is not None and star not in h["stars"]:
                    continue
                if h.get("price_min") and p < h["price_min"]:
                    continue
                if h.get("price_max") and p > h["price_max"]:
                    continue
                chain = h.get("chain") or "any"
                if chain == "chain_only" and not (it.get("brandName") or any(b in name for b in self.brands)):
                    continue
                if chain.startswith("brand:") and chain.split(":", 1)[1] not in name:
                    continue
            out.append({"shId": it.get("shId"), "name": name, "star": (it.get("star") or "").strip(),
                        "price_per_night": p, "address": it.get("address"), "near": it.get("interestsPoi"),
                        "pic": it.get("mainPic"), "url": it.get("detailUrl")})
        area = (h.get("area") or "").strip()
        if area and not re.search(r"(区|县|市)$", area) and h.get("mode") != "specific":
            hit = [x for x in out if area in (x["name"] or "") + (x["address"] or "") + (x["near"] or "")]
            if hit:
                out = hit
            elif out:
                msg = f"「{area}」范围内没有符合条件的酒店，已放宽到{cand['dest']}全市"
                if msg not in self.warnings:
                    self.warnings.append(msg)
        out.sort(key=lambda x: (x["price_per_night"], x["name"]))
        return out


# ---------------------------------------------------------------- 预算与截断
def needed_queries(cand, req):
    """估算一个候选需要的查询键（用于预算截断，近似即可）。"""
    keys = set()
    home = req["home_city"]
    for o in cand["origins"]:
        hub = home if o["kind"] == "home" else o["city"]
        evening = cand["out"]["mode"] == "after_work"
        keys.add(("f", hub, cand["dest"], cand["out_date"], evening))
        keys.add(("f", cand["dest"], hub, cand["ret_date"]))
        if o["kind"] == "nearby":
            keys.add(("t_out", hub, cand["out_date"], cand["out"]["mode"]))
            keys.add(("t_ret", hub, cand["ret_date"]))
    keys.add(("h", cand["dest"], cand["out_date"], cand["ret_date"]))
    return keys


def price_hints(client, req, cands):
    """对每个目的地各做 1 次去程、1 次回程的日期区间查询，得到部分日期的最低价线索。
    常住城市没有航线时改用第一个邻近出发城市。"""
    hints = {}
    hubs = [req["home_city"]] + (req["transport"]["nearby_cities"] if req["transport"]["nearby_origin_ok"] else [])
    for dest in req["destinations"]:
        ds = [c for c in cands if c["dest"] == dest]
        if not ds:
            continue
        for direction, k in (("out", "out_date"), ("ret", "ret_date")):
            days = sorted({c[k] for c in ds})
            for hub in hubs:
                o, de = (hub, dest) if direction == "out" else (dest, hub)
                a = ["search-flight", "--origin", o, "--destination", de,
                     "--dep-date-start", days[0], "--dep-date-end", days[-1], "--sort-type", "3"]
                if not req["transport"]["transfer_ok"]:
                    a += ["--journey-type", "1"]
                data = client.call(a)
                items = (data or {}).get("itemList") or []
                for it in items:
                    j = parse_journey(it, "flight")
                    if j:
                        key = (direction, dest, str(j["segments"][0]["dep"].date()))
                        hints[key] = min(hints.get(key, 1e9), j["price"])
                if items:
                    break
    return hints


def order_candidates(cands, req, client):
    """按目标给候选排查询顺序；候选多时先用日期区间查询拿低价线索。"""
    goal = req["goal"]["priority"]
    total = set()
    for c in cands:
        total |= needed_queries(c, req)
    info = {"total_candidates": len(cands), "estimated_queries": len(total), "hint_queries": 0}
    hints = {}
    if len(total) > client.max_calls - client.live_calls:
        before = client.live_calls
        hints = price_hints(client, req, cands)
        info["hint_queries"] = client.live_calls - before

    def est_price(c):
        o = hints.get(("out", c["dest"], c["out_date"]))
        r = hints.get(("ret", c["dest"], c["ret_date"]))
        known = [x for x in (o, r) if x is not None]
        return sum(known) + 900 * (2 - len(known))

    if goal == "play":
        order = sorted(cands, key=lambda c: (-c["nights"], c["leave_days"], est_price(c), c["id"]))
    elif goal == "save":
        order = sorted(cands, key=lambda c: (c["leave_days"], est_price(c), c["nights"], c["id"]))
    else:
        order = sorted(cands, key=lambda c: (c["leave_days"], est_price(c) / max(c["nights"], 1), c["id"]))
    info["price_hints"] = {f"{k[0]}|{k[1]}|{k[2]}": v for k, v in sorted(hints.items())}
    return order, info


# ---------------------------------------------------------------- 主流程
def fetch(req, cands, client):
    """按顺序逐个候选查价，真实调用数用满预算为止（缓存共享，越往后越便宜）。
    同类模式连续失败 2 次后跳过其余同类候选（例如「下班后出发」都赶不上航班）。"""
    cfg = load_config("defaults")
    q = Quoter(req, client, cfg)
    order, info = order_candidates(cands, req, client)
    results, not_queried, fails, pattern_skipped = [], [], {}, []
    for c in order:
        if client.stopped or client.live_calls >= client.max_calls:
            not_queried.append(c["id"])
            continue
        pat_out, pat_ret = ("out", c["out"]["mode"]), ("ret", c["ret"]["mode"])
        if fails.get(pat_out, 0) >= 2 or fails.get(pat_ret, 0) >= 2:
            pattern_skipped.append(c["id"])
            continue
        skipped0 = client.skipped
        opts, any_out = [], False
        for o in c["origins"]:
            outs = q.outbound(c, o)
            any_out = any_out or bool(outs)
            rets = q.inbound(c, o) if outs else []
            if outs and rets:
                opts.append({"origin": o, "out": [_leg_out(x) for x in outs], "ret": [_leg_ret(x) for x in rets]})
        hotels = q.hotels(c) if opts else []
        status = "ok" if opts and hotels else ("no_out" if not any_out else ("no_ret" if not opts else "no_hotel"))
        if status != "ok" and client.skipped > skipped0:
            status = "budget"  # 因预算/停止未查全，不能判定为不可行
        if status == "no_out":
            fails[pat_out] = fails.get(pat_out, 0) + 1
        if status == "no_ret":
            fails[pat_ret] = fails.get(pat_ret, 0) + 1
        results.append({**c, "status": status, "options": opts, "hotel": hotels[0] if hotels else None,
                        "hotel_alts": hotels[1:3]})
    info["selected"] = len(results)
    info["truncated"] = bool(not_queried or pattern_skipped)
    info["pattern_skipped"] = len(pattern_skipped)
    for (o, de), days in sorted(q.null_days.items()):
        q.warnings.append(f"{o}→{de} 在 {', '.join(sorted(days))} 没有查到航班（该日无航班或接口无数据）")
    rec = sorted(client.recorded)
    return {"meta": {**client.summary(), **info}, "errors": client.errors,
            "recorded_at": {"first": rec[0], "last": rec[-1]} if rec else None,
            "system_messages": sorted(client.system_messages),
            "warnings": q.warnings, "quotes": results, "not_queried": not_queried,
            "pattern_skipped": pattern_skipped}


def _leg_out(x):
    return {"price": x["price"], "flight": leg_json(x["flight"]), "train": leg_json(x["train"]) if x["train"] else None,
            "leave_home": fmt_dt(x["leave_home"]), "dest_arrive": fmt_dt(x["dest_arrive"]), "flags": x["flags"]}


def _leg_ret(x):
    return {"price": x["price"], "flight": leg_json(x["flight"]), "train": leg_json(x["train"]) if x["train"] else None,
            "dest_leave": fmt_dt(x["dest_leave"]), "home_arrive": fmt_dt(x["home_arrive"]), "flags": x["flags"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--request", required=True)
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache-dir", default="cache")
    ap.add_argument("--replay", action="store_true")
    a = ap.parse_args()
    req = read_json(a.request)
    req = req.get("request", req)
    cands = read_json(a.candidates)["candidates"]
    cfg = load_config("defaults")
    client = FlyaiClient(a.cache_dir, max_calls=cfg["limits"]["max_calls"],
                         min_interval_s=cfg["limits"]["min_interval_s"], replay=a.replay)
    res = fetch(req, cands, client)
    write_json(a.out, res)
    m = res["meta"]
    ok = sum(1 for x in res["quotes"] if x["status"] == "ok")
    print(f"查价完成：候选 {m['total_candidates']}，实际查询 {m['selected']}，有完整报价 {ok}；"
          f"实时调用 {m['live_calls']} 次，缓存命中 {m['cache_hits']} 次，跳过 {m['skipped']} 次"
          f"{'，已截断' if m['truncated'] else ''}{'，错误：' + ','.join(e['code'] for e in res['errors']) if res['errors'] else ''}")


if __name__ == "__main__":
    main()
