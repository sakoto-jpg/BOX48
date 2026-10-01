"""SNH48 官方投稿目录的 SQLite 存储层。

三张表：
- uploads     投稿详情（bvid 主键）
- upload_queue 抓取队列（支持断点续爬）
- sync_state  同步状态（全量是否完成、上次增量时间等）
"""

import json
import os
import sqlite3
from datetime import datetime

from team_utils import clean_team_from_title, resolve_team

UPLOAD_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
  bvid TEXT PRIMARY KEY,
  group_code TEXT,
  aid INTEGER,
  title TEXT NOT NULL DEFAULT '',
  team TEXT,
  pubdate INTEGER,
  pub_date TEXT,
  duration INTEGER,
  parts INTEGER,
  owner_mid INTEGER,
  owner_name TEXT,
  cover TEXT,
  pages_json TEXT,
  first_seen TEXT,
  last_seen TEXT
);
CREATE INDEX IF NOT EXISTS idx_uploads_pubdate ON uploads(pubdate DESC);
CREATE TABLE IF NOT EXISTS upload_queue (
  bvid TEXT PRIMARY KEY,
  group_code TEXT,
  position INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_upload_queue_status ON upload_queue(status, position);
CREATE TABLE IF NOT EXISTS sync_state (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


class UploadStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _ensure_schema(self):
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(UPLOAD_SCHEMA)
            self._migrate(conn)
            conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection):
        """老库补列：group_code / team，并回填历史数据。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(uploads)")}
        if "team" not in columns:
            conn.execute("ALTER TABLE uploads ADD COLUMN team TEXT")
        if "group_code" not in columns:
            conn.execute("ALTER TABLE uploads ADD COLUMN group_code TEXT")
        # 阶段 1 时投稿目录只同步过 SNH48 官方账号，历史行按 snh 回填
        conn.execute(
            "UPDATE uploads SET group_code = 'snh' "
            "WHERE group_code IS NULL OR group_code = ''"
        )
        queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(upload_queue)")}
        if "group_code" not in queue_columns:
            conn.execute("ALTER TABLE upload_queue ADD COLUMN group_code TEXT")
        conn.execute(
            "UPDATE upload_queue SET group_code = 'snh' "
            "WHERE group_code IS NULL OR group_code = ''"
        )
        # 阶段 5：同步状态改为按分团分键，历史全局键迁移给 snh
        for key in ("full_sync_completed", "enumerated_at", "enumerated_count",
                    "last_incremental_at"):
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key=?", (key,)
            ).fetchone()
            if row:
                conn.execute(
                    "INSERT OR IGNORE INTO sync_state (key, value) VALUES (?, ?)",
                    (f"snh:{key}", row[0]),
                )
        rows = conn.execute(
            "SELECT bvid, title FROM uploads WHERE team IS NULL OR team = ''"
        ).fetchall()
        if rows:
            conn.executemany(
                "UPDATE uploads SET team = ? WHERE bvid = ?",
                [(resolve_team(row[1], "snh"), row[0]) for row in rows],
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def enqueue(self, bvid: str, position: int, group_code: str = "snh") -> bool:
        """加入抓取队列；已存在（含已抓取）返回 False。"""
        now = self._now()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO upload_queue "
                "(bvid, group_code, position, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', 0, ?, ?)",
                (bvid, group_code, position, now, now),
            )
            conn.commit()
            return cur.rowcount > 0

    def pending(self, limit: int | None = None, include_failed: bool = False):
        statuses = "('pending', 'failed')" if include_failed else "('pending')"
        sql = f"SELECT bvid FROM upload_queue WHERE status IN {statuses} ORDER BY position"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as conn:
            return [row[0] for row in conn.execute(sql).fetchall()]

    def next_pending(self):
        """取队首待抓取项，返回 (bvid, group_code)。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT bvid, group_code FROM upload_queue WHERE status = 'pending' "
                "ORDER BY position LIMIT 1"
            ).fetchone()
            return (row[0], row[1] or "snh") if row else None

    def pending_stats(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM upload_queue GROUP BY status"
            ).fetchall()
            return {row[0]: row[1] for row in rows}

    def mark_done(self, bvid: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE upload_queue SET status='done', last_error=NULL, updated_at=? WHERE bvid=?",
                (self._now(), bvid),
            )
            conn.commit()

    def record_failure(self, bvid: str, error: str, max_attempts: int = 3):
        """记录一次失败；达到 max_attempts 后标记 failed，否则保持 pending。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM upload_queue WHERE bvid=?", (bvid,)
            ).fetchone()
            attempts = (row[0] if row else 0) + 1
            status = "failed" if attempts >= max_attempts else "pending"
            conn.execute(
                "UPDATE upload_queue SET status=?, attempts=?, last_error=?, updated_at=? "
                "WHERE bvid=?",
                (status, attempts, (error or "")[:500], self._now(), bvid),
            )
            conn.commit()
            return status, attempts

    def reset_failed(self) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE upload_queue SET status='pending', attempts=0, updated_at=? "
                "WHERE status='failed'",
                (self._now(),),
            )
            conn.commit()
            return cur.rowcount

    def upsert_upload(self, detail: dict):
        now = self._now()
        group_code = detail.get("group_code") or "snh"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO uploads (
                  bvid, group_code, aid, title, team, pubdate, pub_date, duration, parts,
                  owner_mid, owner_name, cover, pages_json, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                  group_code=excluded.group_code,
                  aid=excluded.aid,
                  title=excluded.title,
                  team=excluded.team,
                  pubdate=excluded.pubdate,
                  pub_date=excluded.pub_date,
                  duration=excluded.duration,
                  parts=excluded.parts,
                  owner_mid=excluded.owner_mid,
                  owner_name=excluded.owner_name,
                  cover=excluded.cover,
                  pages_json=excluded.pages_json,
                  last_seen=excluded.last_seen
                """,
                (
                    detail.get("bvid"),
                    group_code,
                    detail.get("aid"),
                    detail.get("title") or "",
                    detail.get("team") or resolve_team(detail.get("title"), group_code),
                    detail.get("pubdate"),
                    detail.get("pub_date"),
                    detail.get("duration"),
                    detail.get("parts"),
                    detail.get("owner_mid"),
                    detail.get("owner_name"),
                    detail.get("cover"),
                    json.dumps(detail.get("pages") or [], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()

    def counts(self) -> dict:
        with self._connect() as conn:
            uploads = conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
            queue = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT status, COUNT(*) FROM upload_queue GROUP BY status"
                ).fetchall()
            }
            latest = conn.execute("SELECT MAX(pub_date) FROM uploads").fetchone()[0]
        return {"uploads": uploads, "queue": queue, "latest_pub_date": latest}

    def group_stats(self) -> list:
        """按分团统计已入库数量与最新发布日期。"""
        with self._connect() as conn:
            return [
                {"group_code": row[0] or "snh", "uploads": row[1],
                 "latest_pub_date": row[2]}
                for row in conn.execute(
                    "SELECT group_code, COUNT(*), MAX(pub_date) FROM uploads "
                    "GROUP BY group_code ORDER BY group_code"
                ).fetchall()
            ]

    def get_state(self, key: str):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM sync_state WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sync_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()

    def list_uploads(self, limit: int = 50, offset: int = 0):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT bvid, title, pub_date, duration, parts FROM uploads "
                "ORDER BY pubdate DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]
