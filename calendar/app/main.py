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
CACHE_TTL = 3600 * 24

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

TEAM_CN = {"s": "S队", "n": "N队", "h": "H队", "x": "X队", "ybs": "YBS", "def": "", "ckg": "CKG", "cii": "CII", "gii": "GII"}


def load_local_data():
    return schedule_store.load()


def save_local_data(data):
    schedule_store.save(data)


def fetch_from_api(month_str):
    """从官方 API 获取指定月份的票务/公演数据."""
    resp = requests.post(
        f"{API_BASE}/getticketinfo.php?gid=1",
        headers=API_HEADERS,
        data={"act": "choose", "date": month_str, "team": "ALL"},
        timeout=15,
    )
    if resp.status_code != 200:
        return []

    text = resp.text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []

    if data.get("status") != "200":
        return []

    results = []
    for item in data.get("desc", []):
        api_date = item.get("date", "")  # "2026-05-31 18:30"
        api_title = item.get("title", "").replace(" 剧场公演", "")
        team_code = item.get("team", "def")
        # SNH48 API 对 YBS 预备生/新生公演返回 team="def"，统一转为 ybs
        if team_code == "def":
            team_code = "ybs"
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
            ticket_id = f"api-{api_date.replace(' ', '-').replace(':', '')}-{team_code}"

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
        })

    return results


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
        key = (entry["date"], entry.get("show_time", ""))
        time_index[key] = tid

    for item in api_items:
        entry = {
            k: v for k, v in item.items()
            if k not in ("is_real", "api_date")
        }

        tid = entry["id"]
        time_key = (entry["date"], entry["show_time"])

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
        for month in [current_month, next_month]:
            api_items = fetch_from_api(month)
            merge_api_items(local_data, api_items)

        save_local_data(local_data)
        return sorted(local_data.values(), key=sort_key, reverse=True)

    except Exception as e:
        print(f"Error fetching data: {e}")
        return sorted(local_data.values(), key=lambda x: x["date"], reverse=True)


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
    if current_time - cache["timestamp"] > CACHE_TTL:
        cache["data"] = fetch_snh48_tickets()
        cache["timestamp"] = current_time
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
    if current_time - cache["timestamp"] > CACHE_TTL:
        cache["data"] = fetch_snh48_tickets()
        cache["timestamp"] = current_time
    enrich_status(cache["data"])
    return {"status": "success", "data": cache["data"], "updated_at": cache["timestamp"]}


@app.get("/api/calendar.ics")
async def get_calendar_ics():
    current_time = time.time()
    if current_time - cache["timestamp"] > CACHE_TTL:
        cache["data"] = fetch_snh48_tickets()
        cache["timestamp"] = current_time

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
