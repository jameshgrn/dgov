from unittest.mock import patch

from dgov.monitor import _classify_deterministic, _take_action


def test_todo_no_longer_waiting():
    output = "# TODO: implement this"
    assert _classify_deterministic(output) != "waiting_input"


def test_done_precedence():
    output = "I have committed the changes. Task is done."
    assert _classify_deterministic(output) == "done"


def test_done_no_commits_auto_completes():
    worker = {"slug": "w1", "classification": "done", "has_commits": False}
    history = {"w1": {"classifications": ["done", "done"], "last_action_at": 0}}
    with patch("dgov.monitor.get_pane", return_value={"state": "active"}):
        with patch("dgov.monitor._auto_complete"):
            action = _take_action("/tmp", "/tmp", worker, history)
            assert action == "auto_complete"
