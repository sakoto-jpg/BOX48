"""官方投稿目录查询接口。

数据来自 tools/sync_uploads.py 写入的 uploads / upload_queue / sync_state 表，
并与 downloads 表（已入库历史）关联，标记哪些投稿已经下载过。
"""

import os
import re
import sqlite3

from fastapi import APIRouter, Query

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

DB_PATH = os.path.join(
    os.path.dirname(os.environ.get("CONFIG_PATH", "/config/roster.yaml")),
    "snh48.db",
)


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
        if downloaded_codec in ("av1", "h264") and join_downloads:
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
