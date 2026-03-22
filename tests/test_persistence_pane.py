from __future__ import annotations

from pathlib import Path

import pytest

from dgov.persistence import (
    IllegalTransitionError,
    WorkerPane,
    _get_db,
    add_pane,
    all_panes,
    clear_preserved_artifacts,
    emit_event,
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
    """Create a temp session root with initialized DB."""
    session = tmp_path / "pane-session"
    session.mkdir(parents=True, exist_ok=True)
    # Initialize the SQLite state DB (creates tables).
    _get_db(str(session))
    return str(session)


def _make_pane(slug: str, session_root: str) -> WorkerPane:
    """Helper to construct a basic WorkerPane."""
    return WorkerPane(
        slug=slug,
        prompt="Do something important",
        pane_id="%1",
        agent="pi",
        project_root=session_root,
        worktree_path=str(Path(session_root) / ".dgov" / "worktrees" / slug),
        branch_name=slug,
    )


class TestRegisterAndGetPane:
    def test_register_creates_and_get_retrieves(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _make_pane("pane-1", session)

        add_pane(session, pane)
        result = get_pane(session, "pane-1")

        assert result is not None
        assert result["slug"] == "pane-1"
        assert result["pane_id"] == "%1"

    def test_duplicate_slug_replaces_existing(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane1 = _make_pane("pane-dup", session)
        pane2 = _make_pane("pane-dup", session)
        pane2.pane_id = "%2"
        pane2.prompt = "Updated prompt"

        add_pane(session, pane1)
        add_pane(session, pane2)

        panes = all_panes(session)
        assert len(panes) == 1
        assert panes[0]["pane_id"] == "%2"
        assert panes[0]["prompt"] == "Updated prompt"


class TestUpdatePaneState:
    def test_valid_state_transition_succeeds(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _make_pane("pane-state", session)
        add_pane(session, pane)

        # active -> closed is a valid transition and is terminal,
        # so no backend title update is attempted.
        update_pane_state(session, "pane-state", "closed")

        updated = get_pane(session, "pane-state")
        assert updated is not None
        assert updated["state"] == "closed"

    def test_invalid_state_transition_raises(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _make_pane("pane-bad-transition", session)
        add_pane(session, pane)

        # active -> reviewed_pass is not a legal transition.
        with pytest.raises(IllegalTransitionError):
            update_pane_state(session, "pane-bad-transition", "reviewed_pass")


class TestPaneMetadata:
    def test_set_pane_metadata_typed_columns(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _make_pane("pane-meta", session)
        add_pane(session, pane)

        set_pane_metadata(session, "pane-meta", landing=1, retry_count=3)

        updated = get_pane(session, "pane-meta")
        assert updated is not None
        assert updated["landing"] == 1
        assert updated["retry_count"] == 3

    def test_set_pane_metadata_rejects_unknown_keys(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _make_pane("pane-meta", session)
        add_pane(session, pane)

        with pytest.raises(ValueError, match="unknown key"):
            set_pane_metadata(session, "pane-meta", bogus_key="nope")

    def test_mark_and_clear_preserved_artifacts(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _make_pane("pane-meta", session)
        add_pane(session, pane)

        mark_preserved_artifacts(
            session,
            "pane-meta",
            reason="review_pending",
            recoverable=False,
            state="review_pending",
        )

        updated = get_pane(session, "pane-meta")
        artifacts = get_preserved_artifacts(updated)
        assert artifacts is not None
        assert artifacts["reason"] == "review_pending"
        assert artifacts["recoverable"] is False

        clear_preserved_artifacts(session, "pane-meta")
        assert get_preserved_artifacts(get_pane(session, "pane-meta")) is None


class TestRemovePane:
    def test_remove_pane_deletes_record(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _make_pane("pane-remove", session)
        add_pane(session, pane)

        assert get_pane(session, "pane-remove") is not None

        remove_pane(session, "pane-remove")

        assert get_pane(session, "pane-remove") is None
        assert all_panes(session) == []


class TestPaneListing:
    def test_list_panes_slim_returns_minimal_fields(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        long_prompt = "x" * 300
        pane = _make_pane("pane-slim", session)
        pane.prompt = long_prompt
        add_pane(session, pane)

        slim = list_panes_slim(session)
        assert len(slim) == 1
        entry = slim[0]
        # Prompt should be truncated to the first 200 characters.
        assert entry["prompt"] == long_prompt[:200]
        assert entry["slug"] == "pane-slim"
        # Metadata is present even in the slim view.
        assert "project_root" in entry

    def test_all_panes_returns_full_records(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        long_prompt = "y" * 300
        pane = _make_pane("pane-all", session)
        pane.prompt = long_prompt
        add_pane(session, pane)

        panes = all_panes(session)
        assert len(panes) == 1
        entry = panes[0]
        # Full prompt should be preserved.
        assert entry["prompt"] == long_prompt
        assert entry["slug"] == "pane-all"


class TestPaneEvents:
    def test_emit_and_read_events_round_trip(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)

        emit_event(session, "pane_created", "pane-events", action="created", index=1)
        emit_event(session, "pane_done", "pane-events", action="done", index=2)

        events = read_events(session, slug="pane-events")
        assert len(events) == 2

        first, second = events
        assert first["event"] == "pane_created"
        assert first["pane"] == "pane-events"
        assert first["action"] == "created"
        assert first["index"] == 1

        assert second["event"] == "pane_done"
        assert second["pane"] == "pane-events"
        assert second["action"] == "done"
        assert second["index"] == 2

    def test_queue_dispatch_emits_event_and_take_dispatch_queue_clears(
        self, tmp_path: Path
    ) -> None:
        session = _make_session(tmp_path)

        depth = queue_dispatch(session, {"summary": "ship it", "agent_hint": "qwen-35b"})

        assert depth == 1
        events = read_events(session)
        dispatch_event = [event for event in events if event["event"] == "dispatch_queued"][0]
        assert dispatch_event["pane"] == "dispatch-queue"
        assert dispatch_event["summary"] == "ship it"
        assert dispatch_event["agent_hint"] == "qwen-35b"

        queued = take_dispatch_queue(session)
        assert len(queued) == 1
        assert queued[0]["summary"] == "ship it"
        assert queued[0]["agent_hint"] == "qwen-35b"
        assert "ts" in queued[0]
        assert take_dispatch_queue(session) == []
