"""公演排期 SQLite 存储层（替代原来的 app/data.json）。

保持原有数据结构：一行对应一场公演，原始记录以 JSON 保存，
读取时还原为 {ticket_id: record} 字典，保证 API 返回结构完全不变。
"""

import json
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS performances (
  id TEXT PRIMARY KEY,
  group_code TEXT,
  date TEXT,
  show_time TEXT,
  title TEXT,
  link TEXT,
  team TEXT,
  fetch_time TEXT,
  raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_performances_date ON performances(date);
"""


class ScheduleStore:
    def __init__(self, db_path: str, legacy_json: str = None):
        self.db_path = db_path
        self.legacy_json = legacy_json
        self._ensure_schema()
        self._import_legacy_if_empty()

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
            columns = {row[1] for row in conn.execute("PRAGMA table_info(performances)")}
            if "group_code" not in columns:
                conn.execute("ALTER TABLE performances ADD COLUMN group_code TEXT")
            # 阶段 1：历史排期全部属于 SNH48
            conn.execute(
                "UPDATE performances SET group_code = 'snh' "
                "WHERE group_code IS NULL OR group_code = ''"
            )
            conn.commit()

    def _import_legacy_if_empty(self):
        """数据库为空时，把项目内置的 data.json 一次性导入。"""
        try:
            if self.count() > 0:
                return
            if not self.legacy_json or not os.path.exists(self.legacy_json):
                return
            with open(self.legacy_json, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data:
                self.save(data)
                print(f"✅ [Schedule] 已从 {self.legacy_json} 导入 {len(data)} 场公演", flush=True)
        except Exception as exc:
            print(f"⚠️ [Schedule] 旧数据导入失败: {exc}", flush=True)

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM performances").fetchone()[0]

    def load(self) -> dict:
        with self._connect() as conn:
            rows = conn.execute("SELECT raw_json FROM performances").fetchall()
        data = {}
        for row in rows:
            try:
                record = json.loads(row[0])
            except (TypeError, json.JSONDecodeError):
                continue
            if record.get("id"):
                data[record["id"]] = record
        return data

    def save(self, data: dict):
        if not data:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO performances
                  (id, group_code, date, show_time, title, link, team, fetch_time, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  group_code=excluded.group_code,
                  date=excluded.date,
                  show_time=excluded.show_time,
                  title=excluded.title,
                  link=excluded.link,
                  team=excluded.team,
                  fetch_time=excluded.fetch_time,
                  raw_json=excluded.raw_json
                """,
                [
                    (
                        str(entry.get("id")),
                        entry.get("group_code") or "snh",
                        entry.get("date", ""),
                        entry.get("show_time", ""),
                        entry.get("title", ""),
                        entry.get("link", ""),
                        entry.get("team", ""),
                        entry.get("fetch_time", ""),
                        json.dumps(entry, ensure_ascii=False),
                    )
                    for entry in data.values()
                    if entry.get("id")
                ],
            )
            conn.commit()

    def delete(self, ids: list):
        """删除指定 id 的排期记录（用于清理已失效的临时场次）。"""
        if not ids:
            return
        with self._connect() as conn:
            conn.executemany("DELETE FROM performances WHERE id=?", [(i,) for i in ids])
            conn.commit()
