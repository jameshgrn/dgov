"""Tests for state machine transition enforcement."""

from __future__ import annotations

import pytest

from dgov.persistence import (
    PANE_STATES,
    VALID_TRANSITIONS,
    IllegalTransitionError,
    WorkerPane,
    _add_pane,
    _get_pane,
    _update_pane_state,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def state_dir(tmp_path):
    """Return a session root with .dgov dir ready."""
    (tmp_path / ".dgov").mkdir()
    return str(tmp_path)


def _seed_pane(state_dir: str, slug: str = "test", state: str = "active") -> None:
    """Insert a pane record in the given state."""
    pane = WorkerPane(
        slug=slug,
        prompt="do stuff",
        pane_id="%99",
        agent="claude",
        project_root=state_dir,
        worktree_path=f"{state_dir}/wt",
        branch_name="test-branch",
        state=state,
    )
    _add_pane(state_dir, pane)


class TestValidTransition:
    def test_active_to_done(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.persistence.tmux.update_pane_status", lambda *a: None)
        _seed_pane(state_dir)
        _update_pane_state(state_dir, "test", "done")
        rec = _get_pane(state_dir, "test")
        assert rec["state"] == "done"

    def test_done_to_merged(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.persistence.tmux.update_pane_status", lambda *a: None)
        _seed_pane(state_dir, state="done")
        _update_pane_state(state_dir, "test", "merged")
        rec = _get_pane(state_dir, "test")
        assert rec["state"] == "merged"


class TestSameStateNoop:
    def test_done_to_done(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.persistence.tmux.update_pane_status", lambda *a: None)
        _seed_pane(state_dir, state="done")
        # Should not raise, should be a no-op
        _update_pane_state(state_dir, "test", "done")
        rec = _get_pane(state_dir, "test")
        assert rec["state"] == "done"

    def test_active_to_active(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.persistence.tmux.update_pane_status", lambda *a: None)
        _seed_pane(state_dir)
        _update_pane_state(state_dir, "test", "active")
        rec = _get_pane(state_dir, "test")
        assert rec["state"] == "active"


class TestIllegalTransition:
    def test_active_to_merged_raises(self, state_dir):
        _seed_pane(state_dir)
        with pytest.raises(IllegalTransitionError) as exc_info:
            _update_pane_state(state_dir, "test", "merged")
        assert exc_info.value.current == "active"
        assert exc_info.value.target == "merged"
        assert exc_info.value.slug == "test"

    def test_done_to_active_raises(self, state_dir):
        _seed_pane(state_dir, state="done")
        with pytest.raises(IllegalTransitionError):
            _update_pane_state(state_dir, "test", "active")

    def test_state_unchanged_after_illegal(self, state_dir):
        _seed_pane(state_dir)
        with pytest.raises(IllegalTransitionError):
            _update_pane_state(state_dir, "test", "merged")
        rec = _get_pane(state_dir, "test")
        assert rec["state"] == "active"


class TestClosedIsTerminal:
    def test_closed_to_anything_raises(self, state_dir):
        _seed_pane(state_dir, state="closed")
        for target in PANE_STATES - {"closed"}:
            with pytest.raises(IllegalTransitionError):
                _update_pane_state(state_dir, "test", target)


class TestTransitionTableCompleteness:
    def test_all_states_in_transition_table(self):
        for state in PANE_STATES:
            assert state in VALID_TRANSITIONS, f"{state} missing from VALID_TRANSITIONS"

    def test_transition_targets_are_valid_states(self):
        for source, targets in VALID_TRANSITIONS.items():
            assert source in PANE_STATES, f"source {source} not in PANE_STATES"
            for t in targets:
                assert t in PANE_STATES, f"target {t} (from {source}) not in PANE_STATES"
