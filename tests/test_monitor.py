"""Tests for dgov.monitor — 4B worker classification and monitoring."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from dgov.decision import (
    DecisionKind,
    DecisionRecord,
    MonitorOutputDecision,
    ProviderError,
    StaticDecisionProvider,
)

pytestmark = pytest.mark.unit


class TestClassifyOutput:
    """Test classify_output() 4B classification."""

    def _provider(self, classification: str) -> StaticDecisionProvider:
        return StaticDecisionProvider(
            classify_output_fn=lambda request: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="static-monitor",
                decision=MonitorOutputDecision(classification=classification),
            )
        )

    @patch("dgov.provider_registry.get_provider")
    def test_classify_working(self, mock_provider):
        from dgov.monitor import classify_output

        mock_provider.return_value = self._provider("working")
        assert classify_output("Reading src/dgov/agents.py") == "working"

    @patch("dgov.provider_registry.get_provider")
    def test_classify_done(self, mock_provider):
        from dgov.monitor import classify_output

        mock_provider.return_value = self._provider("done")
        # Use a string that doesn't hit DETERMINISTIC_PATTERNS
        result = classify_output("I have completed the requested changes and verified them.")
        assert result == "done"

    @patch("dgov.provider_registry.get_provider")
    def test_classify_stuck(self, mock_provider):
        from dgov.monitor import classify_output

        mock_provider.return_value = self._provider("stuck")
        # Use a string that doesn't hit DETERMINISTIC_PATTERNS
        assert (
            classify_output("I am trying to find the issue but I keep looking at the same files.")
            == "stuck"
        )

    @patch("dgov.provider_registry.get_provider")
    def test_classify_idle(self, mock_provider):
        from dgov.monitor import classify_output

        mock_provider.return_value = self._provider("idle")
        assert classify_output("$ ") == "idle"

    @patch("dgov.provider_registry.get_provider")
    def test_classify_fallback_on_error(self, mock_provider):
        from dgov.monitor import classify_output

        def _fail(request):  # noqa: ANN001
            raise ProviderError("4B unreachable")

        mock_provider.return_value = StaticDecisionProvider(classify_output_fn=_fail)
        assert classify_output("anything") == "unknown"

    @patch("dgov.provider_registry.get_provider")
    def test_classify_normalizes_response(self, mock_provider):
        from dgov.monitor import classify_output

        mock_provider.return_value = self._provider("working")
        assert classify_output("test") == "working"

    @patch("dgov.provider_registry.get_provider")
    def test_classify_invalid_response_returns_unknown(self, mock_provider):
        from dgov.monitor import classify_output

        mock_provider.return_value = self._provider("unknown")
        assert classify_output("test") == "unknown"

    def test_classify_empty_output_returns_idle(self):
        from dgov.monitor import classify_output

        assert classify_output("") == "idle"


class TestPollWorkers:
    """Test poll_workers() integration."""

    @patch("dgov.monitor.get_pane")
    @patch("dgov.monitor.tail_worker_log")
    @patch("dgov.monitor.list_worker_panes")
    @patch("dgov.monitor.classify_output")
    @patch("dgov.monitor._has_new_commits")
    def test_poll_active_workers(
        self, mock_commits, mock_classify, mock_list, mock_tail, mock_get_pane
    ):
        from dgov.monitor import poll_workers

        mock_get_pane.return_value = {"base_sha": "abc123"}
        mock_list.return_value = [
            {
                "slug": "w1",
                "agent": "claude",
                "state": "active",
                "alive": True,
                "project_root": "/tmp",
                "branch": "w1",
            },
        ]
        mock_tail.return_value = "Reading file..."
        mock_classify.return_value = "working"
        mock_commits.return_value = False
        result = poll_workers("/tmp")
        assert len(result) == 1
        assert result[0]["slug"] == "w1"
        assert result[0]["classification"] == "working"

    @patch("dgov.monitor.tail_worker_log")
    @patch("dgov.monitor.list_worker_panes")
    @patch("dgov.monitor.classify_output")
    @patch("dgov.monitor._has_new_commits")
    def test_poll_skips_done_panes(self, mock_commits, mock_classify, mock_list, mock_tail):
        from dgov.monitor import poll_workers

        mock_list.return_value = [
            {"slug": "w1", "agent": "pi", "state": "done", "alive": False},
        ]
        result = poll_workers("/tmp")
        assert len(result) == 0

    @patch("dgov.monitor.get_pane")
    @patch("dgov.monitor.tail_worker_log")
    @patch("dgov.monitor.list_worker_panes")
    @patch("dgov.monitor.classify_output")
    @patch("dgov.monitor._has_new_commits")
    def test_poll_empty_output_classifies_idle(
        self, mock_commits, mock_classify, mock_list, mock_tail, mock_get_pane
    ):
        from dgov.monitor import poll_workers

        mock_get_pane.return_value = {"base_sha": "abc"}
        mock_list.return_value = [
            {
                "slug": "w1",
                "agent": "claude",
                "state": "active",
                "alive": True,
                "project_root": "/tmp",
                "branch": "w1",
            },
        ]
        mock_tail.return_value = None
        mock_commits.return_value = False
        result = poll_workers("/tmp")
        assert result[0]["classification"] == "idle"
        mock_classify.assert_not_called()

    @patch("dgov.monitor.tail_worker_log")
    @patch("dgov.monitor.list_worker_panes")
    @patch("dgov.monitor.classify_output")
    @patch("dgov.monitor._has_new_commits")
    def test_poll_skips_landing_panes(self, mock_commits, mock_classify, mock_list, mock_tail):
        from dgov.monitor import poll_workers

        mock_list.return_value = [
            {
                "slug": "w1",
                "agent": "claude",
                "state": "active",
                "alive": True,
                "landing": True,
            },
        ]
        result = poll_workers("/tmp")
        assert len(result) == 0
        mock_classify.assert_not_called()


class TestTakeAction:
    """Test _take_action() decision engine."""

    @patch("dgov.monitor.get_pane", return_value={"state": "active"})
    @patch("dgov.monitor._auto_complete")
    def test_auto_complete_after_two_done(self, mock_complete, mock_get_pane):
        from dgov.monitor import _take_action

        history = {"w1": {"classifications": ["done", "done"], "last_action_at": 0}}
        worker = {"slug": "w1", "classification": "done", "has_commits": True}
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action is not None
        mock_complete.assert_called_once()

    @patch("dgov.monitor.get_pane", return_value={"state": "active"})
    @patch("dgov.monitor._nudge_stuck")
    def test_nudge_after_three_stuck(self, mock_nudge, mock_get_pane):
        from dgov.monitor import _take_action

        history = {"w1": {"classifications": ["stuck", "stuck", "stuck"], "last_action_at": 0}}
        worker = {"slug": "w1", "classification": "stuck", "has_commits": False}
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action is not None
        mock_nudge.assert_called_once()

    @patch("dgov.monitor.get_pane", return_value={"state": "active"})
    @patch("dgov.monitor._mark_idle_failed")
    def test_idle_timeout_after_four(self, mock_fail, mock_get_pane):
        from dgov.monitor import _take_action

        history = {
            "w1": {"classifications": ["idle", "idle", "idle", "idle"], "last_action_at": 0}
        }
        worker = {"slug": "w1", "classification": "idle", "has_commits": False}
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action is not None
        mock_fail.assert_called_once()

    def test_no_action_for_working(self):
        from dgov.monitor import _take_action

        history = {"w1": {"classifications": ["working"], "last_action_at": 0}}
        worker = {"slug": "w1", "classification": "working", "has_commits": False}
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action is None

    def test_new_slug_initializes_history(self):
        from dgov.monitor import _take_action

        history = {}
        worker = {"slug": "new-w", "classification": "working", "has_commits": False}
        _take_action("/tmp", "/tmp", worker, history)
        assert "new-w" in history
        assert "working" in history["new-w"]["classifications"]

    @patch("dgov.monitor.get_pane", return_value={"state": "active"})
    @patch("dgov.monitor._auto_complete")
    def test_done_with_commits_acts_immediately(self, mock_complete, mock_get_pane):
        from dgov.monitor import _take_action

        worker = {"slug": "w1", "classification": "done", "has_commits": True, "is_alive": True}
        # consecutive = 1 (current classification is 'done')
        history = {"w1": {"classifications": [], "last_action_at": 0}}
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action == "auto_complete"
        mock_complete.assert_called_once()

    @patch("dgov.monitor.get_pane")
    @patch("dgov.executor.run_complete_pane")
    @patch("dgov.monitor._has_new_commits", return_value=True)
    def test_auto_complete_touches_done_signal(
        self, mock_has_commits, mock_complete, mock_get_pane, tmp_path
    ):
        from dgov.executor import StateTransitionResult
        from dgov.monitor import _auto_complete

        mock_get_pane.return_value = {
            "slug": "w1",
            "branch_name": "w1",
            "base_sha": "abc123",
            "project_root": str(tmp_path),
        }
        mock_complete.return_value = StateTransitionResult(
            slug="w1", new_state="done", changed=True
        )
        (tmp_path / ".dgov" / "done").mkdir(parents=True)
        _auto_complete(str(tmp_path), str(tmp_path), "w1")
        assert (tmp_path / ".dgov" / "done" / "w1").exists()
        mock_complete.assert_called_once()
        mock_has_commits.assert_called_once_with(str(tmp_path), "w1", "abc123")

    @patch("dgov.monitor.get_pane")
    @patch("dgov.executor.run_complete_pane")
    @patch("dgov.monitor._has_new_commits", return_value=False)
    def test_auto_complete_skips_done_signal_without_commits(
        self, mock_has_commits, mock_complete, mock_get_pane, tmp_path
    ):
        from dgov.monitor import _auto_complete

        mock_get_pane.return_value = {
            "slug": "w1",
            "branch_name": "w1",
            "base_sha": "abc123",
            "project_root": str(tmp_path),
        }
        (tmp_path / ".dgov" / "done").mkdir(parents=True)
        _auto_complete(str(tmp_path), str(tmp_path), "w1")
        assert not (tmp_path / ".dgov" / "done" / "w1").exists()
        mock_complete.assert_not_called()
        mock_has_commits.assert_called_once_with(str(tmp_path), "w1", "abc123")


class TestRunMonitor:
    """Test run_monitor() loop."""

    @patch("dgov.monitor._take_action")
    @patch("dgov.monitor.poll_workers")
    def test_dry_run_writes_status(self, mock_poll, mock_action, tmp_path):
        from dgov.monitor import run_monitor

        mock_poll.return_value = [
            {
                "slug": "w1",
                "agent": "pi",
                "classification": "working",
                "has_commits": False,
                "output_preview": "test",
            }
        ]
        mock_action.return_value = None
        run_monitor(str(tmp_path), dry_run=True)
        status_file = tmp_path / ".dgov" / "monitor" / "status.json"
        assert status_file.exists()
        import json

        data = json.loads(status_file.read_text())
        assert "workers" in data
        assert "actions" in data

    @patch("dgov.monitor.latest_event_id", return_value=12)
    @patch("dgov.monitor._wait_for_monitor_wakeup", side_effect=KeyboardInterrupt)
    @patch("dgov.monitor._take_action")
    @patch("dgov.monitor.poll_workers")
    def test_non_dry_run_waits_on_event_boundary(
        self, mock_poll, mock_action, mock_wait, mock_latest_event_id, tmp_path
    ):
        from dgov.monitor import run_monitor

        mock_poll.return_value = []
        mock_action.return_value = None

        run_monitor(str(tmp_path), dry_run=False, poll_interval=7)

        mock_wait.assert_called_once_with(str(tmp_path), str(tmp_path), 12, 7)
        assert mock_latest_event_id.call_count >= 1

    def test_ensure_monitor_running_uses_flock_and_spawns(self, tmp_path, monkeypatch):
        from dgov.monitor import ensure_monitor_running

        project_root = tmp_path / "project"
        session_root = tmp_path / "session"
        project_root.mkdir()
        session_root.mkdir()

        captured: dict[str, object] = {}

        class DummyProc:
            pid = 4321

        def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN201
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return DummyProc()

        monkeypatch.setattr(
            "shutil.which",
            lambda exe: "/opt/homebrew/bin/uv" if exe == "uv" else None,
        )
        monkeypatch.setattr("subprocess.Popen", fake_popen)

        # No lock held — should spawn
        ensure_monitor_running(str(project_root), session_root=str(session_root))

        assert captured["cmd"] == [
            "uv",
            "run",
            "dgov",
            "monitor",
            "-r",
            str(project_root.resolve()),
            "--session-root",
            str(session_root.resolve()),
        ]
        assert captured["kwargs"]["cwd"] == str(project_root.resolve())

    def test_ensure_monitor_running_skips_when_locked(self, tmp_path, monkeypatch):
        import fcntl

        from dgov.monitor import _source_hash, ensure_monitor_running

        project_root = tmp_path / "project"
        session_root = tmp_path / "session"
        project_root.mkdir()
        session_root.mkdir()
        (session_root / ".dgov").mkdir(parents=True)

        # Hold the lock to simulate a running monitor with current version
        lock_path = session_root / ".dgov" / "monitor.lock"
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(f"12345\n0.9.0\n{_source_hash()}")
        lock_fd.flush()

        captured: dict[str, object] = {}

        def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN201
            captured["cmd"] = cmd
            return type("P", (), {"pid": 9999})()

        monkeypatch.setattr("subprocess.Popen", fake_popen)

        # Lock held with current hash — should NOT spawn
        ensure_monitor_running(str(project_root), session_root=str(session_root))
        assert "cmd" not in captured

        lock_fd.close()

    def test_ensure_monitor_kills_stale_monitor(self, tmp_path, monkeypatch):
        import fcntl

        from dgov.monitor import ensure_monitor_running

        project_root = tmp_path / "project"
        session_root = tmp_path / "session"
        project_root.mkdir()
        session_root.mkdir()
        (session_root / ".dgov").mkdir(parents=True)

        # Write stale hash to lock file, then hold the lock
        lock_path = session_root / ".dgov" / "monitor.lock"
        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write("12345\n0.8.0\nstale_hash_value")
        lock_fd.flush()

        killed_pids: list[int] = []
        spawned: dict[str, object] = {}

        import os as _os

        def fake_kill(pid, sig):  # noqa: ANN001, ANN201
            killed_pids.append(pid)

        def fake_popen(cmd, **kwargs):  # noqa: ANN001, ANN201
            spawned["cmd"] = cmd
            return type("P", (), {"pid": 9999})()

        monkeypatch.setattr(_os, "kill", fake_kill)
        monkeypatch.setattr("subprocess.Popen", fake_popen)
        # Make sleep a no-op
        monkeypatch.setattr("time.sleep", lambda _: None)

        # Keep lock held — ensure_monitor_running should detect stale hash,
        # kill the pid, and spawn a replacement
        try:
            ensure_monitor_running(str(project_root), session_root=str(session_root))
            assert 12345 in killed_pids
            assert "cmd" in spawned
        finally:
            lock_fd.close()


class TestNudgeStuck:
    """Test _nudge_stuck edge cases."""

    @patch("dgov.monitor.get_pane")
    def test_nudge_no_pane(self, mock_get_pane):
        from dgov.monitor import _nudge_stuck

        mock_get_pane.return_value = None
        _nudge_stuck("/tmp", "/tmp", "missing-slug")
        # Should return early without sending input


class TestClassifyDeterministic:
    """Test _classify_deterministic() rule-based classification."""

    def test_todo_no_longer_waiting(self):
        from dgov.monitor import _classify_deterministic

        output = "# TODO: implement this"
        assert _classify_deterministic(output) != "waiting_input"

    def test_done_precedence(self):
        from dgov.monitor import _classify_deterministic

        output = "I have committed the changes. Task is done."
        assert _classify_deterministic(output) == "done"

    def test_stuck_regex(self):
        from dgov.monitor import _classify_deterministic

        assert _classify_deterministic("Error: module not found") == "stuck"
        assert _classify_deterministic("Exception in thread main") == "stuck"
        assert _classify_deterministic("Panic! at the disco") == "stuck"


class TestTakeActionBugFixes:
    """Test _take_action() bug fixes and new features."""

    @patch("dgov.monitor.get_pane", return_value={"state": "active"})
    @patch("dgov.monitor._auto_complete")
    def test_done_no_commits_auto_completes(self, mock_complete, mock_get_pane):
        from dgov.monitor import _take_action

        worker = {"slug": "w1", "classification": "done", "has_commits": False}
        history = {"w1": {"classifications": ["done", "done"], "last_action_at": 0}}
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action == "auto_complete"

    @patch("dgov.monitor.emit_event")
    @patch("dgov.monitor.get_pane", return_value={"state": "active"})
    def test_blocked_event_after_three_waiting_input(self, mock_get_pane, mock_emit):
        from dgov.monitor import _take_action

        worker = {"slug": "w1", "classification": "waiting_input", "has_commits": False}
        history = {
            "w1": {"classifications": ["waiting_input", "waiting_input"], "last_action_at": 0}
        }
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action == "blocked_event"
        mock_emit.assert_called_with("/tmp", "monitor_blocked", "w1", reason="waiting_input")

    @patch("dgov.monitor.get_pane", return_value={"state": "active"})
    @patch("dgov.monitor._auto_complete")
    def test_stale_worker_with_commits_auto_completes(self, mock_complete, mock_get_pane):
        from dgov.monitor import _take_action

        worker = {"slug": "w1", "classification": "idle", "has_commits": True, "is_alive": False}
        history = {"w1": {"classifications": ["idle"], "last_action_at": 0}}
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action == "stale_auto_complete"
        mock_complete.assert_called_once()

    @patch("dgov.monitor.get_pane", return_value={"state": "active"})
    @patch("dgov.monitor._mark_idle_failed")
    def test_stale_worker_no_commits_fails(self, mock_fail, mock_get_pane):
        from dgov.monitor import _take_action

        worker = {"slug": "w1", "classification": "idle", "has_commits": False, "is_alive": False}
        history = {"w1": {"classifications": ["idle"], "last_action_at": 0}}
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action == "stale_fail"
        mock_fail.assert_called_once()


class TestTryAutoMerge:
    """Test _try_auto_merge auto-merge logic."""

    @patch("dgov.executor.run_land_only")
    def test_auto_merge_safe_verdict(self, mock_run_land_only):
        from dgov.monitor import _try_auto_merge

        mock_run_land_only.return_value = type(
            "R",
            (),
            {
                "review": {"verdict": "safe", "slug": "test-1", "commit_count": 1},
                "merge_result": {"merged": "test-1", "branch": "dgov-test-1"},
                "error": None,
            },
        )()
        result = _try_auto_merge("/tmp/proj", "/tmp/proj", "test-1")
        assert result == "auto_merge"
        mock_run_land_only.assert_called_once()

    @patch("dgov.executor.run_land_only")
    def test_auto_merge_review_verdict(self, mock_run_land_only):
        from dgov.monitor import _try_auto_merge

        mock_run_land_only.return_value = type(
            "R",
            (),
            {
                "review": {"verdict": "review", "issues": ["uncommitted changes"]},
                "merge_result": None,
                "failure_stage": "review_failed",
                "error": "Review verdict is review; refusing to merge",
            },
        )()
        result = _try_auto_merge("/tmp/proj", "/tmp/proj", "test-1")
        assert result is None

    @patch("dgov.executor.run_land_only")
    def test_auto_merge_review_error(self, mock_run_land_only):
        from dgov.monitor import _try_auto_merge

        mock_run_land_only.return_value = type(
            "R",
            (),
            {
                "review": {"error": "Pane not found"},
                "merge_result": None,
                "failure_stage": "review_error",
                "error": "Pane not found",
            },
        )()
        result = _try_auto_merge("/tmp/proj", "/tmp/proj", "test-1")
        assert result is None

    @patch("dgov.monitor._try_auto_merge", return_value="auto_merge")
    @patch("dgov.monitor.get_pane", return_value={"slug": "test-1", "state": "done"})
    def test_process_auto_merge_candidates_clears_active_slug(
        self,
        mock_get_pane,
        mock_try_merge,
    ):
        from dgov.monitor import MonitorLoopState, _process_auto_merge_candidates

        state = MonitorLoopState(
            event_cursor=0,
            active_slugs={"test-1"},
            merge_candidates={"test-1"},
        )

        actions = _process_auto_merge_candidates("/tmp/proj", "/tmp/proj", state, set())

        assert actions == [{"slug": "test-1", "action": "auto_merge"}]
        assert "test-1" not in state.merge_candidates
        assert "test-1" not in state.active_slugs


class TestMonitorWakeup:
    def test_wait_for_monitor_wakeup_uses_event_waiter(self):
        from dgov.monitor import _MONITOR_WAKE_EVENTS, _wait_for_monitor_wakeup

        with (
            patch("dgov.monitor.wait_for_events", return_value=[{"id": 13}]) as mock_wait,
            patch("pathlib.Path.is_dir", return_value=True),
        ):
            events = _wait_for_monitor_wakeup("/tmp/proj", "/tmp/proj", 12, 9)

        assert events == [{"id": 13}]
        mock_wait.assert_called_once_with(
            "/tmp/proj",
            after_id=12,
            event_types=_MONITOR_WAKE_EVENTS,
            timeout_s=9.0,
        )


class TestMonitorEventState:
    def test_apply_monitor_events_tracks_candidates_incrementally(self):
        from dgov.monitor import MonitorLoopState, _apply_monitor_events

        state = MonitorLoopState(event_cursor=4)
        events = [
            {"id": 5, "event": "dispatch_queued", "pane": "dispatch-queue"},
            {"id": 6, "event": "pane_created", "pane": "worker-1"},
            {"id": 7, "event": "pane_done", "pane": "worker-1"},
            {"id": 8, "event": "pane_failed", "pane": "worker-2"},
            {"id": 9, "event": "pane_review_pending", "pane": "worker-1"},
        ]

        _apply_monitor_events(
            "/project",
            "/session",
            state,
            events,
            auto_merge=True,
            auto_retry=True,
        )

        assert state.event_cursor == 9
        assert state.queue_dirty is True
        assert "worker-1" not in state.active_slugs
        assert "worker-1" not in state.merge_candidates
        assert "worker-2" in state.retry_candidates


class TestApplyDagEvents:
    """Regression test: dag_run_id must be read from event dict, not parsed data blob."""

    def test_dag_started_reads_run_id_from_event(self, monkeypatch):
        from dgov.monitor import MonitorLoopState, _apply_monitor_events

        # Simulate event dict as returned by wait_for_events:
        # data fields are merged into the top-level dict, no "data" key.
        events = [
            {"id": 10, "event": "dag_started", "pane": "dag/99", "dag_run_id": 99},
        ]
        state = MonitorLoopState(event_cursor=9)

        # Mock _load_dag_run to track if it was called
        loaded = []
        monkeypatch.setattr(
            "dgov.persistence.get_dag_run",
            lambda sr, rid: {"id": rid, "status": "running"} if rid == 99 else None,
        )
        monkeypatch.setattr(
            "dgov.monitor._load_dag_run",
            lambda pr, sr, run: loaded.append(run["id"]) or "fake_state",
        )

        _apply_monitor_events("/p", "/s", state, events, auto_merge=False, auto_retry=False)

        assert loaded == [99], "dag_started must trigger _load_dag_run with the correct run_id"
        assert 99 in state.active_dags

    def test_dag_completed_reads_run_id_from_event(self):
        from dgov.monitor import MonitorLoopState, _apply_monitor_events

        events = [
            {"id": 11, "event": "dag_completed", "pane": "dag/99", "dag_run_id": 99},
        ]
        state = MonitorLoopState(event_cursor=10)
        state.active_dags[99] = "placeholder"

        _apply_monitor_events("/p", "/s", state, events, auto_merge=False, auto_retry=False)

        assert 99 not in state.active_dags, "dag_completed must remove the run from active_dags"


class TestTryAutoRetry:
    """Test _try_auto_retry auto-retry logic."""

    @patch("dgov.executor.run_retry_or_escalate")
    def test_auto_retry_success(self, mock_retry):
        from dgov.executor import RetryResult
        from dgov.monitor import _try_auto_retry

        mock_retry.return_value = RetryResult(slug="test-1", new_slug="test-1-2")
        result = _try_auto_retry("/tmp/proj", "/tmp/proj", "test-1")
        assert result == "auto_retry"

    @patch("dgov.executor.run_retry_or_escalate")
    def test_auto_escalate(self, mock_retry):
        from dgov.executor import EscalateResult
        from dgov.monitor import _try_auto_retry

        mock_retry.return_value = EscalateResult(
            slug="test-1", new_slug="test-1-esc-1", target_agent="qwen35-122b"
        )
        result = _try_auto_retry("/tmp/proj", "/tmp/proj", "test-1")
        assert result == "auto_escalate"

    @patch("dgov.executor.run_retry_or_escalate")
    def test_no_retry_policy(self, mock_retry):
        from dgov.executor import RetryResult
        from dgov.monitor import _try_auto_retry

        mock_retry.return_value = RetryResult(
            slug="test-1", error="No retry/escalation action taken"
        )
        result = _try_auto_retry("/tmp/proj", "/tmp/proj", "test-1")
        assert result is None

    @patch("dgov.monitor.read_events", return_value=[])
    @patch("dgov.monitor.get_pane", return_value={"superseded_by": "test-1-2"})
    def test_resolve_retry_successor_prefers_pane_metadata(self, mock_get_pane, mock_read_events):
        from dgov.monitor import _resolve_retry_successor_slug

        result = _resolve_retry_successor_slug("/tmp/proj", "test-1")

        assert result == "test-1-2"
        mock_read_events.assert_not_called()

    @patch("dgov.monitor.read_events", return_value=[{"new_slug": "test-1-2"}])
    @patch("dgov.monitor.get_pane", return_value={})
    def test_resolve_retry_successor_falls_back_to_events(self, mock_get_pane, mock_read_events):
        from dgov.monitor import _resolve_retry_successor_slug

        result = _resolve_retry_successor_slug("/tmp/proj", "test-1")

        assert result == "test-1-2"
        mock_read_events.assert_called_once_with("/tmp/proj", slug="test-1", limit=5)

    @patch("dgov.monitor._try_auto_retry", return_value="auto_retry")
    @patch("dgov.monitor.read_events", return_value=[])
    @patch("dgov.monitor.get_pane")
    def test_process_auto_retry_candidates_tracks_new_active_slug(
        self,
        mock_get_pane,
        mock_read_events,
        mock_try_retry,
    ):
        from dgov.monitor import MonitorLoopState, _process_auto_retry_candidates

        mock_get_pane.side_effect = [
            {"slug": "test-1", "state": "failed"},
            {"superseded_by": "test-1-2"},
        ]
        state = MonitorLoopState(
            event_cursor=0,
            retry_candidates={"test-1"},
        )

        actions = _process_auto_retry_candidates("/tmp/proj", "/tmp/proj", state, set())

        assert actions == [{"slug": "test-1", "action": "auto_retry"}]
        assert "test-1" not in state.retry_candidates
        assert "test-1-2" in state.active_slugs


class TestRunMonitorAutoMergePolicy:
    """Test run_monitor() explicit auto-merge policy."""

    @patch("dgov.monitor.poll_workers")
    @patch("dgov.monitor._take_action")
    def test_dry_run_does_not_attempt_auto_merge(self, mock_action, mock_poll, tmp_path):
        """Dry-run mode should exit before attempting any auto-merge.

        When dry_run=True, run_monitor returns immediately after writing status,
        before entering the auto-merge/auto-retry loop.
        """
        from dgov.monitor import run_monitor

        mock_poll.return_value = []
        mock_action.return_value = None

        # Patch all_panes to return a done pane that would trigger auto-merge
        with patch("dgov.monitor.all_panes") as mock_all_panes:
            mock_all_panes.return_value = [
                {
                    "slug": "done-worker",
                    "state": "done",
                    "branch_name": "done-worker",
                    "base_sha": "abc123",
                    "project_root": str(tmp_path),
                }
            ]

            # Patch _try_auto_merge to detect if it was called
            with patch("dgov.monitor._try_auto_merge") as mock_try_merge:
                mock_try_merge.return_value = "auto_merge"
                run_monitor(str(tmp_path), dry_run=True)

                # Verify _try_auto_merge was never called during dry-run
                mock_try_merge.assert_not_called()

    @patch("dgov.monitor.poll_workers")
    @patch("dgov.monitor._take_action")
    def test_auto_merge_true_attempts_merge(self, mock_action, mock_poll, tmp_path):
        """When auto_merge=True, run_monitor should attempt auto-merge on done panes."""
        from dgov.monitor import run_monitor

        mock_poll.return_value = []
        mock_action.return_value = None

        with patch("dgov.monitor.all_panes") as mock_all_panes:
            mock_all_panes.return_value = [
                {
                    "slug": "done-worker",
                    "state": "done",
                    "branch_name": "done-worker",
                    "base_sha": "abc123",
                    "project_root": str(tmp_path),
                }
            ]

            with (
                patch(
                    "dgov.monitor.get_pane",
                    return_value={"slug": "done-worker", "state": "done"},
                ),
                patch("dgov.monitor._try_auto_merge") as mock_try_merge,
            ):
                mock_try_merge.return_value = "auto_merge"
                # Call with auto_merge=True (the default)
                run_monitor(str(tmp_path), dry_run=True, auto_merge=True)

                # Verify _try_auto_merge WAS called
                mock_try_merge.assert_called_once_with(str(tmp_path), str(tmp_path), "done-worker")

    @patch("dgov.monitor.poll_workers")
    @patch("dgov.monitor._take_action")
    def test_auto_merge_false_skips_merge(self, mock_action, mock_poll, tmp_path):
        """When auto_merge=False, run_monitor should skip auto-merge entirely."""
        from dgov.monitor import run_monitor

        mock_poll.return_value = []
        mock_action.return_value = None

        with patch("dgov.monitor.all_panes") as mock_all_panes:
            mock_all_panes.return_value = [
                {
                    "slug": "done-worker",
                    "state": "done",
                    "branch_name": "done-worker",
                    "base_sha": "abc123",
                    "project_root": str(tmp_path),
                }
            ]

            with patch("dgov.monitor._try_auto_merge") as mock_try_merge:
                mock_try_merge.return_value = "auto_merge"
                # Call with auto_merge=False
                run_monitor(str(tmp_path), dry_run=True, auto_merge=False)

                # Verify _try_auto_merge was never called
                mock_try_merge.assert_not_called()


class TestRecordFastFailure:
    """Test _record_fast_failure backend failure recording."""

    @patch("dgov.monitor.get_pane")
    @patch("dgov.router.record_backend_failure")
    def test_record_fast_failure_calls_circuit_breaker(self, mock_record, mock_get_pane, tmp_path):
        """When a pane dies within 60s, _record_fast_failure records a backend failure."""
        import time

        from dgov.monitor import _record_fast_failure

        # Create a pane record that was created 30 seconds ago
        mock_get_pane.return_value = {"agent": "test-backend", "created_at": time.time() - 30}

        recorded = []
        mock_record.side_effect = lambda sr, agent: recorded.append(agent)

        _record_fast_failure(str(tmp_path), "test-slug")
        assert recorded == ["test-backend"]
        mock_get_pane.assert_called_once_with(str(tmp_path), "test-slug")

    @patch("dgov.monitor.get_pane")
    @patch("dgov.router.record_backend_failure")
    def test_record_fast_failure_skips_old_panes(self, mock_record, mock_get_pane, tmp_path):
        """Panes older than 60s should not trigger circuit breaker recording."""
        import time

        from dgov.monitor import _record_fast_failure

        mock_get_pane.return_value = {"agent": "test-backend", "created_at": time.time() - 120}

        recorded = []
        mock_record.side_effect = lambda sr, agent: recorded.append(agent)

        _record_fast_failure(str(tmp_path), "test-slug")
        assert recorded == []
        mock_record.assert_not_called()

    @patch("dgov.monitor.get_pane", return_value=None)
    def test_record_fast_failure_skips_missing_pane(self, mock_get_pane, tmp_path):
        """Missing pane records should be skipped."""
        from dgov.monitor import _record_fast_failure

        with patch("dgov.router.record_backend_failure") as mock_record:
            _record_fast_failure(str(tmp_path), "missing-slug")
            mock_record.assert_not_called()

    @patch("dgov.monitor.get_pane", return_value={})
    def test_record_fast_failure_skips_missing_fields(self, mock_get_pane, tmp_path):
        """Pane records without agent or created_at should be skipped."""
        from dgov.monitor import _record_fast_failure

        with patch("dgov.router.record_backend_failure") as mock_record:
            _record_fast_failure(str(tmp_path), "incomplete-slug")
            mock_record.assert_not_called()

    @patch("dgov.monitor.get_pane", return_value={"agent": "", "created_at": 0.0})
    def test_record_fast_failure_empty_agent_id(self, mock_get_pane, tmp_path):
        """Empty agent ID should be skipped."""
        from dgov.monitor import _record_fast_failure

        with patch("dgov.router.record_backend_failure") as mock_record:
            _record_fast_failure(str(tmp_path), "empty-agent-slug")
            mock_record.assert_not_called()

    @patch("dgov.monitor.get_pane", return_value={"agent": "test", "created_at": "invalid"})
    def test_record_fast_failure_invalid_created_at(self, mock_get_pane, tmp_path):
        """Invalid created_at value should be skipped via exception handling."""
        from dgov.monitor import _record_fast_failure

        with patch("dgov.router.record_backend_failure") as mock_record:
            _record_fast_failure(str(tmp_path), "invalid-timestamp-slug")
            mock_record.assert_not_called()
