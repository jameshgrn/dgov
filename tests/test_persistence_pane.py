from __future__ import annotations

from pathlib import Path

import pytest

from dgov.persistence import (
    STATE_DIR,
    IllegalTransitionError,
    WorkerPane,
    _notify_dir,
    _notify_waiters,
    _wait_for_notify,
    add_pane,
    all_panes,
    clear_preserved_artifacts,
    emit_event,
    ensure_dag_tables,
    get_pane,
    get_preserved_artifacts,
    list_panes_slim,
    mark_preserved_artifacts,
    queue_dispatch,
    read_events,
    remove_pane,
    set_pane_metadata,
    take_dispatch_queue,
    update_pane_state,
)

pytestmark = pytest.mark.unit


def _make_session(tmp_path: Path) -> str:
    session = str(tmp_path / "session")
    Path(session).mkdir(parents=True, exist_ok=True)
    (Path(session) / STATE_DIR).mkdir(parents=True, exist_ok=True)
    return session


def _pane(slug: str, **kwargs) -> WorkerPane:
    defaults = {
        "prompt": "test",
        "pane_id": "%1",
        "agent": "pi",
        "project_root": "/tmp",
        "worktree_path": "/tmp/wt",
        "branch_name": slug,
    }
    defaults.update(kwargs)
    return WorkerPane(slug=slug, **defaults)


class TestPaneLifecycle:
    def test_add_and_get_pane(self, tmp_path):
        session = _make_session(tmp_path)
        pane = _pane("test-1", prompt="do stuff")
        add_pane(session, pane)
        result = get_pane(session, "test-1")
        assert result is not None
        assert result["slug"] == "test-1"
        assert result["agent"] == "pi"

    def test_get_nonexistent_pane(self, tmp_path):
        session = _make_session(tmp_path)
        assert get_pane(session, "nope") is None

    def test_state_transitions(self, tmp_path):
        session = _make_session(tmp_path)
        pane = _pane("trans-1", pane_id="%2")
        add_pane(session, pane)
        update_pane_state(session, "trans-1", "done")
        result = get_pane(session, "trans-1")
        assert result["state"] == "done"

    def test_invalid_transition_raises(self, tmp_path):
        session = _make_session(tmp_path)
        pane = _pane("bad-1", pane_id="%3")
        add_pane(session, pane)
        update_pane_state(session, "bad-1", "done")
        with pytest.raises(IllegalTransitionError):
            update_pane_state(session, "bad-1", "active")


class TestPaneMetadata:
    def test_set_and_get_metadata(self, tmp_path):
        session = _make_session(tmp_path)
        pane = _pane("meta-1", pane_id="%4")
        add_pane(session, pane)
        set_pane_metadata(session, "meta-1", landing=True)
        result = get_pane(session, "meta-1")
        assert result["landing"]

    def test_mark_and_clear_preserved_artifacts(self, tmp_path):
        session = _make_session(tmp_path)
        pane = _pane("pres-1", pane_id="%5")
        add_pane(session, pane)
        mark_preserved_artifacts(session, "pres-1", reason="test", recoverable=True, state="done")
        result = get_pane(session, "pres-1")
        artifacts = get_preserved_artifacts(result)
        assert artifacts is not None
        assert artifacts["reason"] == "test"
        clear_preserved_artifacts(session, "pres-1")
        result = get_pane(session, "pres-1")
        assert get_preserved_artifacts(result) is None


class TestRemovePane:
    def test_remove_pane_deletes_record(self, tmp_path):
        session = _make_session(tmp_path)
        pane = _pane("rm-1", pane_id="%6")
        add_pane(session, pane)
        remove_pane(session, "rm-1")
        assert get_pane(session, "rm-1") is None


