"""A05：日历的 def 队伍判定必须按分团隔离。"""

import sqlite3

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("icalendar")
pytest.importorskip("jinja2")
pytest.importorskip("requests")
pytest.importorskip("yaml")

from app import main as calendar_main


def _make_uploads_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE uploads (bvid TEXT PRIMARY KEY, group_code TEXT, title TEXT)")
    conn.executemany(
        "INSERT INTO uploads (bvid, group_code, title) VALUES (?, ?, ?)",
        [("BV1xx411c7mD", "gnz", "20260531 新生公演《心的旅程》")],
    )
    conn.commit()
    conn.close()


def test_def_label_only_uses_same_group_uploads(tmp_path, monkeypatch):
    db_path = tmp_path / "snh48.db"
    _make_uploads_db(db_path)
    monkeypatch.setattr(calendar_main, "SCHEDULE_DB", str(db_path))
    # 花名册不存在：排除关键词回退的影响，只验证投稿查询的分团过滤
    monkeypatch.setattr(calendar_main, "ROSTER_PATH", str(tmp_path / "roster.yaml"))

    api_date = "2026-05-31 18:30"
    assert calendar_main._lookup_def_label(api_date, "《心的旅程》", "snh") == ""
    assert calendar_main._lookup_def_label(api_date, "《心的旅程》", "gnz") == "预备生"


def test_def_label_requires_matching_play(tmp_path, monkeypatch):
    db_path = tmp_path / "snh48.db"
    _make_uploads_db(db_path)
    monkeypatch.setattr(calendar_main, "SCHEDULE_DB", str(db_path))
    monkeypatch.setattr(calendar_main, "ROSTER_PATH", str(tmp_path / "roster.yaml"))

    assert calendar_main._lookup_def_label(
        "2026-05-31 18:30", "《其他剧目》", "gnz") == ""


def test_trainee_keywords_are_loaded_per_group(tmp_path, monkeypatch):
    roster = tmp_path / "roster.yaml"
    roster.write_text(
        "teams:\n"
        "  - name: 预备生\n"
        '    keywords: ["预备生", "新生"]\n'
        "  - name: GNZ48预备生\n"
        '    keywords: ["GNZ新生"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(calendar_main, "ROSTER_PATH", str(roster))
    monkeypatch.setattr(
        calendar_main, "_trainee_keyword_cache", {"mtime": None, "by_group": {}})
    monkeypatch.setattr(calendar_main.group_config, "teams", lambda path=None: [
        {"group": "snh", "name": "预备生"},
        {"group": "gnz", "name": "GNZ48预备生"},
    ])

    assert calendar_main._load_trainee_keywords("snh") == ["预备生", "新生"]
    assert calendar_main._load_trainee_keywords("gnz") == ["GNZ新生"]
    assert calendar_main._load_trainee_keywords("bej") == []
