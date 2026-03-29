"""Tests for state machine transition enforcement."""

from __future__ import annotations

import pytest

from dgov.persistence import (
    PANE_STATES,
    VALID_TRANSITIONS,
    CompletionTransitionResult,
    IllegalTransitionError,
    WorkerPane,
    add_pane,
    get_pane,
    settle_completion_state,
    update_pane_state,
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
    add_pane(state_dir, pane)


class TestValidTransition:
    def test_active_to_done(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.tmux.set_title", lambda *a: None)
        _seed_pane(state_dir)
        update_pane_state(state_dir, "test", "done")
        rec = get_pane(state_dir, "test")
        assert rec["state"] == "done"

    def test_done_to_merged_requires_review(self, state_dir, monkeypatch):
        """done → merged is illegal — must go through reviewed_pass first."""
        monkeypatch.setattr("dgov.tmux.set_title", lambda *a: None)
        _seed_pane(state_dir, state="done")
        with pytest.raises(IllegalTransitionError):
            update_pane_state(state_dir, "test", "merged")


class TestSameStateNoop:
    def test_done_to_done(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.tmux.set_title", lambda *a: None)
        _seed_pane(state_dir, state="done")
        # Should not raise, should be a no-op
        update_pane_state(state_dir, "test", "done")
        rec = get_pane(state_dir, "test")
        assert rec["state"] == "done"

    def test_active_to_active(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.tmux.set_title", lambda *a: None)
        _seed_pane(state_dir)
        update_pane_state(state_dir, "test", "active")
        rec = get_pane(state_dir, "test")
        assert rec["state"] == "active"


class TestTimedOutTransitions:
    def test_timed_out_to_done(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.tmux.set_title", lambda *a: None)
        _seed_pane(state_dir, state="timed_out")
        update_pane_state(state_dir, "test", "done")
        rec = get_pane(state_dir, "test")
        assert rec["state"] == "done"

    def test_timed_out_to_merged_requires_review(self, state_dir):
        """timed_out → merged is illegal — must go through done + reviewed_pass."""
        _seed_pane(state_dir, state="timed_out")
        with pytest.raises(IllegalTransitionError):
            update_pane_state(state_dir, "test", "merged")


class TestCompletionTransitions:
    def test_settle_completion_updates_legal_transition(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.tmux.set_title", lambda *a: None)
        _seed_pane(state_dir, state="timed_out")

        result = settle_completion_state(state_dir, "test", "done")

        assert result == CompletionTransitionResult(state="done", changed=True)
        assert get_pane(state_dir, "test")["state"] == "done"

    def test_settle_completion_preserves_existing_terminal_state(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.tmux.set_title", lambda *a: None)
        _seed_pane(state_dir, state="failed")

        result = settle_completion_state(state_dir, "test", "done")

        assert result == CompletionTransitionResult(state="failed", changed=False)
        assert get_pane(state_dir, "test")["state"] == "failed"

    def test_settle_completion_allows_abandoned_override(self, state_dir, monkeypatch):
        monkeypatch.setattr("dgov.tmux.set_title", lambda *a: None)
        _seed_pane(state_dir, state="abandoned")

        result = settle_completion_state(state_dir, "test", "done", allow_abandoned=True)

        assert result == CompletionTransitionResult(state="done", changed=True)
        assert get_pane(state_dir, "test")["state"] == "done"

    def test_settle_completion_still_raises_for_non_completion_target(self, state_dir):
        _seed_pane(state_dir)

        with pytest.raises(ValueError):
            settle_completion_state(state_dir, "test", "merged")


class TestIllegalTransition:
    def test_done_to_active_raises(self, state_dir):
        _seed_pane(state_dir, state="done")
        with pytest.raises(IllegalTransitionError):
            update_pane_state(state_dir, "test", "active")

    def test_active_to_merged_requires_review(self, state_dir):
        """active → merged is illegal — must go through done + reviewed_pass first."""
        _seed_pane(state_dir)
        with pytest.raises(IllegalTransitionError):
            update_pane_state(state_dir, "test", "merged")

    def test_state_unchanged_after_illegal(self, state_dir):
        _seed_pane(state_dir)
        with pytest.raises(IllegalTransitionError):
            update_pane_state(state_dir, "test", "reviewed_pass")
        rec = get_pane(state_dir, "test")
        assert rec["state"] == "active"


class TestClosedIsTerminal:
    def test_closed_to_anything_raises(self, state_dir):
        _seed_pane(state_dir, state="closed")
        for target in PANE_STATES - {"closed"}:
            with pytest.raises(IllegalTransitionError):
                update_pane_state(state_dir, "test", target)


class TestTransitionTableCompleteness:
    def test_all_states_in_transition_table(self):
        for state in PANE_STATES:
            assert state in VALID_TRANSITIONS, f"{state} missing from VALID_TRANSITIONS"

    def test_transition_targets_are_valid_states(self):
        for source, targets in VALID_TRANSITIONS.items():
            assert source in PANE_STATES, f"source {source} not in PANE_STATES"
            for t in targets:
                assert t in PANE_STATES, f"target {t} (from {source}) not in PANE_STATES"
