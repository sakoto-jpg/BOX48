"""A02：下载协调器的 BVID 幂等认领、串行执行与任务状态。"""

import threading

import pytest

from download_coordinator import (
    STATE_FAILED,
    STATE_SKIPPED,
    STATE_SUCCEEDED,
    DownloadCommand,
    DownloadCoordinator,
)

BVID = "BV1xx411c7mD"
BVID2 = "BV1xx411c7mE"


def test_command_validates_bvid_and_preserves_force_defaults():
    with pytest.raises(ValueError):
        DownloadCommand("not-a-bvid")

    assert DownloadCommand(BVID).preserve_existing is False
    assert DownloadCommand(BVID, force=True).preserve_existing is True
    # 显式值优先于 force 推导
    assert DownloadCommand(BVID, force=True, preserve_existing=False).preserve_existing is False


def test_same_bvid_is_only_claimed_once():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def runner(command):
        calls.append(command.bvid)
        started.set()
        assert release.wait(2)

    coordinator = DownloadCoordinator(runner, lambda bvid: False)

    first = coordinator.submit(DownloadCommand(BVID, source="batch"))
    duplicate = coordinator.submit(DownloadCommand(BVID, source="manual"))
    assert first is not None
    assert duplicate is None
    assert started.wait(2)

    # 执行期间仍然不允许重复认领
    assert coordinator.is_claimed(BVID)
    assert coordinator.submit(DownloadCommand(BVID, source="rewash")) is None

    release.set()
    assert coordinator.wait_idle(2)
    assert calls == [BVID]
    assert coordinator.describe(first.id)["state"] == STATE_SUCCEEDED


def test_already_downloaded_is_skipped_unless_forced():
    calls = []
    coordinator = DownloadCoordinator(
        lambda command: calls.append(command.bvid), lambda bvid: True)

    skipped = coordinator.submit(DownloadCommand(BVID))
    assert coordinator.wait_idle(2)
    assert calls == []
    assert coordinator.describe(skipped.id)["state"] == STATE_SKIPPED

    forced = coordinator.submit(DownloadCommand(BVID, force=True))
    assert coordinator.wait_idle(2)
    assert calls == [BVID]
    assert coordinator.describe(forced.id)["state"] == STATE_SUCCEEDED


def test_submit_many_dedupes_and_preserves_order():
    started = threading.Event()
    release = threading.Event()
    calls = []

    def runner(command):
        calls.append(command.bvid)
        started.set()
        assert release.wait(2)

    coordinator = DownloadCoordinator(runner, lambda bvid: False)
    accepted, duplicates = coordinator.submit_many([
        DownloadCommand(BVID, source="batch"),
        DownloadCommand(BVID2, source="batch"),
        DownloadCommand(BVID, source="batch"),
    ])

    assert [task.command.bvid for task in accepted] == [BVID, BVID2]
    assert [command.bvid for command in duplicates] == [BVID]
    assert started.wait(2)

    # 单 worker：第一个执行中，第二个仍在排队
    assert [item["bvid"] for item in coordinator.queue_snapshot()] == [BVID2]

    release.set()
    assert coordinator.wait_idle(2)
    assert calls == [BVID, BVID2]
    assert coordinator.queue_snapshot() == []


def test_failure_is_reported_and_claim_released():
    def runner(command):
        return command.bvid != BVID

    coordinator = DownloadCoordinator(runner, lambda bvid: False)
    failed = coordinator.submit(DownloadCommand(BVID))
    assert coordinator.wait_idle(2)
    status = coordinator.describe(failed.id)
    assert status["state"] == STATE_FAILED
    assert status["finished_at"] is not None

    # 失败后必须释放认领，允许重试
    retried = coordinator.submit(DownloadCommand(BVID, force=True))
    assert retried is not None
    assert coordinator.wait_idle(2)


def test_runner_exception_marks_failed_and_releases_claim():
    def runner(command):
        raise RuntimeError("boom")

    coordinator = DownloadCoordinator(runner, lambda bvid: False)
    task = coordinator.submit(DownloadCommand(BVID))
    assert coordinator.wait_idle(2)

    status = coordinator.describe(task.id)
    assert status["state"] == STATE_FAILED
    assert "boom" in status["error"]
    assert not coordinator.is_claimed(BVID)
