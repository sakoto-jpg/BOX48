from fastapi import Response
from icalendar import Calendar, Event
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import requests
import time
import re
import json
import os
import sqlite3
import hashlib
import yaml

try:
    from . import group_config
except ImportError:
    import group_config
from urllib.parse import urlparse, parse_qs

app = FastAPI()
templates = Jinja2Templates(directory="app/templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

cache = {"data": [], "timestamp": 0}
CACHE_TTL_DEFAULT = 3600 * 6      # 票务缓存刷新间隔默认 6 小时（可在设置页调整）
RETRY_COOLDOWN = 600   # 抓取失败后 10 分钟再重试（不占满 24 小时缓存）

_ticket_ttl_cache = {"mtime": None, "ttl": CACHE_TTL_DEFAULT}


def ticket_cache_ttl() -> int:
    """从 roster.yaml 读取票务刷新间隔（refresh.tickets.hours），默认 6 小时。"""
    try:
        mtime = os.path.getmtime(ROSTER_PATH)
    except OSError:
        return CACHE_TTL_DEFAULT
    if _ticket_ttl_cache["mtime"] == mtime:
        return _ticket_ttl_cache["ttl"]
    ttl = CACHE_TTL_DEFAULT
    try:
        with open(ROSTER_PATH, encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}
        hours = ((config.get("refresh") or {}).get("tickets") or {}).get("hours")
        if hours:
            ttl = max(1, min(168, int(hours))) * 3600
    except Exception as exc:                      # noqa: BLE001
        print(f"⚠️ 读取票务刷新间隔失败，用默认 {CACHE_TTL_DEFAULT // 3600}h: {exc}",
              flush=True)
    _ticket_ttl_cache.update(mtime=mtime, ttl=ttl)
    print(f"🕒 票务缓存刷新间隔: {ttl // 3600} 小时", flush=True)
    return ttl

# 排期数据改用 SQLite（与下载器共用 /config/snh48.db）；
# 原 data.json 仅作为首次启动时的种子数据。
SCHEDULE_DB = os.environ.get("SCHEDULE_DB", os.path.join("/config", "snh48.db"))
LEGACY_JSON = os.path.join(os.path.dirname(__file__), "data.json")

try:
    from .schedule_store import ScheduleStore
except ImportError:
    from schedule_store import ScheduleStore

schedule_store = ScheduleStore(SCHEDULE_DB, legacy_json=LEGACY_JSON)

API_BASE = "https://api.snh48.com"
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.snh48.com/ticket.html",
    "Origin": "https://www.snh48.com",
}

# 官方接口的 s_tag → 标题里补的中文队名（阶段 5：补齐全部分团）
TEAM_CN = {
    # SNH48
    "s": "S队", "n": "N队", "h": "H队", "x": "X队", "ybs": "YBS", "def": "",
    # GNZ48
    "g": "G队", "niii": "NIII队", "z": "Z队", "gnz48预备生": "预备生",
    # BEJ48
    "b": "B队", "e": "E队", "j": "J队",
    # CKG48 / CGT48
    "c": "C队", "k": "K队", "ckg": "CKG",
    "cii": "CII队", "gii": "GII队",
    # 拼盘场次不加标签
    "union": "",
}


_TRAINEE_TEAM_NAME = os.environ.get("TRAINEE_TEAM_NAME", "预备生")
ROSTER_PATH = os.environ.get("ROSTER_PATH", "/config/roster.yaml")
_trainee_keyword_cache = {"mtime": None, "keywords": []}


