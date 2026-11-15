"""A01：下载/合并失败时不得删除已有有效媒体。"""

import glob
import os
import time

import pytest

pytest.importorskip("yt_dlp")
pytest.importorskip("requests")

import scheduler

BVID = "BV1xx411c7mD"


@pytest.fixture
def scanner(tmp_path, monkeypatch):
    config_path = tmp_path / "roster.yaml"
    config_path.write_text("teams: []\n", encoding="utf-8")
    instance = scheduler.Snh48Scanner(
        str(config_path), str(tmp_path / "cookie.txt"), str(tmp_path / "downloads"))
    monkeypatch.setattr(instance, "_probe_best_codec", lambda *args, **kwargs: "av01")
    monkeypatch.setattr(instance, "_save_episode_thumb", lambda *args, **kwargs: None)
    return instance


def _prepare_episode(scanner, tmp_path, monkeypatch, with_old_media):
    perf_dir = tmp_path / "downloads" / "GNZ48 TEAM G" / "2026-01-01 公演测试"
    perf_dir.mkdir(parents=True)
    base_path = str(perf_dir / "2026-01-01 公演测试")
    monkeypatch.setattr(
        scanner.nfo_builder, "build_metadata", lambda **kwargs: base_path)

    final_path = f"{base_path}.mkv"
    if with_old_media:
        with open(final_path, "wb") as fh:
            fh.write(b"old-valid-media")
        old_time = time.time() - 3600
        os.utime(final_path, (old_time, old_time))
    return base_path, final_path, perf_dir


def _patch_downloader(scanner, monkeypatch, fail_parts):
    def fake_download_part(bvid, p_number, output_path,
                           progress_callback=None, prefer_codec="av01"):
        if p_number in fail_parts:
            return False, ""
        with open(f"{output_path}.mkv", "wb") as fh:
            fh.write(b"current-run-part")
        return True, f"P{p_number}"

    monkeypatch.setattr(scanner.downloader, "download_part", fake_download_part)


def _run(scanner, preserve_existing):
    info = {
        "title": "20260101 GNZ48 TEAM G 公演《测试》",
        "description": "",
        "thumbnail": "",
        "entries": [{"title": "P1"}, {"title": "P2"}],
    }
    return scanner._process_video_info(
        BVID, info["title"], info, "GNZ48 TEAM G", "20260101", "2026", "2026-01-01",
        preserve_existing=preserve_existing, scope="full", group_code="gnz")


def test_forced_download_failure_preserves_old_media(scanner, tmp_path, monkeypatch):
    base_path, final_path, _ = _prepare_episode(
        scanner, tmp_path, monkeypatch, with_old_media=True)
    _patch_downloader(scanner, monkeypatch, fail_parts={2})

    assert _run(scanner, preserve_existing=True) is False

    with open(final_path, "rb") as fh:
        assert fh.read() == b"old-valid-media"
    assert glob.glob(f"{base_path} - pt*") == []
    assert scanner.history_store.get(BVID) is None


def test_merge_failure_preserves_preexisting_media(scanner, tmp_path, monkeypatch):
    # 即使入口没有传 force（preserve_existing=False），目录里已有的媒体也不能删
    base_path, final_path, _ = _prepare_episode(
        scanner, tmp_path, monkeypatch, with_old_media=True)
    _patch_downloader(scanner, monkeypatch, fail_parts=set())
    monkeypatch.setattr(scheduler, "merge_parts", lambda *args, **kwargs: False)

    assert _run(scanner, preserve_existing=False) is False

    with open(final_path, "rb") as fh:
        assert fh.read() == b"old-valid-media"
    assert glob.glob(f"{base_path} - pt*") == []
    assert scanner.history_store.get(BVID) is None


def test_fresh_download_failure_cleans_incomplete_directory(scanner, tmp_path, monkeypatch):
    _, final_path, perf_dir = _prepare_episode(
        scanner, tmp_path, monkeypatch, with_old_media=False)
    _patch_downloader(scanner, monkeypatch, fail_parts={2})

    assert _run(scanner, preserve_existing=False) is False

    assert not os.path.exists(final_path)
    assert not os.path.exists(perf_dir)
    assert scanner.history_store.get(BVID) is None
