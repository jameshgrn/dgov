"""Unit tests for LT-GOV no-worktree dispatch mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.inspection import _inspect_worker_pane
from dgov.merger import merge_worker_pane
from dgov.persistence import (
    STATE_DIR,
    WorkerPane,
    add_pane,
    get_pane,
    list_panes_slim,
    update_pane_state,
)

pytestmark = pytest.mark.unit


def _make_session(tmp_path: Path) -> str:
    session = str(tmp_path / "session")
    Path(session).mkdir(parents=True, exist_ok=True)
    (Path(session) / STATE_DIR).mkdir(parents=True, exist_ok=True)
    return session


def _ltgov_pane(slug: str, project_root: str = "/tmp/repo") -> WorkerPane:
    return WorkerPane(
        slug=slug,
        prompt="orchestrate workers",
        pane_id="%99",
        agent="codex-mini",
        project_root=project_root,
        worktree_path=project_root,
        branch_name="",
        owns_worktree=False,
        role="lt-gov",
    )


class TestLtGovNoWorktree:
    def test_ltgov_pane_stored_correctly(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _ltgov_pane("ltgov-1")
        add_pane(session, pane)
        result = get_pane(session, "ltgov-1")
        assert result is not None
        assert result["role"] == "lt-gov"
        assert result["owns_worktree"] is False
        assert result["branch_name"] == ""
        assert result["worktree_path"] == "/tmp/repo"

    def test_ltgov_merge_blocked(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _ltgov_pane("ltgov-merge")
        add_pane(session, pane)
        update_pane_state(session, "ltgov-merge", "done")
        result = merge_worker_pane("/tmp/repo", "ltgov-merge", session_root=session)
        assert result.error is not None
        assert "LT-GOV" in result.error

    def test_ltgov_inspection_returns_safe(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _ltgov_pane("ltgov-inspect")
        add_pane(session, pane)
        result = _inspect_worker_pane("/tmp/repo", "ltgov-inspect", session_root=session)
        assert result.verdict == "safe"
        assert result.commit_count == 0
        assert result.lt_gov is True

    def test_ltgov_close_skips_worktree_removal(self, tmp_path: Path) -> None:
        """Verify owns_worktree=False prevents worktree cleanup attempts."""
        session = _make_session(tmp_path)
        pane = _ltgov_pane("ltgov-close", project_root=str(tmp_path))
        add_pane(session, pane)
        update_pane_state(session, "ltgov-close", "done")
        result = get_pane(session, "ltgov-close")
        assert result is not None
        assert result["owns_worktree"] is False
        # The key invariant: owns_worktree=False means _full_cleanup
        # skips git worktree remove. We verify the flag is persisted correctly.
        # Full integration test of close_worker_pane would require tmux mocking.

    def test_ltgov_list_panes_with_empty_branch(self, tmp_path: Path) -> None:
        session = _make_session(tmp_path)
        pane = _ltgov_pane("ltgov-list")
        add_pane(session, pane)
        panes = list_panes_slim(session)
        ltgov = [p for p in panes if p["slug"] == "ltgov-list"][0]
        assert ltgov["branch_name"] == ""
        assert ltgov["role"] == "lt-gov"