class TestPaneListing:
    def test_list_panes_slim_returns_minimal_fields(self, tmp_path):
        session = _make_session(tmp_path)
        pane = _pane("list-1", prompt="long prompt", pane_id="%7")
        add_pane(session, pane)
        slim = list_panes_slim(session)
        assert len(slim) == 1
        assert slim[0]["slug"] == "list-1"

    def test_all_panes_returns_full_records(self, tmp_path):
        session = _make_session(tmp_path)
        pane = _pane("full-1", prompt="test prompt", pane_id="%8")
        add_pane(session, pane)
        full = all_panes(session)
        assert len(full) == 1
        assert full[0]["prompt"] == "test prompt"


class TestPaneEvents:
    def test_emit_and_read_events_round_trip(self, tmp_path):
        session = _make_session(tmp_path)
        ensure_dag_tables(session)
        emit_event(session, "pane_created", "ev-1", agent="pi")
        events = read_events(session)
        assert len(events) >= 1
        assert events[-1]["event"] == "pane_created"

    def test_queue_dispatch_emits_event_and_take_dispatch_queue_clears(self, tmp_path):
        session = _make_session(tmp_path)
        ensure_dag_tables(session)
        queue_dispatch(session, {"slug": "queued-1", "agent": "pi", "prompt": "do thing"})
        pending = take_dispatch_queue(session)
        assert len(pending) == 1
        assert pending[0]["slug"] == "queued-1"
        assert take_dispatch_queue(session) == []


class TestEventNotification:
    """Tests for the per-process pipe notification system."""

    def test_notify_dir_created(self, tmp_path):
        """_notify_dir creates the notify directory."""
        session = _make_session(tmp_path)
        d = _notify_dir(session)
        assert d.is_dir()

    def test_notify_waiters_no_reader_ok(self, tmp_path):
        """_notify_waiters doesn't error when no readers exist."""
        session = _make_session(tmp_path)
        _notify_waiters(session)

    def test_notify_waiters_no_dir_ok(self, tmp_path):
        """_notify_waiters doesn't error when notify dir doesn't exist yet."""
        _notify_waiters(str(tmp_path))

    def test_wait_for_notify_timeout(self, tmp_path):
        """_wait_for_notify returns False on timeout when no notification."""
        import time as _time

        session = _make_session(tmp_path)
        start = _time.monotonic()
        result = _wait_for_notify(session, 0.1)
        elapsed = _time.monotonic() - start
        assert result is False
        assert elapsed < 0.5

    def test_notify_wakes_waiter(self, tmp_path):
        """_notify_waiters wakes a blocked _wait_for_notify."""
        import threading
        import time as _time

        session = _make_session(tmp_path)
        result = {}

        def waiter():
            result["notified"] = _wait_for_notify(session, 5.0)
            result["time"] = _time.monotonic()

        start = _time.monotonic()
        t = threading.Thread(target=waiter)
        t.start()
        _time.sleep(0.2)
        _notify_waiters(session)
        t.join(timeout=3.0)

        assert result.get("notified") is True
        assert result["time"] - start < 2.0

    def test_emit_event_triggers_notification(self, tmp_path):
        """emit_event triggers notify pipes so waiters wake up."""
        import threading
        import time as _time

        session = _make_session(tmp_path)
        ensure_dag_tables(session)
        result = {}

        def waiter():
            result["notified"] = _wait_for_notify(session, 5.0)

        t = threading.Thread(target=waiter)
        t.start()
        _time.sleep(0.2)
        emit_event(session, "pane_done", "test-slug")
        t.join(timeout=3.0)

        assert result.get("notified") is True

    def test_multiple_waiters_all_wake(self, tmp_path):
        """Multiple _wait_for_notify calls in different threads all get woken."""
        import threading
        import time as _time

        session = _make_session(tmp_path)
        results = {}

        def waiter(name):
            results[name] = _wait_for_notify(session, 5.0)

        t1 = threading.Thread(target=waiter, args=("a",))
        t2 = threading.Thread(target=waiter, args=("b",))
        t1.start()
        t2.start()
        _time.sleep(0.2)
        _notify_waiters(session)
        t1.join(timeout=3.0)
        t2.join(timeout=3.0)

        assert results.get("a") is True
        assert results.get("b") is True
