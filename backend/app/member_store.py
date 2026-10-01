"""花名册任职区间（member_terms）的 SQLite 存储层。

成员在某分团、某队伍的任职区间按年记录，数据来自 Fandom wiki 成员页的
team / former_team 字段，由 tools/sync_member_terms_from_wiki.py 同步。

用途：
- 下载历史公演时按「公演日期 + 队伍」还原当期阵容
- 为 Emby 演职人员提供历史名单

人工维护的 roster.yaml 仍是「当前阵容」的第一来源，member_terms 只用于
按日期回溯历史阵容。
"""

import os
import sqlite3
from datetime import datetime
from typing import List, Optional

MEMBER_TERMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS member_terms (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  member TEXT NOT NULL,
  group_code TEXT NOT NULL,
  team_code TEXT NOT NULL,
  team_raw TEXT,
  start_year INTEGER,
  end_year INTEGER,
  concurrent INTEGER NOT NULL DEFAULT 0,
  wiki_page TEXT,
  source TEXT NOT NULL DEFAULT 'wiki',
  updated_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_member_terms_unique
  ON member_terms(member, group_code, team_code, start_year, end_year);
CREATE INDEX IF NOT EXISTS idx_member_terms_team
  ON member_terms(group_code, team_code, start_year, end_year);
CREATE INDEX IF NOT EXISTS idx_member_terms_member ON member_terms(member);
"""


class MemberStore:
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
            conn.executescript(MEMBER_TERMS_SCHEMA)
            conn.commit()

    def replace_all(self, rows: List[dict]) -> int:
        """整表重建：rows 为 dict(member/group_code/team_code/team_raw/
        start_year/end_year/concurrent/wiki_page)。"""
        now = datetime.now().isoformat(timespec="seconds")
        payload = [
            (
                row["member"], row["group_code"], row["team_code"], row.get("team_raw"),
                row.get("start_year"), row.get("end_year"),
                1 if row.get("concurrent") else 0,
                row.get("wiki_page"), row.get("source", "wiki"), now,
            )
            for row in rows
        ]
        with self._connect() as conn:
            conn.execute("DELETE FROM member_terms")
            conn.executemany(
                "INSERT OR REPLACE INTO member_terms "
                "(member, group_code, team_code, team_raw, start_year, end_year, "
                " concurrent, wiki_page, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                payload,
            )
            conn.commit()
        return len(payload)

    def members_at(self, group_code: str, team_code: str, year: int) -> List[str]:
        """返回某分团某队伍在指定年份的成员（按中文名排序）。"""
        with self._connect() as conn:
            curs = conn.execute(
                "SELECT DISTINCT member FROM member_terms "
                "WHERE group_code = ? AND team_code = ? "
                "  AND (start_year IS NULL OR start_year <= ?) "
                "  AND (end_year IS NULL OR end_year >= ?) "
                "ORDER BY member",
                (group_code, team_code, year, year),
            )
            return [row["member"] for row in curs.fetchall()]

    def terms_of(self, member: str) -> List[dict]:
        with self._connect() as conn:
            curs = conn.execute(
                "SELECT * FROM member_terms WHERE member = ? "
                "ORDER BY start_year IS NULL, start_year",
                (member,),
            )
            return [dict(row) for row in curs.fetchall()]

    def stats(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM member_terms").fetchone()["n"]
            members = conn.execute(
                "SELECT COUNT(DISTINCT member) AS n FROM member_terms"
            ).fetchone()["n"]
            teams = conn.execute(
                "SELECT COUNT(DISTINCT group_code || '/' || team_code) AS n FROM member_terms"
            ).fetchone()["n"]
            updated = conn.execute(
                "SELECT MAX(updated_at) AS ts FROM member_terms"
            ).fetchone()["ts"]
        return {"rows": total, "members": members, "teams": teams, "updated_at": updated}
