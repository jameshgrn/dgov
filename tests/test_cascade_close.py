"""Tests for cascading close and explicit terminal garbage collection."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.backend import set_backend
from dgov.persistence import (
    WorkerPane,
    add_pane,
    get_child_panes,
    get_pane,
    update_pane_state,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def mock_backend():
    import dgov.backend as _be

    prev = _be._backend
    mock = MagicMock()
    mock.create_pane.return_value = "%1"
    mock.is_alive.return_value = False
    mock.bulk_info.return_value = {}
    set_backend(mock)
    yield mock
    _be._backend = prev


def _add_pane(tmp_path: Path, slug: str, parent_slug: str = "", role: str = "worker", **kw):
    add_pane(
        str(tmp_path),
        WorkerPane(
            slug=slug,
            prompt=kw.get("prompt", "test"),
            pane_id=kw.get("pane_id", f"%{slug}"),
            agent=kw.get("agent", "claude"),
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / slug),
            branch_name=slug,
            role=role,
            parent_slug=parent_slug,
            created_at=kw.get("created_at", time.time()),
        ),
    )


# -- get_child_panes --


class TestGetChildPanes:
    def test_returns_children(self, tmp_path: Path) -> None:
        _add_pane(tmp_path, "ltgov-1", role="lt-gov")
        _add_pane(tmp_path, "child-a", parent_slug="ltgov-1")
        _add_pane(tmp_path, "child-b", parent_slug="ltgov-1")
        _add_pane(tmp_path, "unrelated")

        children = get_child_panes(str(tmp_path), "ltgov-1")
        slugs = {c["slug"] for c in children}
        assert slugs == {"child-a", "child-b"}

    def test_no_children(self, tmp_path: Path) -> None:
        _add_pane(tmp_path, "ltgov-1", role="lt-gov")
        assert get_child_panes(str(tmp_path), "ltgov-1") == []


# -- Cascading close --


class TestCascadingClose:
    @patch("dgov.lifecycle._full_cleanup", return_value={"cleaned": True})
    @patch("dgov.lifecycle.emit_event")
    def test_closes_children_before_parent(self, mock_event, mock_cleanup, tmp_path: Path) -> None:
        sr = str(tmp_path)
        _add_pane(tmp_path, "ltgov-1", role="lt-gov")
        _add_pane(tmp_path, "child-a", parent_slug="ltgov-1")
        _add_pane(tmp_path, "child-b", parent_slug="ltgov-1")

        from dgov.lifecycle import close_worker_pane

        close_worker_pane(sr, "ltgov-1", sr)

        # All three should be closed (removed from DB)
        assert get_pane(sr, "child-a") is None
        assert get_pane(sr, "child-b") is None
        assert get_pane(sr, "ltgov-1") is None

    @patch("dgov.lifecycle._full_cleanup", return_value={"cleaned": True})
    @patch("dgov.lifecycle.emit_event")
    def test_nested_cascade(self, mock_event, mock_cleanup, tmp_path: Path) -> None:
        """Grandchild panes are also closed recursively."""
        sr = str(tmp_path)
        _add_pane(tmp_path, "ltgov-1", role="lt-gov")
        _add_pane(tmp_path, "child-a", parent_slug="ltgov-1")
        _add_pane(tmp_path, "grandchild-1", parent_slug="child-a")

        from dgov.lifecycle import close_worker_pane

        close_worker_pane(sr, "ltgov-1", sr)

        assert get_pane(sr, "grandchild-1") is None
        assert get_pane(sr, "child-a") is None
        assert get_pane(sr, "ltgov-1") is None

    @patch("dgov.lifecycle._full_cleanup", return_value={"cleaned": True})
    @patch("dgov.lifecycle.emit_event")
    def test_no_children_still_works(self, mock_event, mock_cleanup, tmp_path: Path) -> None:
        sr = str(tmp_path)
        _add_pane(tmp_path, "solo-worker")

        from dgov.lifecycle import close_worker_pane

        close_worker_pane(sr, "solo-worker", sr)

        assert get_pane(sr, "solo-worker") is None


# -- Terminal-state pruning --


class TestTerminalStatePruning:
    def test_prune_keeps_old_terminal_panes_until_gc(self, tmp_path: Path) -> None:
        sr = str(tmp_path)
        old_time = time.time() - 7200  # 2 hours ago
        _add_pane(tmp_path, "old-merged", created_at=old_time)
        (tmp_path / "old-merged").mkdir()
        update_pane_state(sr, "old-merged", "done")
        update_pane_state(sr, "old-merged", "merged")

        _add_pane(tmp_path, "old-closed", created_at=old_time)
        (tmp_path / "old-closed").mkdir()
        update_pane_state(sr, "old-closed", "closed")

        from dgov.status import prune_stale_panes

        pruned = prune_stale_panes(sr, sr)

        assert pruned == []
        assert get_pane(sr, "old-merged") is not None
        assert get_pane(sr, "old-closed") is not None

    @patch("dgov.lifecycle._full_cleanup", return_value={"cleaned": True})
    @patch("dgov.lifecycle.emit_event")
    def test_gc_prunes_old_terminal_panes(self, mock_event, mock_cleanup, tmp_path: Path) -> None:
        sr = str(tmp_path)
        old_time = time.time() - 7200
        _add_pane(tmp_path, "old-merged", created_at=old_time)
        (tmp_path / "old-merged").mkdir()
        update_pane_state(sr, "old-merged", "done")
        update_pane_state(sr, "old-merged", "merged")

        _add_pane(tmp_path, "old-closed", created_at=old_time)
        (tmp_path / "old-closed").mkdir()
        update_pane_state(sr, "old-closed", "closed")

        from dgov.status import gc_retained_panes

        result = gc_retained_panes(sr, sr, older_than_s=3600)

        assert result == {"pruned": ["old-merged", "old-closed"], "skipped": []}
        assert get_pane(sr, "old-merged") is None
        assert get_pane(sr, "old-closed") is None

    def test_keeps_recent_terminal_panes(self, tmp_path: Path) -> None:
        sr = str(tmp_path)
        _add_pane(tmp_path, "recent-merged")
        # Create worktree dir so pass 1 doesn't prune it as orphan
        (tmp_path / "recent-merged").mkdir()
        update_pane_state(sr, "recent-merged", "done")
        update_pane_state(sr, "recent-merged", "merged")

        from dgov.status import prune_stale_panes

        pruned = prune_stale_panes(sr, sr)

        # recent-merged was created just now, should NOT be pruned
        assert "recent-merged" not in pruned
        assert get_pane(sr, "recent-merged") is not None

    def test_keeps_active_old_panes(self, tmp_path: Path) -> None:
        sr = str(tmp_path)
        old_time = time.time() - 7200
        _add_pane(tmp_path, "old-active", created_at=old_time)
        # Create worktree dir so pass 1 doesn't prune it as orphan
        (tmp_path / "old-active").mkdir()

        from dgov.status import prune_stale_panes

        pruned = prune_stale_panes(sr, sr)

        # active pane should not be pruned even if old
        assert "old-active" not in pruned
        assert get_pane(sr, "old-active") is not None
