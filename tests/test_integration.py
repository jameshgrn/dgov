"""Integration tests: full dgov lifecycle with real git repos."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.inspection import review_worker_pane
from dgov.lifecycle import close_worker_pane, create_worker_pane
from dgov.merger import merge_worker_pane
from dgov.persistence import (
    WorkerPane,
    _close_cached_connections,
    get_pane,
    read_events,
    update_pane_state,
)
from dgov.recovery import retry_worker_pane

pytestmark = pytest.mark.integration


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture()
def repo(tmp_path: Path):
    """Create a real git repo with an initial commit and mock backend/hooks."""
    repo_dir = str(tmp_path / "project")
    Path(repo_dir).mkdir()

    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@test.com")
    _git(repo_dir, "config", "user.name", "Test")

    # Gitignore .dgov so state files don't pollute worktree status
    (Path(repo_dir) / ".gitignore").write_text(".dgov/\n")
    (Path(repo_dir) / "README.md").write_text("init\n")
    _git(repo_dir, "add", ".gitignore", "README.md")
    _git(repo_dir, "commit", "-m", "Initial commit")

    session_root = repo_dir

    mock_backend = MagicMock()
    mock_backend.create_pane.return_value = "%mock-1"
    mock_backend.is_alive.return_value = False
    mock_backend.bulk_info.return_value = {}

    from dgov.backend import set_backend

    set_backend(mock_backend)

    with patch("dgov.lifecycle._trigger_hook", return_value=False):
        yield {
            "repo_dir": repo_dir,
            "session_root": session_root,
            "backend": mock_backend,
        }

    set_backend(None)  # type: ignore[arg-type]
    _close_cached_connections()


class TestHappyPath:
    """Full lifecycle: create -> work -> done -> review -> merge -> close."""

    def test_full_lifecycle(self, repo):
        repo_dir = repo["repo_dir"]
        session_root = repo["session_root"]

        # 1. Create worker pane
        pane = create_worker_pane(
            project_root=repo_dir,
            prompt="Add hello.txt",
            agent="claude",
            slug="add-hello",
            session_root=session_root,
        )
        assert isinstance(pane, WorkerPane)
        assert pane.slug == "add-hello"
        assert pane.branch_name == "add-hello"

        # Verify pane stored in state
        record = get_pane(session_root, "add-hello")
        assert record is not None
        assert record["state"] == "active"

        # 2. Simulate agent work: write file + commit in worktree
        wt = pane.worktree_path
        (Path(wt) / "hello.txt").write_text("hello world\n")
        _git(wt, "add", "hello.txt")
        _git(wt, "commit", "-m", "Add hello.txt")

        # 3. Mark pane as done
        update_pane_state(session_root, "add-hello", "done")
        record = get_pane(session_root, "add-hello")
        assert record["state"] == "done"

        # 4. Review — should see our commit
        review = review_worker_pane(repo_dir, "add-hello", session_root=session_root)
        assert "error" not in review
        assert review["commit_count"] >= 1
        assert review["verdict"] == "safe"

        # 5. Merge into main
        merge_result = merge_worker_pane(repo_dir, "add-hello", session_root=session_root)
        assert "merged" in merge_result
        assert merge_result["merged"] == "add-hello"

        # Verify file landed on main
        assert (Path(repo_dir) / "hello.txt").read_text() == "hello world\n"

        # 6. Close (should be a no-op since merge already cleaned up)
        closed = close_worker_pane(repo_dir, "add-hello", session_root=session_root)
        assert closed is True

        # 7. Verify events
        events = read_events(session_root)
        event_types = [e["event"] for e in events]
        assert "pane_created" in event_types
        assert "pane_merged" in event_types
        # review_pass emitted by review_worker_pane when verdict=safe
        assert "review_pass" in event_types


class TestRetryPath:
    """Failure/retry: create -> timeout -> retry -> work -> done -> merge."""

    def test_retry_lifecycle(self, repo):
        repo_dir = repo["repo_dir"]
        session_root = repo["session_root"]
        backend = repo["backend"]

        # Give unique pane_ids per create call
        pane_ids = iter(["%mock-1", "%mock-2", "%mock-3"])
        backend.create_pane.side_effect = lambda **kw: next(pane_ids)

        # 1. Create initial pane
        pane = create_worker_pane(
            project_root=repo_dir,
            prompt="Fix bug",
            agent="claude",
            slug="fix-bug",
            session_root=session_root,
        )
        assert pane.slug == "fix-bug"

        # 2. Mark as timed_out
        update_pane_state(session_root, "fix-bug", "timed_out")
        record = get_pane(session_root, "fix-bug")
        assert record["state"] == "timed_out"

        # 3. Retry — creates new pane linked to original
        retry_result = retry_worker_pane(
            repo_dir,
            "fix-bug",
            session_root=session_root,
            agent="claude",
        )
        assert retry_result.get("retried") is True
        new_slug = retry_result["new_slug"]
        assert new_slug.startswith("fix-bug-")

        # Original should be superseded
        old_record = get_pane(session_root, "fix-bug")
        assert old_record["state"] == "superseded"

        # 4. Simulate work in retry pane
        new_record = get_pane(session_root, new_slug)
        assert new_record is not None
        new_wt = new_record["worktree_path"]
        (Path(new_wt) / "fix.py").write_text("# fixed\n")
        _git(new_wt, "add", "fix.py")
        _git(new_wt, "commit", "-m", "Fix the bug")

        # 5. Mark retry pane done
        update_pane_state(session_root, new_slug, "done")

        # 6. Merge retry pane
        merge_result = merge_worker_pane(repo_dir, new_slug, session_root=session_root)
        assert "merged" in merge_result
        assert merge_result["merged"] == new_slug

        # Verify file on main
        assert (Path(repo_dir) / "fix.py").read_text() == "# fixed\n"

        # 7. Verify events include retry-related entries
        events = read_events(session_root)
        event_types = [e["event"] for e in events]
        assert "pane_created" in event_types
        assert "pane_superseded" in event_types
        assert "pane_retry_spawned" in event_types
        assert "pane_merged" in event_types
