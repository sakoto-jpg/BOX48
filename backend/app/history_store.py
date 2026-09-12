"""下载历史的 SQLite 存储层。

替代原来的 config/download_history.txt：
- 一场公演一条记录（bvid 主键），天然去重
- 只有 status='ok' 才视为已入库，missing/partial 会触发重新下载
- WAL + busy_timeout，多线程/多进程短事务写入安全
- 旧 txt 保留为只读备份，由 tools/migrate_history.py 一次性导入
"""

import os
import re
import sqlite3
from datetime import datetime
from typing import List, Optional, Tuple

DATE8_RE = re.compile(r"20\d{6}")
DATE_DASH_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
  bvid TEXT PRIMARY KEY,
  title TEXT NOT NULL DEFAULT '',
  codec TEXT,
  path TEXT,
  status TEXT NOT NULL DEFAULT 'ok',
  path_source TEXT,
  codec_source TEXT,
  file_size INTEGER,
  duration REAL,
  team TEXT,
  perf_date TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS orphan_files (
  path TEXT PRIMARY KEY,
  title TEXT,
  perf_date TEXT,
  discovered_at TEXT
);
"""


def _derive_team(path: Optional[str]) -> Optional[str]:
    if not path or not path.startswith("/downloads/"):
        return None
    rest = path[len("/downloads/"):]
    if "/" not in rest:
        return None
    return rest.split("/", 1)[0]


def _derive_date(title: str, path: Optional[str]) -> Optional[str]:
    match = DATE8_RE.search(title or "")
    if match:
        return match.group(0)
    if path:
        match = DATE8_RE.search(os.path.basename(path))
        if match:
            return match.group(0)
        match = DATE_DASH_RE.search(path)
        if match:
            return "".join(match.groups())
    return None


class HistoryStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_schema()
        self._warn_legacy_history()

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
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1')"
            )
            conn.commit()

    def _warn_legacy_history(self):
        """数据库为空但存在旧 txt 时给出提示，避免静默丢历史。"""
        legacy_path = os.path.join(os.path.dirname(self.db_path), "download_history.txt")
        try:
            if not os.path.exists(legacy_path) or os.path.getsize(legacy_path) == 0:
                return
            with self._connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
            if count == 0:
                print(
                    f"⚠️ [History] 数据库为空但检测到旧历史文件 {legacy_path}，"
                    f"请运行 tools/migrate_history.py 导入",
                    flush=True,
                )
        except Exception:
            pass

    def is_downloaded(self, bvid: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM downloads WHERE bvid = ? AND status = 'ok' LIMIT 1",
                (bvid,),
            ).fetchone()
        return row is not None

    def mark_downloaded(self, bvid: str, title: str, codec: str = "",
                        path: str = "", status: str = "ok"):
        now = datetime.now().isoformat(timespec="seconds")
        file_size = os.path.getsize(path) if path and os.path.exists(path) else None
        codec_source = "probed" if codec else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO downloads (
                  bvid, title, codec, path, status, path_source, codec_source,
                  file_size, team, perf_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'recorded', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                  title=excluded.title,
                  codec=excluded.codec,
                  path=excluded.path,
                  status=excluded.status,
                  path_source=excluded.path_source,
                  codec_source=excluded.codec_source,
                  file_size=excluded.file_size,
                  team=excluded.team,
                  perf_date=excluded.perf_date,
                  updated_at=excluded.updated_at
                """,
                (
                    bvid, title, codec or None, path or None, status, codec_source,
                    file_size, _derive_team(path), _derive_date(title, path), now, now,
                ),
            )
            conn.commit()

    def recent(self, limit: int = 2) -> List[Tuple[str, str, str, str]]:
        """最近入库的 (bvid, title, codec, path)，供自动洗版使用。

        按 rowid 倒序：插入顺序即入库顺序。洗版只更新同一行，不会把老场次
        顶到最前面，避免反复选中同一场。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT bvid, title, COALESCE(codec, ''), COALESCE(path, '') "
                "FROM downloads WHERE status = 'ok' ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(row[0], row[1], row[2], row[3]) for row in rows]

    def list_completed(self) -> List[dict]:
        """已入库列表（新→旧），供 /api/history 使用。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT bvid, title, codec, path, team, perf_date "
                "FROM downloads WHERE status = 'ok' ORDER BY rowid DESC"
            ).fetchall()
        return [dict(row) for row in rows]
