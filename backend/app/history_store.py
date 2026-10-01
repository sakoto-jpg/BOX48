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

from team_utils import (
    CANON_GROUP,
    CANON_HII,
    CANON_NII,
    CANON_SII,
    CANON_TRAINEE,
    CANON_X,
    canon_from_dir,
    normalize_team,
)

DATE8_RE = re.compile(r"20\d{6}")
DATE_DASH_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")
CANONICAL_TEAMS = {CANON_NII, CANON_HII, CANON_SII, CANON_X, CANON_TRAINEE, CANON_GROUP}

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS downloads (
  bvid TEXT PRIMARY KEY,
  group_code TEXT,
  title TEXT NOT NULL DEFAULT '',
  codec TEXT,
  path TEXT,
  scope TEXT,
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
    # 目录名可能是 SNH48 TEAM NII 这种带分团前缀的规范形式
    return canon_from_dir(rest.split("/", 1)[0]) or rest.split("/", 1)[0]


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
            self._migrate(conn)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('schema_version', '1')"
            )
            conn.commit()

    @staticmethod
    def _migrate(conn: sqlite3.Connection):
        """老库补列：scope（整场/仅MC）与 group_code（分团）。"""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(downloads)")}
        if "scope" not in columns:
            conn.execute("ALTER TABLE downloads ADD COLUMN scope TEXT")
        if "group_code" not in columns:
            conn.execute("ALTER TABLE downloads ADD COLUMN group_code TEXT")
        # 阶段 1：历史数据全部属于 SNH48
        conn.execute(
            "UPDATE downloads SET group_code = 'snh' "
            "WHERE group_code IS NULL OR group_code = ''"
        )
        # 历史记录里标题/路径带「MC合集」的必然是仅MC，其余按整场处理
        conn.execute(
            "UPDATE downloads SET scope = CASE "
            "WHEN path LIKE '%MC合集%' OR title LIKE '%MC合集%' THEN 'mc' "
            "ELSE 'full' END "
            "WHERE scope IS NULL OR scope = ''"
        )
        # 对齐队伍字段：旧库里是目录名(YBS、特殊公演等)，统一转成规范名称
        updates = []
        for row in conn.execute("SELECT bvid, title, path, team FROM downloads").fetchall():
            if row[3] in CANONICAL_TEAMS:
                continue
            updates.append((normalize_team(row[1], _derive_team(row[2])), row[0]))
        if updates:
            conn.executemany("UPDATE downloads SET team = ? WHERE bvid = ?", updates)

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
                        path: str = "", status: str = "ok", scope: str = "full",
                        group_code: str = "snh"):
        now = datetime.now().isoformat(timespec="seconds")
        file_size = os.path.getsize(path) if path and os.path.exists(path) else None
        codec_source = "probed" if codec else None
        scope = scope if scope in ("full", "mc") else "full"
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO downloads (
                  bvid, group_code, title, codec, path, scope, status, path_source, codec_source,
                  file_size, team, perf_date, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'recorded', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                  group_code=excluded.group_code,
                  title=excluded.title,
                  codec=excluded.codec,
                  path=excluded.path,
                  scope=excluded.scope,
                  status=excluded.status,
                  path_source=excluded.path_source,
                  codec_source=excluded.codec_source,
                  file_size=excluded.file_size,
                  team=excluded.team,
                  perf_date=excluded.perf_date,
                  updated_at=excluded.updated_at
                """,
                (
                    bvid, group_code or "snh", title, codec or None, path or None,
                    scope, status, codec_source,
                    file_size, normalize_team(title, _derive_team(path)),
                    _derive_date(title, path), now, now,
                ),
            )
            conn.commit()

    def get(self, bvid: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT bvid, group_code, title, codec, path, scope, status "
                "FROM downloads WHERE bvid=?",
                (bvid,),
            ).fetchone()
        return dict(row) if row else None

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
                "SELECT bvid, title, codec, path, scope, team, perf_date, group_code "
                "FROM downloads WHERE status = 'ok' ORDER BY rowid DESC"
            ).fetchall()
        return [dict(row) for row in rows]
