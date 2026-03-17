"""Tests for dgov.monitor — 4B worker classification and monitoring."""

from __future__ import annotations

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestClassifyOutput:
    """Test classify_output() 4B classification."""

    @patch("dgov.monitor.chat_completion_local_first")
    def test_classify_working(self, mock_llm):
        from dgov.monitor import classify_output

        mock_llm.return_value = {"choices": [{"message": {"content": "working"}}]}
        assert classify_output("Reading src/dgov/agents.py") == "working"

    @patch("dgov.monitor.chat_completion_local_first")
    def test_classify_done(self, mock_llm):
        from dgov.monitor import classify_output

        mock_llm.return_value = {"choices": [{"message": {"content": "done"}}]}
        # Use a string that doesn't hit DETERMINISTIC_PATTERNS
        assert classify_output("I have completed the requested changes and verified them.") == "done"

    @patch("dgov.monitor.chat_completion_local_first")
    def test_classify_stuck(self, mock_llm):
        from dgov.monitor import classify_output

        mock_llm.return_value = {"choices": [{"message": {"content": "stuck"}}]}
        # Use a string that doesn't hit DETERMINISTIC_PATTERNS
        assert classify_output("I am trying to find the issue but I keep looking at the same files.") == "stuck"

    @patch("dgov.monitor.chat_completion_local_first")
    def test_classify_idle(self, mock_llm):
        from dgov.monitor import classify_output

        mock_llm.return_value = {"choices": [{"message": {"content": "idle"}}]}
        assert classify_output("$ ") == "idle"

    @patch("dgov.monitor.chat_completion_local_first")
    def test_classify_fallback_on_error(self, mock_llm):
        from dgov.monitor import classify_output

        mock_llm.side_effect = RuntimeError("4B unreachable")
        assert classify_output("anything") == "unknown"

    @patch("dgov.monitor.chat_completion_local_first")
    def test_classify_normalizes_response(self, mock_llm):
        from dgov.monitor import classify_output

        mock_llm.return_value = {"choices": [{"message": {"content": "  Working  \n"}}]}
        assert classify_output("test") == "working"

    @patch("dgov.monitor.chat_completion_local_first")
    def test_classify_invalid_response_returns_unknown(self, mock_llm):
        from dgov.monitor import classify_output

        mock_llm.return_value = {
            "choices": [{"message": {"content": "I think the agent is working"}}]
        }
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


class TestAutoComplete:
    """Test _auto_complete() action."""

    @patch("dgov.monitor.emit_event")
    @patch("dgov.monitor.update_pane_state")
    def test_auto_complete_touches_done_signal(self, mock_update, mock_event, tmp_path):
        from dgov.monitor import _auto_complete

        (tmp_path / ".dgov" / "done").mkdir(parents=True)
        _auto_complete(str(tmp_path), str(tmp_path), "w1")
        assert (tmp_path / ".dgov" / "done" / "w1").exists()
        mock_update.assert_called_once()
        mock_event.assert_called_once()


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


class TestTakeActionBugFixes:
    """Test _take_action() bug fixes from monitor-bogs."""

    @patch("dgov.monitor.get_pane", return_value={"state": "active"})
    @patch("dgov.monitor._auto_complete")
    def test_done_no_commits_auto_completes(self, mock_complete, mock_get_pane):
        from dgov.monitor import _take_action

        worker = {"slug": "w1", "classification": "done", "has_commits": False}
        history = {"w1": {"classifications": ["done", "done"], "last_action_at": 0}}
        action = _take_action("/tmp", "/tmp", worker, history)
        assert action == "auto_complete"
