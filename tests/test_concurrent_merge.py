"""Concurrent merge stress tests.

Proves dgov's merge infrastructure (merge-tree + merge lock + candidate worktree)
handles parallel agent workloads without data loss.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dgov.merger import merge_worker_pane
from dgov.persistence import WorkerPane, add_pane

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    """Create a git repo with an initial Python file containing 5 functions."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")

    src = repo / "module.py"
    src.write_text(
        "def func_a():\n    return 'a'\n\n"
        "def func_b():\n    return 'b'\n\n"
        "def func_c():\n    return 'c'\n\n"
        "def func_d():\n    return 'd'\n\n"
        "def func_e():\n    return 'e'\n"
    )
    _git(repo, "add", "module.py")
    _git(repo, "commit", "-m", "initial")
    return repo


def _create_worktree(repo: Path, slug: str, session_root: str) -> tuple[Path, str]:
    """Create a worktree + pane record, return (worktree_path, base_sha)."""
    base_sha = _git(repo, "rev-parse", "HEAD")
    wt = repo / ".dgov" / "worktrees" / slug
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", str(wt), "-b", slug)
    # Copy git config to worktree
    _git(wt, "config", "user.email", "test@test.com")
    _git(wt, "config", "user.name", "Test")

    pane = WorkerPane(
        slug=slug,
        prompt="test",
        pane_id=f"%{slug}",
        agent="test-agent",
        project_root=str(repo),
        worktree_path=str(wt),
        branch_name=slug,
        base_sha=base_sha,
        state="done",
    )
    add_pane(session_root, pane)
    return wt, base_sha


