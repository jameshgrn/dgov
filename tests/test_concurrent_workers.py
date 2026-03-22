"""Stress tests: concurrent workers editing overlapping files + DB thread safety."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from dgov.merger import _plumbing_merge
from dgov.persistence import (
    WorkerPane,
    add_pane,
    all_panes,
)

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
    """Real git repo with a shared file that 3 worktrees will edit."""
    repo_dir = str(tmp_path / "project")
    Path(repo_dir).mkdir()

    _git(repo_dir, "init", "-b", "main")
    _git(repo_dir, "config", "user.email", "test@test.com")
    _git(repo_dir, "config", "user.name", "Test")

    (Path(repo_dir) / ".gitignore").write_text(".dgov/\n")

    # Shared file with distinct sections separated by blank lines
    shared = (
        "# Section A\nline-a1\nline-a2\n\n"
        "# Section B\nline-b1\nline-b2\n\n"
        "# Section C\nline-c1\nline-c2\n"
    )
    (Path(repo_dir) / "shared.txt").write_text(shared)
    _git(repo_dir, "add", ".gitignore", "shared.txt")
    _git(repo_dir, "commit", "-m", "Initial commit")

    yield repo_dir

    pass


class TestConcurrentMerges:
    """3 worktrees append to different sections of the same file, merge sequentially."""

    def test_three_non_overlapping_merges(self, repo):
        repo_dir = repo

        # Create 3 worktrees, each on its own branch
        branches = ["worker-a", "worker-b", "worker-c"]
        worktrees = {}
        for branch in branches:
            wt_path = str(Path(repo_dir).parent / branch)
            _git(repo_dir, "worktree", "add", "-b", branch, wt_path)
            _git(wt_path, "config", "user.email", "test@test.com")
            _git(wt_path, "config", "user.name", "Test")
            worktrees[branch] = wt_path

        # Worker A: append after Section A
        wt_a = worktrees["worker-a"]
        text = (Path(wt_a) / "shared.txt").read_text()
        text = text.replace(
            "# Section A\nline-a1\nline-a2",
            "# Section A\nline-a1\nline-a2\nadded-by-worker-a",
        )
        (Path(wt_a) / "shared.txt").write_text(text)
        _git(wt_a, "add", "shared.txt")
        _git(wt_a, "commit", "-m", "Worker A edits section A")

        # Worker B: append after Section B
        wt_b = worktrees["worker-b"]
        text = (Path(wt_b) / "shared.txt").read_text()
        text = text.replace(
            "# Section B\nline-b1\nline-b2",
            "# Section B\nline-b1\nline-b2\nadded-by-worker-b",
        )
        (Path(wt_b) / "shared.txt").write_text(text)
        _git(wt_b, "add", "shared.txt")
        _git(wt_b, "commit", "-m", "Worker B edits section B")

        # Worker C: append after Section C
        wt_c = worktrees["worker-c"]
        text = (Path(wt_c) / "shared.txt").read_text()
        text = text.replace(
            "# Section C\nline-c1\nline-c2",
            "# Section C\nline-c1\nline-c2\nadded-by-worker-c",
        )
        (Path(wt_c) / "shared.txt").write_text(text)
        _git(wt_c, "add", "shared.txt")
        _git(wt_c, "commit", "-m", "Worker C edits section C")

        # Merge all 3 sequentially into main
        for branch in branches:
            result = _plumbing_merge(repo_dir, branch)
            assert result.success, f"Merge of {branch} failed: {result.stderr}"

        # Verify final file contains all 3 additions
        final = (Path(repo_dir) / "shared.txt").read_text()
        assert "added-by-worker-a" in final
        assert "added-by-worker-b" in final
        assert "added-by-worker-c" in final

        # Verify no conflict markers leaked
        assert "<<<<<<" not in final
        assert "======" not in final
        assert ">>>>>>" not in final

        # Cleanup worktrees
        for branch in branches:
            _git(repo_dir, "worktree", "remove", "--force", worktrees[branch])

    def test_each_merge_is_conflict_free(self, repo):
        """Each sequential merge should return success=True (no conflicts)."""
        repo_dir = repo

        branches = ["edit-top", "edit-mid", "edit-bot"]
        worktrees = {}
        for branch in branches:
            wt_path = str(Path(branch).resolve().parent / f"{branch}-wt")
            wt_path = str(Path(repo_dir).parent / f"{branch}-wt")
            _git(repo_dir, "worktree", "add", "-b", branch, wt_path)
            _git(wt_path, "config", "user.email", "test@test.com")
            _git(wt_path, "config", "user.name", "Test")
            worktrees[branch] = wt_path

        # Each worker adds a unique file (guaranteed no overlap)
        for i, branch in enumerate(branches):
            wt = worktrees[branch]
            fname = f"unique-{i}.txt"
            (Path(wt) / fname).write_text(f"content from {branch}\n")
            _git(wt, "add", fname)
            _git(wt, "commit", "-m", f"{branch} adds {fname}")

        results = []
        for branch in branches:
            r = _plumbing_merge(repo_dir, branch)
            results.append(r)

        assert all(r.success for r in results), [r.stderr for r in results if not r.success]

        # All files present on main
        for i, branch in enumerate(branches):
            assert (Path(repo_dir) / f"unique-{i}.txt").exists()

        for branch in branches:
            _git(repo_dir, "worktree", "remove", "--force", worktrees[branch])


class TestConcurrentDBWrites:
    """Thread-safety: multiple threads call add_pane simultaneously."""

    def test_concurrent_add_pane(self, repo, tmp_path):
        repo_dir = repo
        session_root = repo_dir

        # Ensure .dgov dir exists for state.db
        (Path(session_root) / ".dgov").mkdir(exist_ok=True)

        num_threads = 3
        panes = [
            WorkerPane(
                slug=f"thread-pane-{i}",
                prompt=f"Task {i}",
                pane_id=f"%mock-{i}",
                agent="claude",
                project_root=repo_dir,
                worktree_path=f"/tmp/wt-{i}",
                branch_name=f"branch-{i}",
            )
            for i in range(num_threads)
        ]

        errors: list[Exception] = []

        def _add(pane: WorkerPane) -> None:
            try:
                add_pane(session_root, pane)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_add, args=(p,)) for p in panes]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Concurrent add_pane errors: {errors}"

        stored = all_panes(session_root)
        stored_slugs = {p["slug"] for p in stored}
        expected_slugs = {f"thread-pane-{i}" for i in range(num_threads)}
        assert expected_slugs == stored_slugs

    def test_concurrent_add_pane_10_threads(self, repo, tmp_path):
        """Higher contention: 10 threads writing simultaneously."""
        repo_dir = repo
        session_root = repo_dir
        (Path(session_root) / ".dgov").mkdir(exist_ok=True)

        num_threads = 10
        panes = [
            WorkerPane(
                slug=f"stress-{i}",
                prompt=f"Stress task {i}",
                pane_id=f"%stress-{i}",
                agent="claude",
                project_root=repo_dir,
                worktree_path=f"/tmp/stress-wt-{i}",
                branch_name=f"stress-branch-{i}",
            )
            for i in range(num_threads)
        ]

        errors: list[Exception] = []

        def _add(pane: WorkerPane) -> None:
            try:
                add_pane(session_root, pane)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_add, args=(p,)) for p in panes]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"Concurrent add_pane errors: {errors}"

        stored = all_panes(session_root)
        assert len(stored) == num_threads