def _load_trainee_keywords() -> list:
    """从花名册读取预备生队伍的关键词（与下载器共用 /config/roster.yaml）。"""
    try:
        mtime = os.path.getmtime(ROSTER_PATH)
    except OSError:
        return []
    if _trainee_keyword_cache["mtime"] == mtime:
        return _trainee_keyword_cache["keywords"]
    keywords = []
    try:
        with open(ROSTER_PATH, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        for team in data.get("teams", []):
            if team.get("name") == _TRAINEE_TEAM_NAME:
                keywords = [str(k) for k in team.get("keywords", []) if str(k).strip()]
                break
    except Exception as exc:
        print(f"⚠️ [Schedule] 读取预备生关键词失败: {exc}", flush=True)
    _trainee_keyword_cache.update(mtime=mtime, keywords=keywords)
    return keywords


def _clean_keyword(text: str) -> str:
    return re.sub(r"[《》\s]", "", text or "")


def _lookup_def_label(api_date: str, bz: str) -> str:
    """官方接口对所有非固定队伍场次返回 team=def（含新生公演与拼盘公演）。

    判定顺序：
    1) 同一天存在标题含“新生/预备生”的官方投稿、且剧目名匹配；
    2) 剧目名命中花名册「预备生」队伍的关键词（配置驱动）。
    两者都不满足则不加队伍标签，避免拼盘公演被误标。
    """
    day = (api_date or "")[:10].replace("-", "")
    if day and os.path.exists(SCHEDULE_DB):
        plays = re.findall(r"《([^》]+)》", bz or "")
        try:
            conn = sqlite3.connect(SCHEDULE_DB, timeout=5)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT title FROM uploads WHERE title LIKE ?", (f"%{day}%",)
            ).fetchall()
            conn.close()
        except Exception:
            rows = []
        for row in rows:
            title = row["title"] or ""
            if "新生" not in title and "预备生" not in title:
                continue
            if plays and not any(play in title for play in plays):
                continue
            return "预备生"

    # 花名册关键词兜底：覆盖“录播尚未上传”的未来场次
    text = _clean_keyword(bz)
    for keyword in _load_trainee_keywords():
        if _clean_keyword(keyword) and _clean_keyword(keyword) in text:
            return "预备生"
    return ""


def load_local_data():
    return schedule_store.load()


def save_local_data(data):
    schedule_store.save(data)


def fetch_from_api(month_str, group_code: str = "snh", gid: int = 1):
    """从官方 API 获取指定分团、指定月份的票务/公演数据."""
    try:
        resp = requests.post(
            f"{API_BASE}/getticketinfo.php?gid={gid}",
            headers=API_HEADERS,
            data={"act": "choose", "date": month_str, "team": "ALL"},
            timeout=15,
        )
    except Exception as exc:
        print(f"⚠️ 排期接口请求失败 {month_str}: {exc}", flush=True)
        return [], False
    if resp.status_code != 200:
        return [], False

    text = resp.text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], False

    if data.get("status") == "202":   # 官方返回 “no data”，请求本身是成功的
        return [], True
    if data.get("status") != "200":
        return [], False

    results = []
    for item in data.get("desc", []):
        api_date = item.get("date", "")  # "2026-05-31 18:30"
        api_title = item.get("title", "").replace(" 剧场公演", "")
        team_code = item.get("team", "def")
        theatre = item.get("theatre_id_name", "SNH48星梦剧院")
        bz = item.get("bz", "")
        btn2 = item.get("btn2", "")
        is_real = item.get("is_real", "0")

        # 解析日期时间: "2026-05-31 18:30"
        date_match = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}:\d{2})", api_date)
        if not date_match:
            continue

        month_cn = str(int(date_match.group(2)))
        day_cn = str(int(date_match.group(3)))
        date_str = f"{month_cn}月{day_cn}日"
        show_time = date_match.group(4)

        # 生成 ID：优先从购票链接提取，否则用日期+队伍
        ticket_id = ""
        ticket_link = ""
        if btn2:
            ticket_id = btn2.split("/item/")[-1].split("?")[0]
            ticket_link = f"https://shop.48.cn/tickets/item/{ticket_id}"
        if not ticket_id:
            # 临时 ID 用「日期+队伍+剧目名」生成，避免官方调整场次时间时产生重复记录
            bz_key = re.sub(r"\s+", "", bz or api_title)
            digest = hashlib.md5(
                f"{date_match.group(1)}{date_match.group(2)}{date_match.group(3)}"
                f"|{team_code}|{bz_key}".encode("utf-8")
            ).hexdigest()[:8]
            ticket_id = (
                f"api-{date_match.group(1)}{date_match.group(2)}{date_match.group(3)}"
                f"-{team_code}-{digest}"
            )

        # def = 非固定队伍场次（新生公演、拼盘公演等）：按同日官方投稿判断是否为预备生
        if team_code == "def":
            team_cn = _lookup_def_label(api_date, bz)
        else:
            team_cn = TEAM_CN.get(team_code, "")
        if team_cn:
            full_title = f"{date_str} {theatre} {team_cn} {bz}".strip()
        else:
            full_title = f"{date_str} {theatre} {bz}".strip()
        # 清理多余空格
        full_title = re.sub(r"\s+", " ", full_title)

        results.append({
            "id": ticket_id,
            "date": date_str,
            "show_time": show_time,
            "title": full_title,
            "link": ticket_link,
            "team": team_code,
            "is_real": is_real,
            "api_date": api_date,
            "fetch_time": datetime.now().strftime("%Y-%m-%d"),
            "group_code": group_code,
        })

    return results, True


