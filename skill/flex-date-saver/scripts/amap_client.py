"""高德 Web 服务调用（第一晚酒店的位置筛选用）。Key 取环境变量 AMAP_KEY；没有 Key 时所有函数返回 None，调用方跳过位置筛选。
结果缓存到 <cache_dir>/amap.json（1 天）。"""
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

KEY = os.environ.get("AMAP_KEY", "")
_lock = threading.Lock()


def haversine_km(a, b):
    (lng1, lat1), (lng2, lat2) = a, b
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl, dp = math.radians(lng2 - lng1), p2 - p1
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def loc_of(s):
    lng, lat = str(s).split(",")
    return float(lng), float(lat)


class Amap:
    def __init__(self, cache_dir):
        self.ok = bool(KEY)
        self.path = Path(cache_dir) / "amap.json"
        try:
            self.cache = json.loads(self.path.read_text()) if self.path.exists() else {}
        except Exception:
            self.cache = {}
        self.calls = 0

    def _get(self, path, **params):
        if not self.ok:
            return None
        q = urllib.parse.urlencode(sorted(params.items()), safe=",|")
        k = f"{path}?{q}"
        hit = self.cache.get(k)
        if hit and time.time() - hit[0] < 86400:
            return hit[1]
        d = None
        for attempt in range(4):
            wait = getattr(self, "_last", 0) + 0.35 - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                with urllib.request.urlopen(f"https://restapi.amap.com{path}?{q}&key={KEY}", timeout=12) as r:
                    d = json.loads(r.read().decode())
            except Exception:
                return None
            if "QPS" in str(d.get("info")) and attempt < 3:
                time.sleep(1.0 + attempt)
                continue
            break
        self.calls += 1
        if str(d.get("status")) != "1":
            return None
        with _lock:
            self.cache[k] = (time.time(), d)
            try:
                self.path.write_text(json.dumps(self.cache, ensure_ascii=False))
            except Exception:
                pass
        return d

    def place(self, keywords, city):
        """站点/地点 → {location, city, district}"""
        d = self._get("/v3/place/text", keywords=keywords, city=city, citylimit="true", offset=1)
        p = ((d or {}).get("pois") or [None])[0]
        if not p or not p.get("location"):
            return None
        return {"location": p["location"], "city": p.get("cityname") or city, "district": p.get("adname") or ""}

    def regeo(self, location):
        d = self._get("/v3/geocode/regeo", location=location)
        ac = ((d or {}).get("regeocode") or {}).get("addressComponent") or {}
        city = ac.get("city") if isinstance(ac.get("city"), str) and ac.get("city") else ac.get("province")
        if not city:
            return None
        return {"location": location, "city": city, "district": ac.get("district") if isinstance(ac.get("district"), str) else ""}

    def drive_min(self, origin, dest):
        d = self._get("/v5/direction/driving", origin=origin, destination=dest, show_fields="cost", strategy=32)
        p = (((d or {}).get("route") or {}).get("paths") or [{}])[0]
        sec = int((p.get("cost") or {}).get("duration") or 0)
        return max(3, round(sec / 60)) if sec else None
