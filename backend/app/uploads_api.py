"""官方投稿目录查询接口。

数据来自 tools/sync_uploads.py 写入的 uploads / upload_queue / sync_state 表，
并与 downloads 表（已入库历史）关联，标记哪些投稿已经下载过。
"""

import os
import re
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Query

from team_utils import CANON_GROUP, CODE_TO_CANON, clean_team_from_title
from upload_store import UploadStore

router = APIRouter()

BV_RE = re.compile(r"BV[0-9A-Za-z]{10}")

# 快捷筛选分类：与 roster.yaml 的队伍关键词保持一致
# NII 需排除 NIII（"TEAM NIII" 里包含 "TEAM NII" 子串）
CATEGORY_CONDITIONS = {
    "nii": ("((u.title LIKE '%TEAM NII%' OR u.title LIKE '%N队%') "
            "AND (u.title NOT LIKE '%NIII%' OR REPLACE(u.title,'NIII','') LIKE '%NII%'))"),
    "x": "(u.title LIKE '%TEAM X%' OR u.title LIKE '%X队%')",
    "hii": "(u.title LIKE '%TEAM H%' OR u.title LIKE '%H队%')",
    "sii": "(u.title LIKE '%TEAM S%' OR u.title LIKE '%S队%')",
    "trainee": "(u.title LIKE '%预备生%' OR u.title LIKE '%新生%')",
}

def _upload_year_month_day(title: str, pub_date: str):
    """从投稿标题/发布日期解析 (年, 月, 日)。

    标题里的日期可能是官方笔误（例如 202606923 多一位），解析结果非法时
    回退到 pub_date，避免整场排期匹配不上。
    """
    match = re.search(r"(20\d{2})(\d{2})(\d{2})", title or "")
    if match:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return year, month, day
    match = re.search(r"(20\d{2})-(\d{2})-(\d{2})", pub_date or "")
    if match:
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    return None


def _performance_year(fetch_time: str, month: int) -> int:
    """排期年份：优先用抓取时间所在年份，兜底沿用日历的跨年推断。"""
    match = re.search(r"(20\d{2})", fetch_time or "")
    if match:
        return int(match.group(1))
    now = datetime.now()
    year = now.year
    if month < now.month - 2:
        year += 1
    return year


def _pick_schedule_upload(perf, candidates, year):
    """在同年投稿里为一场排期挑选录播。

    先按规范队伍匹配，再用剧目名《》收敛；若队伍判定不一致（例如排期标题
    写 YBS《心的旅程》、投稿标题写“新生公演”），则放宽为同日期按剧目名匹配。
    """
    same_day = [u for u in candidates if u["year"] == year]
    if not same_day:
        return None

    team = clean_team_from_title(perf["title"])
    code = (perf["team"] or "").lower()
    if team == CANON_GROUP and code in ("n", "h", "s", "x"):
        team = CODE_TO_CANON[code]

    pool = [u for u in same_day if u["team"] == team]
    plays = re.findall(r"《([^》]+)》", perf["title"] or "")
    if plays:
        named = [u for u in pool if any(p in (u["title"] or "") for p in plays)]
        if named:
            pool = named
        else:
            named_any_team = [u for u in same_day if any(p in (u["title"] or "") for p in plays)]
            if named_any_team:
                pool = named_any_team
    if len(pool) > 1:
        pool = sorted(pool, key=lambda u: (u["parts"], u["pubdate"]), reverse=True)[:1]
    return pool[0] if pool else None

DB_PATH = os.path.join(
    os.path.dirname(os.environ.get("CONFIG_PATH", "/config/roster.yaml")),
    "snh48.db",
)

# 启动时确保 uploads.team 列存在并按标题回填（历史数据一次性清洗）
_upload_store = UploadStore(DB_PATH)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


@router.get("/uploads")
def list_uploads(
    keyword: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    only_missing: bool = False,
    only_downloaded: bool = False,
    category: str = "",
    downloaded_scope: str = "",
    downloaded_codec: str = "",
):
    keyword = keyword.strip()
    where = []
    params = []
    bv_match = BV_RE.search(keyword)
    if bv_match:
        # 直接按 BV 号精确定位（支持粘贴整条链接）
        where.append("u.bvid = ?")
        params.append(bv_match.group(0))
    elif keyword:
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(u.title LIKE ? ESCAPE '\\' OR u.bvid LIKE ? ESCAPE '\\')")
        params.extend([f"%{escaped}%", f"%{escaped}%"])

    if category in CATEGORY_CONDITIONS:
        where.append(CATEGORY_CONDITIONS[category])
    elif category == "special":
        others = " OR ".join(CATEGORY_CONDITIONS.values())
        where.append(f"NOT ({others})")

    conn = _connect()
    try:
        join_downloads = _has_table(conn, "downloads")
        join_sql = (
            "LEFT JOIN downloads d ON d.bvid = u.bvid AND d.status = 'ok'"
            if join_downloads else ""
        )
        downloaded_sql = (
            "CASE WHEN d.bvid IS NULL THEN 0 ELSE 1 END AS downloaded, "
            "d.scope AS downloaded_scope, d.codec AS downloaded_codec"
            if join_downloads else "0 AS downloaded, NULL AS downloaded_scope, NULL AS downloaded_codec"
        )
        if only_missing and join_downloads:
            where.append("d.bvid IS NULL")
        if only_downloaded and join_downloads:
            where.append("d.bvid IS NOT NULL")
        if downloaded_scope in ("full", "mc") and join_downloads:
            where.append("d.scope = ?")
            params.append(downloaded_scope)
        if downloaded_codec in ("av1", "hevc", "h264") and join_downloads:
            where.append("d.codec = ?")
            params.append(downloaded_codec)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM uploads u {join_sql} {where_sql}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT u.bvid, u.title, u.pub_date, u.pubdate, u.duration, u.parts,
                   u.owner_name, {downloaded_sql}
            FROM uploads u {join_sql}
            {where_sql}
            ORDER BY u.pubdate DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
    finally:
        conn.close()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [dict(row) for row in rows],
    }


