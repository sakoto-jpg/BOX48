"""A03：下载入口到历史库的分团身份传递。"""

import pytest

pytest.importorskip("yt_dlp")
pytest.importorskip("requests")

import scheduler

BVID = "BV1xx411c7mD"
BVID2 = "BV1xx411c7mE"

CONFIG_YAML = """
teams:
  - name: GNZ48 TEAM G
    enabled: true
    keywords: ["TEAM G"]
    members: []
  - name: 特殊公演
    enabled: true
    keywords: ["特殊公演"]
    members: []
"""

TEAMS = [
    {"group": "snh", "name": "TEAM NII", "code": "n", "keywords": ["TEAM NII"]},
    {"group": "gnz", "name": "GNZ48 TEAM G", "code": "g", "keywords": ["TEAM G"]},
]


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    config_path = tmp_path / "roster.yaml"
    config_path.write_text(CONFIG_YAML, encoding="utf-8")
    instance = scheduler.Snh48Scanner(
        str(config_path), str(tmp_path / "cookie.txt"), str(tmp_path / "downloads"))
    monkeypatch.setattr(scheduler.group_config, "teams", lambda path=None: list(TEAMS))
    return instance


def _patch_ydl(monkeypatch, title):
    class FakeYoutubeDL:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def extract_info(self, url, download=False):
            return {"title": title, "upload_date": "20260101"}

    monkeypatch.setattr(scheduler.yt_dlp, "YoutubeDL", FakeYoutubeDL)


def _capture_process_info(monkeypatch, scanner, captured):
    def fake_process_info(*args, **kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(scanner, "_process_video_info", fake_process_info)


def test_group_is_derived_from_matched_team(scanner, monkeypatch):
    captured = {}
    _patch_ydl(monkeypatch, "20260101 GNZ48 TEAM G 公演《测试》")
    _capture_process_info(monkeypatch, scanner, captured)

    assert scanner._process_bvid(BVID, include_disabled=False) is True
    assert captured["group_code"] == "gnz"


def test_explicit_group_wins_over_team_lookup(scanner, monkeypatch):
    captured = {}
    _patch_ydl(monkeypatch, "20260101 GNZ48 特殊公演《测试》")
    _capture_process_info(monkeypatch, scanner, captured)

    assert scanner._process_bvid(BVID, include_disabled=True, group_code="gnz") is True
    assert captured["group_code"] == "gnz"


def test_history_persists_resolved_group_and_rejects_empty(scanner):
    scanner._mark_downloaded(
        BVID, "20260101 GNZ48 TEAM G 公演《测试》", "gnz",
        codec="av1", path="", scope="full")

    record = scanner.history_store.get(BVID)
    assert record["group_code"] == "gnz"

    with pytest.raises(ValueError):
        scanner.history_store.mark_downloaded(BVID2, "标题", "")


def test_subscription_group_from_explicit_or_mid(monkeypatch):
    monkeypatch.setattr(scheduler.group_config, "groups", lambda path=None: [
        {"code": "snh", "bilibili_mid": 2832224},
        {"code": "gnz", "bilibili_mid": 27363163},
    ])

    assert scheduler.Snh48Scanner._subscription_group(
        {"url": "https://space.bilibili.com/27363163"}) == "gnz"
    assert scheduler.Snh48Scanner._subscription_group(
        {"url": "https://space.bilibili.com/27363163", "group_code": "bej"}) == "bej"
    # 未知订阅不预设分团，后续按匹配队伍反查
    assert scheduler.Snh48Scanner._subscription_group(
        {"url": "https://space.bilibili.com/99999999"}) == ""
