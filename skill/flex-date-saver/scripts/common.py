"""flex-date-saver 公共工具：配置加载、时间换算、节假日日历。兼容 Python 3.8+。"""
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = SKILL_DIR / "config"


def load_config(name):
    return json.loads((CONFIG_DIR / f"{name}.json").read_text(encoding="utf-8"))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8")


def today():
    """可用环境变量 FDS_TODAY=YYYY-MM-DD 固定「今天」，保证测试可复现。"""
    v = os.environ.get("FDS_TODAY")
    return date.fromisoformat(v) if v else date.today()


def d(s):
    return date.fromisoformat(s) if isinstance(s, str) else s


def hm_to_min(s):
    """'18:30' -> 1110；'24:00' -> 1440。"""
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def at(day, hm):
    """某天的 HH:MM 时刻（支持 24:00 = 次日 0 点）。"""
    return datetime.combine(d(day), datetime.min.time()) + timedelta(minutes=hm_to_min(hm))


def parse_dt(s):
    """接口时间 '2026-11-20 06:40:00' 或 ISO 'YYYY-MM-DDTHH:MM'。"""
    s = s.replace("T", " ")
    fmt = "%Y-%m-%d %H:%M:%S" if s.count(":") == 2 else "%Y-%m-%d %H:%M"
    return datetime.strptime(s, fmt)


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def daterange(start, end):
    """含首尾。"""
    cur, end = d(start), d(end)
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


WEEKDAY_CN = "一二三四五六日"


def weekday_cn(day):
    return "周" + WEEKDAY_CN[d(day).weekday()]


class HolidayCalendar:
    def __init__(self, cfg=None):
        cfg = cfg or load_config("holidays")
        self.cfg = cfg
        self.holidays = {}  # date -> (name, estimated)
        for h in cfg.get("holidays", []):
            for day in daterange(h["start"], h["end"]):
                self.holidays[day] = (h["name"], bool(h.get("estimated")))
        self.makeup = {d(x["date"] if isinstance(x, dict) else x) for x in cfg.get("makeup_workdays", [])}
        self.makeup_estimated = bool(cfg.get("makeup_estimated"))
        cov = cfg.get("coverage", {})
        self.cov_start = d(cov["start"]) if cov.get("start") else None
        self.cov_end = d(cov["end"]) if cov.get("end") else None

    def is_workday(self, day):
        day = d(day)
        if day in self.makeup:
            return True
        if day in self.holidays:
            return False
        return day.weekday() < 5

    def is_estimated(self, day):
        """该日的工作日/假日判断是否依赖预估数据。"""
        day = d(day)
        if day in self.holidays:
            return self.holidays[day][1]
        if day in self.makeup:
            return self.makeup_estimated
        # 调休补班通常落在预估假期前后 10 天内的周末，这些周末是否上班也属于预估
        if self.makeup_estimated and day.weekday() >= 5:
            return any(est and abs((h - day).days) <= 10 for h, (_, est) in self.holidays.items())
        return False

    def label(self, day):
        day = d(day)
        if day in self.holidays:
            name, est = self.holidays[day]
            return f"{name}{'(预估)' if est else ''}"
        if day in self.makeup:
            return "调休补班"
        return "周末" if day.weekday() >= 5 else "工作日"

    def covered(self, day):
        day = d(day)
        return (self.cov_start is None or day >= self.cov_start) and (self.cov_end is None or day <= self.cov_end)