@router.get("/uploads/categories")
def upload_categories():
    """各快捷分类的投稿数量（special 为未命中其他分类的公演）。"""
    conn = _connect()
    try:
        others = " OR ".join(CATEGORY_CONDITIONS.values())
        categories = {
            key: conn.execute(
                f"SELECT COUNT(*) FROM uploads u WHERE {condition}"
            ).fetchone()[0]
            for key, condition in CATEGORY_CONDITIONS.items()
        }
        categories["special"] = conn.execute(
            f"SELECT COUNT(*) FROM uploads u WHERE NOT ({others})"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
    finally:
        conn.close()
    return {"total": total, "categories": categories}


@router.get("/uploads/stats")
def upload_stats():
    conn = _connect()
    try:
        uploads = conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
        latest = conn.execute("SELECT MAX(pub_date) FROM uploads").fetchone()[0]
        queue = {}
        if _has_table(conn, "upload_queue"):
            queue = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM upload_queue GROUP BY status"
                ).fetchall()
            }
        state = {}
        if _has_table(conn, "sync_state"):
            state = {
                row[0]: row[1]
                for row in conn.execute("SELECT key, value FROM sync_state").fetchall()
            }
        downloaded = 0
        if _has_table(conn, "downloads"):
            downloaded = conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE status='ok'"
            ).fetchone()[0]
    finally:
        conn.close()

    return {
        "uploads": uploads,
        "downloaded": downloaded,
        "latest_pub_date": latest,
        "queue": {
            "pending": queue.get("pending", 0),
            "done": queue.get("done", 0),
            "failed": queue.get("failed", 0),
        },
        "full_sync_completed": state.get("full_sync_completed"),
        "last_incremental_at": state.get("last_incremental_at"),
    }


@router.get("/schedule_status")
def schedule_status(date: str = ""):
    """某天排期列表的录播/入库状态（供前端日历卡片查缺补漏）。

    匹配键：年 + 月 + 日 + 规范队伍名（uploads.team / performances 标题清洗），
    再按剧目名《》收敛；年份取自排期的 fetch_time，避免把往年同日投稿错配进来。
    """
    match = re.search(r"(\d+)月(\d+)日", date or "")
    if not match:
        return {"date": date, "items": []}
    month, day = int(match.group(1)), int(match.group(2))

    conn = _connect()
    try:
        if not _has_table(conn, "performances"):
            return {"date": date, "items": []}
        performances = conn.execute(
            "SELECT id, date, show_time, title, team, link, fetch_time "
            "FROM performances WHERE date=?",
            (date,),
        ).fetchall()
        upload_rows = conn.execute(
            "SELECT bvid, title, team, pub_date, pubdate, parts FROM uploads"
        ).fetchall()
        downloads = {}
        if _has_table(conn, "downloads"):
            downloads = {
                row[0]: {"scope": row[1], "codec": row[2]}
                for row in conn.execute(
                    "SELECT bvid, scope, codec FROM downloads WHERE status='ok'"
                )
            }
    finally:
        conn.close()

    candidates = []
    for row in upload_rows:
        parsed = _upload_year_month_day(row["title"], row["pub_date"])
        if not parsed or parsed[1] != month or parsed[2] != day:
            continue
        candidates.append({
            "bvid": row["bvid"],
            "title": row["title"],
            "year": parsed[0],
            "team": row["team"] or clean_team_from_title(row["title"]),
            "pubdate": row["pubdate"] or 0,
            "parts": row["parts"] or 0,
        })

    items = []
    for perf in performances:
        year = _performance_year(perf["fetch_time"], month)
        team = clean_team_from_title(perf["title"])
        code = (perf["team"] or "").lower()
        if team == CANON_GROUP and code in ("n", "h", "s", "x"):
            team = CODE_TO_CANON[code]

        upload = _pick_schedule_upload(perf, candidates, year)
        bvid = upload["bvid"] if upload else None
        downloaded = downloads.get(bvid) if bvid else None
        items.append({
            "performance_id": perf["id"],
            "show_time": perf["show_time"],
            "title": perf["title"],
            "team": team,
            "year": year,
            "bvid": bvid,
            "upload_title": upload["title"] if upload else None,
            "uploaded": bool(upload),
            "downloaded": bool(downloaded),
            "downloaded_scope": (downloaded or {}).get("scope"),
            "downloaded_codec": (downloaded or {}).get("codec"),
        })
    return {"date": date, "items": items}
