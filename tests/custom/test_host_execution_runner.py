"""Offline tests for the conservative host execution dispatcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts import host_execution_runner as runner


def make_record(root: Path, state: str, **extra: object) -> Path:
    path = root / "storage" / "content_jobs" / "niche" / "cid" / "production-schedule.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"content_id": "cid", "production_plan_path": "/plan.json", "production_state": state, **extra}), encoding="utf-8")
    return path


def record(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_now_launching_selected(tmp_path: Path) -> None:
    path = make_record(tmp_path, "launching")
    decisions = runner.reconcile_now(project_root=tmp_path, dry_run=True)
    assert decisions[0]["action"] == "would_launch"
    assert record(path)["production_state"] == "launching"


def test_now_completed_and_error_ignored(tmp_path: Path) -> None:
    for state in ("completed", "error"):
        root = tmp_path / state
        make_record(root, state)
        assert runner.reconcile_now(project_root=root) == [{"kind": "now", "content_id": "cid", "action": "ignored"}]


def test_live_producer_ignored(tmp_path: Path) -> None:
    make_record(tmp_path, "producing", boot_id="boot", pid=88)
    with patch.object(runner, "current_boot_id", return_value="boot"), patch.object(runner, "pid_is_alive", return_value=True):
        assert runner.reconcile_now(project_root=tmp_path)[0]["action"] == "ignored"


def test_dead_or_previous_boot_producer_is_stale_without_execution(tmp_path: Path) -> None:
    for name, boot_id, alive, reason in (("dead", "boot", False, "pid_dead"), ("old", "old-boot", True, "boot_changed")):
        root = tmp_path / name
        make_record(root, "producing", boot_id=boot_id, pid=88)
        with patch.object(runner, "current_boot_id", return_value="boot"), patch.object(runner, "pid_is_alive", return_value=alive), patch.object(runner.subprocess, "Popen") as popen:
            result = runner.reconcile_now(project_root=root)[0]
        assert result["action"] == "stale" and result["reason"] == reason
        popen.assert_not_called()


def test_dry_run_never_executes(tmp_path: Path) -> None:
    path = make_record(tmp_path, "launching")
    with patch.object(runner.subprocess, "Popen") as popen:
        runner.reconcile_once(project_root=tmp_path, dry_run=True)
    popen.assert_not_called()
    assert record(path)["production_state"] == "launching"


def test_now_real_uses_canonical_command_and_completes(tmp_path: Path) -> None:
    path = make_record(tmp_path, "launching", production_plan_path="/plans/approved.json")
    process = MagicMock(pid=4321)
    process.wait.return_value = 0
    with patch.object(runner, "current_boot_id", return_value="boot"), patch.object(runner.subprocess, "Popen", return_value=process) as popen, patch.object(runner.content_delivery, "finalize_production_plan") as deliver:
        runner.reconcile_now(project_root=tmp_path)
    popen.assert_called_once_with(["python3", "scripts/produce_batch.py", "--production", "--approved-plan", "/plans/approved.json"], cwd=tmp_path.as_posix())
    deliver.assert_called_once_with(Path("/plans/approved.json"))
    assert record(path)["production_state"] == "completed"


def test_now_failure_writes_error(tmp_path: Path) -> None:
    path = make_record(tmp_path, "launching")
    process = MagicMock(pid=4321)
    process.wait.return_value = 1
    with patch.object(runner, "current_boot_id", return_value="boot"), patch.object(runner.subprocess, "Popen", return_value=process):
        runner.reconcile_now(project_root=tmp_path)
    assert record(path)["production_state"] == "error"


def test_finalize_only_delivers_once_without_rendering_and_completes_schedule(tmp_path: Path) -> None:
    path = make_record(tmp_path, "error")
    with patch.object(runner.content_delivery, "finalize_production_plan", return_value={"checksum": "x"}) as deliver, patch.object(runner.subprocess, "Popen") as popen:
        result = runner.finalize_only("/plan.json", project_root=tmp_path)
    deliver.assert_called_once_with(Path("/plan.json"))
    popen.assert_not_called()
    assert result == {"checksum": "x"}
    assert record(path)["production_state"] == "completed"


def test_finalize_only_delivery_error_marks_schedule_error_and_keeps_mp4(tmp_path: Path) -> None:
    path = make_record(tmp_path, "producing")
    mp4 = tmp_path / "already-rendered.mp4"
    mp4.write_bytes(b"keep")
    before = mp4.read_bytes()
    with patch.object(runner.content_delivery, "finalize_production_plan", side_effect=runner.content_delivery.DeliveryError("offline")), patch.object(runner.subprocess, "Popen") as popen:
        try:
            runner.finalize_only("/plan.json", project_root=tmp_path)
        except runner.content_delivery.DeliveryError:
            pass
        else:
            raise AssertionError("expected delivery error")
    popen.assert_not_called()
    assert record(path)["production_state"] == "error"
    assert mp4.read_bytes() == before


def test_finalize_only_completed_delivery_remains_idempotently_completed(tmp_path: Path) -> None:
    path = make_record(tmp_path, "completed")
    with patch.object(runner.content_delivery, "finalize_production_plan", return_value={"checksum": "already"}) as deliver:
        runner.finalize_only("/plan.json", project_root=tmp_path)
    deliver.assert_called_once_with(Path("/plan.json"))
    assert record(path)["production_state"] == "completed"


def make_pending_job(root: Path) -> Path:
    pending = root / "storage" / "nightly_jobs" / "pending"
    pending.mkdir(parents=True)
    return pending / "job.json"


def test_night_no_pending_and_outside_window_do_nothing(tmp_path: Path) -> None:
    assert runner.reconcile_night(project_root=tmp_path)["action"] == "no_pending"
    pending_job = make_pending_job(tmp_path)
    pending_job.write_text("{}", encoding="utf-8")
    with patch.object(runner, "nightly_window_is_open", return_value=False):
        assert runner.reconcile_night(project_root=tmp_path)["action"] == "outside_window"


def test_night_active_canonical_lock_does_not_launch_duplicate(tmp_path: Path) -> None:
    pending_job = make_pending_job(tmp_path)
    pending_job.write_text("{}", encoding="utf-8")
    lock = pending_job.parent.parent / "nightly_runner.lock"
    lock.write_text(f"pid={os.getpid()}\n", encoding="utf-8")
    with patch.object(runner, "nightly_window_is_open", return_value=True):
        with patch.object(runner.subprocess, "Popen") as popen:
            assert runner.reconcile_night(project_root=tmp_path)["action"] == "runner_active"
    popen.assert_not_called()


def test_night_stale_canonical_lock_is_reclaimed_and_process_completes(tmp_path: Path) -> None:
    pending_job = make_pending_job(tmp_path)
    pending_job.write_text("{}", encoding="utf-8")
    lock = pending_job.parent.parent / "nightly_runner.lock"
    lock.write_text("pid=999999999\n", encoding="utf-8")
    process = MagicMock()
    process.wait.return_value = 0
    with patch.object(runner, "nightly_window_is_open", return_value=True), patch.object(runner.subprocess, "Popen", return_value=process) as popen:
        assert runner.reconcile_night(project_root=tmp_path)["action"] == "completed"
    popen.assert_called_once_with(["python3", "scripts/nightly_runner.py", "--queue-dir", (tmp_path / "storage" / "nightly_jobs").as_posix()], cwd=tmp_path.as_posix())
    process.wait.assert_called_once_with()
    assert not lock.exists()


def test_night_nonzero_exit_is_error(tmp_path: Path) -> None:
    pending_job = make_pending_job(tmp_path)
    pending_job.write_text("{}", encoding="utf-8")
    process = MagicMock()
    process.wait.return_value = 1
    with patch.object(runner, "nightly_window_is_open", return_value=True), patch.object(runner.subprocess, "Popen", return_value=process):
        assert runner.reconcile_night(project_root=tmp_path)["action"] == "error"


def test_night_dry_run_does_not_execute_subprocess(tmp_path: Path) -> None:
    pending_job = make_pending_job(tmp_path)
    pending_job.write_text("{}", encoding="utf-8")
    with patch.object(runner, "nightly_window_is_open", return_value=True), patch.object(runner.nightly_runner, "acquire_lock") as acquire, patch.object(runner.subprocess, "Popen") as popen:
        assert runner.reconcile_night(project_root=tmp_path, dry_run=True)["action"] == "would_launch"
    acquire.assert_not_called()
    popen.assert_not_called()


def test_night_dry_run_with_lock_is_read_only(tmp_path: Path) -> None:
    pending_job = make_pending_job(tmp_path)
    pending_job.write_text("{}", encoding="utf-8")
    lock = pending_job.parent.parent / "nightly_runner.lock"
    lock.write_text("pid=999999999\n", encoding="utf-8")
    before_content = lock.read_bytes()
    before_mtime_ns = lock.stat().st_mtime_ns
    with patch.object(runner, "nightly_window_is_open", return_value=True), patch.object(runner.nightly_runner, "acquire_lock") as acquire, patch.object(runner.nightly_runner, "release_lock") as release, patch.object(runner.subprocess, "Popen") as popen:
        assert runner.reconcile_night(project_root=tmp_path, dry_run=True)["action"] == "lock_present"
    assert lock.read_bytes() == before_content
    assert lock.stat().st_mtime_ns == before_mtime_ns
    acquire.assert_not_called()
    release.assert_not_called()
    popen.assert_not_called()