def sort_key(item):
    """从中文日期生成可排序的元组，如 '5月31日' -> (5, 31)."""
    m = re.search(r"(\d+)月(\d+)日", item["date"])
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return (0, 0)


def merge_api_items(local_data, api_items):
    """将 API 数据合并到本地数据中."""
    time_index = {}
    for tid, entry in local_data.items():
        # 去重键必须带分团：不同分团同一天同一时间开演是常态，
        # 否则后一个分团会把前一个分团的场次覆盖掉
        key = (entry.get("group_code") or "snh", entry["date"], entry.get("show_time", ""))
        time_index[key] = tid

    for item in api_items:
        entry = {
            k: v for k, v in item.items()
            if k not in ("is_real", "api_date")
        }

        tid = entry["id"]
        time_key = (entry.get("group_code") or "snh", entry["date"], entry["show_time"])

        if tid in local_data:
            local_data[tid].update(entry)
        elif time_key in time_index:
            existing_tid = time_index[time_key]
            if entry["link"]:
                local_data[existing_tid]["link"] = entry["link"]
            local_data[existing_tid]["title"] = entry["title"]
            local_data[existing_tid]["show_time"] = entry["show_time"]
            local_data[existing_tid]["team"] = entry.get("team", "")
            local_data[existing_tid]["fetch_time"] = entry["fetch_time"]
        else:
            local_data[tid] = entry
            time_index[time_key] = tid


def fetch_snh48_tickets():
    local_data = load_local_data()

    try:
        now = datetime.now()
        current_month = now.strftime("%Y-%m")
        next_month = (now.replace(day=28) + timedelta(days=4)).strftime("%Y-%m")

        # 拉取当月和下月数据
        ok_keys = set()
        api_keys = set()
        for group in group_config.enabled_groups():
            group_code = group.get("code", "snh")
            gid = int(group.get("api_gid", 1))
            for month in [current_month, next_month]:
                api_items, ok = fetch_from_api(month, group_code=group_code, gid=gid)
                if ok:
                    ok_keys.add((group_code, int(month.split("-")[1])))
                api_keys.update(
                    (group_code, item["date"], item["show_time"]) for item in api_items
                )
                merge_api_items(local_data, api_items)

        # 清理当月/下月已失效的临时记录（例如官方改了场次时间导致旧记录残留）。
        # 注意按「日期+场次时间」判断，而不是按 ID：合并逻辑对同日期同时间的
        # 场次会沿用旧 ID，按 ID 判断会把刚更新的记录误删。
        stale_ids = []
        for tid, entry in list(local_data.items()):
            if not tid.startswith("api-"):
                continue
            if str(entry.get("fetch_time", ""))[:4] != str(now.year):
                continue
            month_match = re.search(r"(\d+)月", entry.get("date", ""))
            group_code = entry.get("group_code") or "snh"
            key = (group_code, entry.get("date", ""), entry.get("show_time", ""))
            if (month_match and (group_code, int(month_match.group(1))) in ok_keys
                    and key not in api_keys):
                stale_ids.append(tid)
                local_data.pop(tid, None)
        if stale_ids:
            schedule_store.delete(stale_ids)
            print(f"🧹 清理失效场次 {len(stale_ids)} 条: {', '.join(stale_ids)}", flush=True)

        save_local_data(local_data)
        return sorted(local_data.values(), key=sort_key, reverse=True), bool(ok_keys)

    except Exception as e:
        print(f"Error fetching data: {e}")
        return sorted(local_data.values(), key=lambda x: x["date"], reverse=True), False


