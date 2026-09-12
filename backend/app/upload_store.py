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

UPLOAD_SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
  bvid TEXT PRIMARY KEY,
  aid INTEGER,
  title TEXT NOT NULL DEFAULT '',
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
            conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def enqueue(self, bvid: str, position: int) -> bool:
        """加入抓取队列；已存在（含已抓取）返回 False。"""
        now = self._now()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO upload_queue "
                "(bvid, position, status, attempts, created_at, updated_at) "
                "VALUES (?, ?, 'pending', 0, ?, ?)",
                (bvid, position, now, now),
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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO uploads (
                  bvid, aid, title, pubdate, pub_date, duration, parts,
                  owner_mid, owner_name, cover, pages_json, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                  aid=excluded.aid,
                  title=excluded.title,
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
                    detail.get("aid"),
                    detail.get("title") or "",
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
