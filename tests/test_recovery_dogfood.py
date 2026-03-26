"""Dogfood tests for dgov.recovery - retry worker pane functionality."""

from __future__ import annotations

import pytest

from dgov.persistence import WorkerPane, add_pane
from dgov.recovery import retry_worker_pane

pytestmark = pytest.mark.unit


class TestRetrySlugComputation:
    def test_retry_increments_slug(self, tmp_path, monkeypatch):
        """Retry of 'foo' creates 'foo-a2' (lineage-based, no suffix stacking)."""
        from dgov.persistence import _get_db

        _get_db(str(tmp_path))
        pane = WorkerPane(
            slug="foo",
            prompt="do something",
            pane_id="%1",
            agent="pi",
            project_root="/fake/project",
            worktree_path=str(tmp_path / "wt"),
            branch_name="foo",
            state="failed",
        )
        add_pane(str(tmp_path), pane)

        # Mock create_worker_pane to avoid real tmux
        class FakePane:
            slug = "foo-a2"
            pane_id = "%999"
            worktree_path = str(tmp_path / "wt2")

        monkeypatch.setattr("dgov.recovery.create_worker_pane", lambda **kw: FakePane())
        monkeypatch.setattr("dgov.recovery.close_worker_pane", lambda *a, **kw: {"closed": True})
        result = retry_worker_pane(str(tmp_path), "foo", session_root=str(tmp_path))
        assert result["retried"] is True
        assert result["new_slug"] == "foo-a2"
        assert result["attempt"] == 2

    def test_retry_not_found(self, tmp_path):
        """Retry of nonexistent pane returns error."""
        from dgov.persistence import _get_db

        _get_db(str(tmp_path))
        result = retry_worker_pane(str(tmp_path), "nope", session_root=str(tmp_path))
        assert "error" in result