def enrich_status(items):
    """计算每场公演的展示状态：已结束 / 暂未售票 / 可购票."""
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    for item in items:
        # 解析日期时间
        date_match = re.search(r"(\d+)月(\d+)日", item["date"])
        if not date_match:
            item["status"] = "unknown"
            continue

        month = int(date_match.group(1))
        day = int(date_match.group(2))
        year = now.year
        if month < now.month - 2:
            year += 1

        hour, minute = 0, 0
        st = item.get("show_time", "")
        if st and ":" in st and st != "未知时间":
            try:
                parts = st.split(":")
                hour = int(parts[0])
                minute = int(parts[1])
            except ValueError:
                pass

        try:
            show_start = datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            item["status"] = "unknown"
            continue

        if now > show_start:
            item["status"] = "past"
        elif item.get("link"):
            item["status"] = "on_sale"
        else:
            item["status"] = "no_ticket"


@app.get("/", response_class=HTMLResponse)
async def read_item(request: Request):
    current_time = time.time()
    ttl = ticket_cache_ttl()
    if current_time - cache["timestamp"] > ttl:
        data, ok = fetch_snh48_tickets()
        cache["data"] = data
        cache["timestamp"] = current_time if ok else current_time - ttl + RETRY_COOLDOWN
    else:
        if not cache["data"]:
            local_dict = load_local_data()
            cache["data"] = sorted(local_dict.values(), key=lambda x: x["date"], reverse=True)

    enrich_status(cache["data"])

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "performances": cache["data"],
            "update_time": datetime.fromtimestamp(cache["timestamp"]).strftime("%Y-%m-%d %H:%M:%S"),
        },
    )


@app.get("/api/tickets")
async def get_tickets_api():
    current_time = time.time()
    ttl = ticket_cache_ttl()
    if current_time - cache["timestamp"] > ttl:
        data, ok = fetch_snh48_tickets()
        cache["data"] = data
        cache["timestamp"] = current_time if ok else current_time - ttl + RETRY_COOLDOWN
    enrich_status(cache["data"])
    return {"status": "success", "data": cache["data"], "updated_at": cache["timestamp"]}


@app.get("/api/calendar.ics")
async def get_calendar_ics():
    current_time = time.time()
    ttl = ticket_cache_ttl()
    if current_time - cache["timestamp"] > ttl:
        data, ok = fetch_snh48_tickets()
        cache["data"] = data
        cache["timestamp"] = current_time if ok else current_time - ttl + RETRY_COOLDOWN

    cal = Calendar()
    cal.add("prodid", "-//SNH48 Calendar API//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "SNH48 星梦剧院公演")
    cal.add("x-wr-timezone", "Asia/Shanghai")

    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    for item in cache["data"]:
        event = Event()
        event.add("summary", item["title"])

        date_match = re.search(r"(\d+)月(\d+)日", item["date"])
        if not date_match:
            continue

        month = int(date_match.group(1))
        day = int(date_match.group(2))

        year = now.year
        if month < now.month - 2:
            year += 1

        hour, minute = 0, 0
        if item.get("show_time") and ":" in item["show_time"]:
            time_parts = item["show_time"].split(":")
            hour = int(time_parts[0])
            minute = int(time_parts[1])

        start_time = datetime(year, month, day, hour, minute, tzinfo=tz)
        end_time = start_time + timedelta(hours=2)

        event.add("dtstart", start_time)
        event.add("dtend", end_time)
        event.add("dtstamp", now)
        event.add("uid", f"snh48-ticket-{item['id']}@ha.local")
        if item.get("link"):
            event.add("url", item["link"])

        cal.add_component(event)

    return Response(content=cal.to_ical(), media_type="text/calendar")