class TestSequentialMerge:
    """Three workers edit separate files, merge sequentially."""

    def test_three_workers_separate_files(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        session_root = str(tmp_path / "session")
        Path(session_root).mkdir()

        # Worker 1: create file_a.py
        wt1, _ = _create_worktree(repo, "worker-1", session_root)
        (wt1 / "file_a.py").write_text("A = 1\n")
        _git(wt1, "add", "file_a.py")
        _git(wt1, "commit", "-m", "add file_a")

        # Worker 2: create file_b.py
        wt2, _ = _create_worktree(repo, "worker-2", session_root)
        (wt2 / "file_b.py").write_text("B = 2\n")
        _git(wt2, "add", "file_b.py")
        _git(wt2, "commit", "-m", "add file_b")

        # Worker 3: create file_c.py
        wt3, _ = _create_worktree(repo, "worker-3", session_root)
        (wt3 / "file_c.py").write_text("C = 3\n")
        _git(wt3, "add", "file_c.py")
        _git(wt3, "commit", "-m", "add file_c")

        with patch("dgov.done._agent_still_running", return_value=False):
            r1 = merge_worker_pane(str(repo), "worker-1", session_root=session_root)
            assert r1.get("merged"), f"Worker 1 merge failed: {r1}"

            r2 = merge_worker_pane(str(repo), "worker-2", session_root=session_root)
            assert r2.get("merged"), f"Worker 2 merge failed: {r2}"

            r3 = merge_worker_pane(str(repo), "worker-3", session_root=session_root)
            assert r3.get("merged"), f"Worker 3 merge failed: {r3}"

        # Verify: all files present
        assert (repo / "file_a.py").read_text() == "A = 1\n"
        assert (repo / "file_b.py").read_text() == "B = 2\n"
        assert (repo / "file_c.py").read_text() == "C = 3\n"
        assert (repo / "module.py").exists(), "Original file preserved"

        log = _git(repo, "log", "--oneline")
        assert len(log.strip().splitlines()) == 4


class TestConflictingMerge:
    """Two workers edit the same function — second merge should fail."""

    def test_same_function_conflict(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        session_root = str(tmp_path / "session")
        Path(session_root).mkdir()

        # Worker 1: change func_a to return 'x'
        wt1, _ = _create_worktree(repo, "conflict-1", session_root)
        src1 = wt1 / "module.py"
        src1.write_text(src1.read_text().replace("return 'a'", "return 'x'"))
        _git(wt1, "add", "module.py")
        _git(wt1, "commit", "-m", "change func_a to x")

        # Worker 2: change func_a to return 'y' (conflict!)
        wt2, _ = _create_worktree(repo, "conflict-2", session_root)
        src2 = wt2 / "module.py"
        src2.write_text(src2.read_text().replace("return 'a'", "return 'y'"))
        _git(wt2, "add", "module.py")
        _git(wt2, "commit", "-m", "change func_a to y")

        with patch("dgov.done._agent_still_running", return_value=False):
            r1 = merge_worker_pane(str(repo), "conflict-1", session_root=session_root)
            assert r1.get("merged"), f"First merge should succeed: {r1}"

            r2 = merge_worker_pane(str(repo), "conflict-2", session_root=session_root)
            assert r2.get("error"), "Second merge should fail on conflict"
            assert not r2.get("merged"), "Conflicting merge should not succeed"


class TestSameFileDifferentFunctions:
    """Two workers edit different functions in the same file.

    KNOWN LIMITATION (ledger #68): When auto-rebase fails, the fallback
    conflict detection reports same-file edits as conflicts even if they
    don't overlap. This test documents the current behavior.
    """

    def test_non_overlapping_same_file_detected_as_conflict(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        session_root = str(tmp_path / "session")
        Path(session_root).mkdir()

        # Worker 1: edit func_a (top of file)
        wt1, _ = _create_worktree(repo, "nonoverlap-1", session_root)
        src1 = wt1 / "module.py"
        src1.write_text(src1.read_text().replace("return 'a'", "return 'alpha'"))
        _git(wt1, "add", "module.py")
        _git(wt1, "commit", "-m", "modify func_a")

        # Worker 2: edit func_e (bottom of file)
        wt2, _ = _create_worktree(repo, "nonoverlap-2", session_root)
        src2 = wt2 / "module.py"
        src2.write_text(src2.read_text().replace("return 'e'", "return 'echo'"))
        _git(wt2, "add", "module.py")
        _git(wt2, "commit", "-m", "modify func_e")

        with patch("dgov.done._agent_still_running", return_value=False):
            r1 = merge_worker_pane(str(repo), "nonoverlap-1", session_root=session_root)
            assert r1.get("merged"), f"First merge failed: {r1}"

            # BUG: second merge fails even though edits don't overlap
            # because auto-rebase fails and fallback conflict detection
            # is too aggressive (ledger #68)
            r2 = merge_worker_pane(str(repo), "nonoverlap-2", session_root=session_root)
            assert r2.get("error"), "Currently fails due to rebase fallback (ledger #68)"


class TestStrictClaimsEnforcement:
    """--strict-claims blocks merge when worker touches undeclared files."""

    def test_undeclared_file_blocked(self, tmp_path: Path) -> None:
        repo = _init_repo(tmp_path)
        session_root = str(tmp_path / "session")
        Path(session_root).mkdir()

        wt, _ = _create_worktree(repo, "strict-test", session_root)

        # Declare claims for module.py only
        from dgov.persistence import set_pane_metadata

        set_pane_metadata(session_root, "strict-test", file_claims='["module.py"]')

        # Edit claimed file + create an undeclared file, commit both
        src = wt / "module.py"
        src.write_text(src.read_text().replace("return 'a'", "return 'strict'"))
        (wt / "extra.py").write_text("print('undeclared')\n")
        _git(wt, "add", "module.py", "extra.py")
        _git(wt, "commit", "-m", "edit module + create extra")

        with patch("dgov.done._agent_still_running", return_value=False):
            r = merge_worker_pane(
                str(repo),
                "strict-test",
                session_root=session_root,
                strict_claims=True,
            )
            assert r.get("error"), f"Should block on undeclared files: {r}"
            assert "undeclared" in r["error"].lower() or "claim" in r["error"].lower()
            assert not r.get("merged")
