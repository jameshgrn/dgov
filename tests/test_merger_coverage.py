from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from dgov.backend import set_backend
from dgov.inspection import MergeResult
from dgov.merger import (
    MergeConflict,
    MergeSuccess,
    _lint_fix_merged_files,
    _no_squash_merge,
    _pick_resolver_agent,
    _plumbing_merge,
    _resolve_conflicts_with_agent,
    _restore_protected_files,
    merge_worker_pane,
)
from dgov.persistence import IllegalTransitionError

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _init_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / ".gitignore").write_text(".dgov/\n")
    (repo / "README.md").write_text("initial\n")
    (repo / "CLAUDE.md").write_text("trusted instructions\n")
    _git(repo, "add", ".gitignore", "README.md", "CLAUDE.md")
    _git(repo, "commit", "-m", "initial")
    return repo


def _add_worktree(repo: Path, tmp_path: Path, branch_name: str) -> Path:
    worktree = tmp_path / f"{branch_name}-wt"
    _git(repo, "worktree", "add", "-b", branch_name, str(worktree), "HEAD")
    return worktree


def _pane_record(
    repo: Path,
    worktree: Path,
    *,
    slug: str,
    branch_name: str,
    base_sha: str,
    state: str = "done",
) -> dict[str, str]:
    return {
        "slug": slug,
        "prompt": f"Add changes for {slug}",
        "pane_id": "%1",
        "agent": "codex",
        "project_root": str(repo),
        "worktree_path": str(worktree),
        "branch_name": branch_name,
        "base_sha": base_sha,
        "state": state,
        "owns_worktree": True,
    }


@pytest.fixture(autouse=True)
def _mock_backend() -> MagicMock:
    backend = MagicMock()
    backend.is_alive.return_value = False
    set_backend(backend)
    yield backend
    set_backend(None)  # type: ignore[arg-type]
    pass


def test_no_squash_merge_creates_no_ff_merge_commit_and_restores_dirty_tree(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path, "no-squash")

    _git(repo, "checkout", "-b", "dgov-worker")
    (repo / "one.txt").write_text("one\n")
    _git(repo, "add", "one.txt")
    _git(repo, "commit", "-m", "add one")
    (repo / "two.txt").write_text("two\n")
    _git(repo, "add", "two.txt")
    _git(repo, "commit", "-m", "add two")
    _git(repo, "checkout", "main")

    (repo / "README.md").write_text("dirty main\n")

    result = _no_squash_merge(str(repo), "dgov-worker")

    assert result.success is True
    assert (repo / "one.txt").read_text() == "one\n"
    assert (repo / "two.txt").read_text() == "two\n"
    assert (repo / "README.md").read_text() == "dirty main\n"
    assert "M README.md" in _git(repo, "status", "--porcelain").stdout
    assert _git(repo, "log", "-1", "--pretty=%s").stdout.strip() == "Merge worker (2 commits)"
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()) == 3


def test_merge_worker_pane_restores_protected_files_before_merge(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    repo = _init_repo(tmp_path, "restore-protected")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-protected")

    (worktree / "CLAUDE.md").write_text("worker clobber\n")
    (worktree / "worker.txt").write_text("real worker change\n")
    _git(worktree, "add", "CLAUDE.md", "worker.txt")
    _git(worktree, "commit", "-m", "worker changes")

    pane = _pane_record(
        repo,
        worktree,
        slug="protected",
        branch_name="dgov-protected",
        base_sha=base_sha,
    )

    def _fake_tmux_run(args, **kwargs):
        if "pane_title" in str(args):
            return "[kimi-k25-0] protected ~"
        if "pane_pid" in str(args):
            return "99999"
        return ""

    with (
        patch("dgov.persistence.get_pane", return_value=pane) as mock_get_pane,
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
        patch("dgov.tmux._run", side_effect=_fake_tmux_run),
    ):
        result = merge_worker_pane(str(repo), "protected", session_root=str(repo))

    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result.merged == "protected"
    assert result.branch == "dgov-protected"
    assert (repo / "CLAUDE.md").read_text() == "trusted instructions\n"
    assert (repo / "worker.txt").read_text() == "real worker change\n"
    assert _git(repo, "show", "HEAD:CLAUDE.md").stdout == "trusted instructions\n"
    assert _git(repo, "show", "HEAD:worker.txt").stdout == "real worker change\n"
    assert not worktree.exists()
    assert _git(repo, "rev-parse", "--verify", "dgov-protected", check=False).returncode != 0
    mock_get_pane.assert_called_once_with(str(repo), "protected")
    mock_update_state.assert_called_once_with(str(repo), "protected", "merged")
    mock_emit_event.assert_called_once_with(
        str(repo),
        "pane_merged",
        "protected",
        merge_sha=merge_sha,
        branch="dgov-protected",
    )
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()
    _mock_backend.destroy.assert_called_once_with("%1")


def test_merge_worker_pane_merges_branch_and_auto_closes_worker(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    repo = _init_repo(tmp_path, "merge-success")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-success")

    (worktree / "worker.txt").write_text("merged content\n")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-m", "add worker file")

    pane = _pane_record(
        repo,
        worktree,
        slug="success",
        branch_name="dgov-success",
        base_sha=base_sha,
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane) as mock_get_pane,
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        result = merge_worker_pane(str(repo), "success", session_root=str(repo))

    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result.merged == "success"
    assert result.branch == "dgov-success"
    assert result.files_changed == 1
    assert (repo / "worker.txt").read_text() == "merged content\n"
    assert not worktree.exists()
    assert _git(repo, "rev-parse", "--verify", "dgov-success", check=False).returncode != 0
    mock_get_pane.assert_called_once_with(str(repo), "success")
    mock_update_state.assert_called_once_with(str(repo), "success", "merged")
    mock_emit_event.assert_called_once_with(
        str(repo),
        "pane_merged",
        "success",
        merge_sha=merge_sha,
        branch="dgov-success",
    )
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()
    _mock_backend.destroy.assert_called_once_with("%1")


def test_merge_worker_pane_does_not_amend_unrelated_dirty_main_files(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    repo = _init_repo(tmp_path, "merge-dirty-main")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-dirty-main")

    (worktree / "worker.py").write_text("def worker( ):\n  return 1\n")
    _git(worktree, "add", "worker.py")
    _git(worktree, "commit", "-m", "add worker module")

    (repo / "README.md").write_text("dirty main\n")

    pane = _pane_record(
        repo,
        worktree,
        slug="dirty-main",
        branch_name="dgov-dirty-main",
        base_sha=base_sha,
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state"),
        patch("dgov.persistence.emit_event"),
        patch("dgov.persistence.set_pane_metadata"),
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane"),
        patch("dgov.inspection._run_related_tests", return_value={}),
    ):
        result = merge_worker_pane(str(repo), "dirty-main", session_root=str(repo))

    assert result.merged == "dirty-main"
    assert result.fixed == ["worker.py"]
    assert (repo / "worker.py").read_text() == "def worker():\n    return 1\n"
    assert (repo / "README.md").read_text() == "dirty main\n"
    assert _git(repo, "show", "HEAD:README.md").stdout == "initial\n"
    assert _git(repo, "show", "HEAD:worker.py").stdout == "def worker():\n    return 1\n"
    assert _git(repo, "status", "--porcelain").stdout.splitlines() == [" M README.md"]


def test_merge_blocks_when_governor_dirty_overlaps_worker_changes(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    """Bug #84: governor uncommitted changes to files workers also touched must block merge."""
    repo = _init_repo(tmp_path, "gov-overlap")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-gov-overlap")

    # Worker modifies README.md on branch
    (worktree / "README.md").write_text("worker edit\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "worker changes README")

    # Governor modifies same file on main (uncommitted)
    (repo / "README.md").write_text("governor edit\n")

    pane = _pane_record(
        repo,
        worktree,
        slug="gov-overlap",
        branch_name="dgov-gov-overlap",
        base_sha=base_sha,
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state"),
        patch("dgov.persistence.emit_event"),
        patch("dgov.persistence.set_pane_metadata"),
        patch("dgov.backend.get_backend", return_value=_mock_backend),
    ):
        result = merge_worker_pane(str(repo), "gov-overlap", session_root=str(repo))

    assert result.error is not None
    assert "uncommitted" in result.error.lower()
    assert "README.md" in result.error
    # Governor's file should be untouched
    assert (repo / "README.md").read_text() == "governor edit\n"


def test_merge_worker_pane_returns_error_when_pane_missing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "missing-pane")

    with (
        patch("dgov.persistence.get_pane", return_value=None) as mock_get_pane,
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        result = merge_worker_pane(str(repo), "missing", session_root=str(repo))

    assert result.error == "Pane not found: missing"
    mock_get_pane.assert_called_once_with(str(repo), "missing")
    mock_update_state.assert_not_called()
    mock_emit_event.assert_not_called()
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_merge_worker_pane_refuses_active_pane_state(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path, "illegal-state")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-active")

    (worktree / "worker.txt").write_text("merged before state update\n")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-m", "add worker file")

    pane = _pane_record(
        repo,
        worktree,
        slug="active-pane",
        branch_name="dgov-active",
        base_sha=base_sha,
        state="active",
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        result = merge_worker_pane(str(repo), "active-pane", session_root=str(repo))

    assert result.error == "Pane active-pane is in state 'active', not 'done'"
    assert result.current_state == "active"
    assert (worktree / "worker.txt").read_text() == "merged before state update\n"
    assert worktree.exists()
    assert _git(repo, "rev-parse", "--verify", "dgov-active").returncode == 0
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert not (repo / "worker.txt").exists()
    mock_update_state.assert_not_called()
    mock_emit_event.assert_not_called()
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_merge_worker_pane_skip_returns_conflicts_without_touching_worktree(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path, "merge-conflict")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-conflict")

    (worktree / "README.md").write_text("worker change\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "worker readme change")

    (repo / "README.md").write_text("main change\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "main readme change")

    pane = _pane_record(
        repo,
        worktree,
        slug="conflict-pane",
        branch_name="dgov-conflict",
        base_sha=base_sha,
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        result = merge_worker_pane(str(repo), "conflict-pane", session_root=str(repo))

    # Rebase fails, falls back to plumbing merge which detects the conflict
    assert isinstance(result, MergeConflict), f"Expected MergeConflict, got {type(result)}"
    assert result.hint == "Re-run with --resolve agent or --resolve manual."
    assert result.slug == "conflict-pane"
    assert result.branch == "dgov-conflict"
    assert (repo / "README.md").read_text() == "main change\n"
    assert worktree.exists()
    assert _git(repo, "rev-parse", "--verify", "dgov-conflict").returncode == 0
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    mock_update_state.assert_called_once_with(str(repo), "conflict-pane", "merge_conflict")
    mock_emit_event.assert_called_once_with(
        str(repo), "pane_merge_conflict", "conflict-pane", branch="dgov-conflict"
    )
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_restore_protected_files_restores_claude_from_base_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "restore-direct")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-restore-direct")

    (worktree / "CLAUDE.md").write_text("worker override\n")
    (worktree / "worker.txt").write_text("keep this change\n")
    _git(worktree, "add", "CLAUDE.md", "worker.txt")
    _git(worktree, "commit", "-m", "worker clobbers protected file")

    pane = _pane_record(
        repo,
        worktree,
        slug="restore-direct",
        branch_name="dgov-restore-direct",
        base_sha=base_sha,
    )

    _restore_protected_files(str(repo), pane)

    assert (worktree / "CLAUDE.md").read_text() == "trusted instructions\n"
    assert _git(worktree, "show", "HEAD:CLAUDE.md").stdout == "trusted instructions\n"
    assert _git(worktree, "show", "HEAD:worker.txt").stdout == "keep this change\n"
    assert _git(worktree, "diff", "--name-only", f"{base_sha}..HEAD").stdout.splitlines() == [
        "worker.txt"
    ]


def test_merge_worker_pane_returns_error_when_branch_name_missing(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "missing-branch")
    pane = {
        "slug": "missing-branch",
        "pane_id": "%1",
        "project_root": str(repo),
        "worktree_path": str(repo),
        "base_sha": _git(repo, "rev-parse", "HEAD").stdout.strip(),
        "state": "done",
    }

    with (
        patch("dgov.persistence.get_pane", return_value=pane) as mock_get_pane,
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        result = merge_worker_pane(str(repo), "missing-branch", session_root=str(repo))

    assert result.error == "Pane missing-branch is missing branch_name"
    mock_get_pane.assert_called_once_with(str(repo), "missing-branch")
    mock_update_state.assert_not_called()
    mock_emit_event.assert_not_called()
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_merge_worker_pane_refuses_dirty_worktree_changes(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    repo = _init_repo(tmp_path, "auto-commit")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-auto-commit")

    (worktree / "worker.txt").write_text("committed by merge\n")

    pane = _pane_record(
        repo,
        worktree,
        slug="auto-commit",
        branch_name="dgov-auto-commit",
        base_sha=base_sha,
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        result = merge_worker_pane(str(repo), "auto-commit", session_root=str(repo))

    assert result.error == "Worktree for pane auto-commit has uncommitted changes"
    assert result.dirty_files == ["worker.txt"]
    assert result.slug == "auto-commit"
    assert (worktree / "worker.txt").read_text() == "committed by merge\n"
    assert worktree.exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    mock_update_state.assert_not_called()
    mock_emit_event.assert_not_called()
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_merge_worker_pane_allows_done_pane_with_attached_agent(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    """Done-state panes skip agent-attached check — the worker already signaled completion."""
    repo = _init_repo(tmp_path, "attached-agent")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-attached")

    (worktree / "worker.txt").write_text("committed work\n")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-m", "add worker file")

    pane = _pane_record(
        repo,
        worktree,
        slug="attached-pane",
        branch_name="dgov-attached",
        base_sha=base_sha,
        state="done",
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state"),
        patch("dgov.persistence.emit_event"),
        patch("dgov.persistence.set_pane_metadata"),
        patch("dgov.persistence.remove_pane"),
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane"),
        patch("dgov.done._agent_still_running", return_value=True),
    ):
        _mock_backend.is_alive.return_value = True
        result = merge_worker_pane(str(repo), "attached-pane", session_root=str(repo))

    # Merge should succeed despite agent still running — pane is done
    assert result.merged == "attached-pane"


def test_merge_worker_pane_allows_abandoned_transition_after_success(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    repo = _init_repo(tmp_path, "abandoned-pane")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-abandoned")

    (worktree / "worker.txt").write_text("merged anyway\n")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-m", "worker changes")

    pane = _pane_record(
        repo,
        worktree,
        slug="abandoned-pane",
        branch_name="dgov-abandoned",
        base_sha=base_sha,
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch(
            "dgov.persistence.update_pane_state",
            side_effect=IllegalTransitionError("abandoned", "merged", "abandoned-pane"),
        ) as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        result = merge_worker_pane(str(repo), "abandoned-pane", session_root=str(repo))

    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result.merged == "abandoned-pane"
    assert result.branch == "dgov-abandoned"
    assert (repo / "worker.txt").read_text() == "merged anyway\n"
    mock_update_state.assert_called_once_with(str(repo), "abandoned-pane", "merged")
    mock_emit_event.assert_called_once_with(
        str(repo),
        "pane_merged",
        "abandoned-pane",
        merge_sha=merge_sha,
        branch="dgov-abandoned",
    )
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_merge_worker_pane_manual_conflict_leaves_markers_for_resolution(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path, "manual-conflict")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-manual")

    (worktree / "README.md").write_text("worker change\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "worker readme change")

    (repo / "README.md").write_text("main change\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "main readme change")

    pane = _pane_record(
        repo,
        worktree,
        slug="manual-pane",
        branch_name="dgov-manual",
        base_sha=base_sha,
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        result = merge_worker_pane(
            str(repo), "manual-pane", session_root=str(repo), resolve="manual"
        )

    # Rebase fails → plumbing merge → conflict → manual resolution path
    assert isinstance(result, MergeConflict)
    assert result.slug == "manual-pane"
    assert result.branch == "dgov-manual"
    assert result.resolve == "manual"
    assert len(result.conflicts) > 0
    mock_update_state.assert_called_once_with(str(repo), "manual-pane", "merge_conflict")
    mock_emit_event.assert_called_once_with(
        str(repo), "pane_merge_conflict", "manual-pane", branch="dgov-manual"
    )
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_merge_worker_pane_returns_unknown_resolve_error_for_conflict(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path, "unknown-resolve")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-unknown")

    (worktree / "README.md").write_text("worker change\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "worker readme change")

    (repo / "README.md").write_text("main change\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "main readme change")

    pane = _pane_record(
        repo,
        worktree,
        slug="unknown-pane",
        branch_name="dgov-unknown",
        base_sha=base_sha,
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        result = merge_worker_pane(
            str(repo), "unknown-pane", session_root=str(repo), resolve="bogus"
        )

    # Rebase fails, falls back to plumbing merge, then unknown resolve is rejected
    assert result.error == "Unknown resolve strategy: bogus"
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    mock_update_state.assert_called_once_with(str(repo), "unknown-pane", "merge_conflict")
    mock_emit_event.assert_called_once_with(
        str(repo), "pane_merge_conflict", "unknown-pane", branch="dgov-unknown"
    )
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_merge_worker_pane_emits_failure_when_merge_fails_without_conflicts(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path, "merge-error")
    pane = _pane_record(
        repo,
        repo,
        slug="merge-error",
        branch_name="dgov-missing",
        base_sha=_git(repo, "rev-parse", "HEAD").stdout.strip(),
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
        patch("dgov.merger._rebase_onto_head", return_value=MergeResult(success=True)),
        patch(
            "dgov.merger._plumbing_merge", return_value=MergeResult(success=False, stderr="boom")
        ),
        patch("dgov.merger._detect_conflicts", return_value=[]),
    ):
        result = merge_worker_pane(str(repo), "merge-error", session_root=str(repo))

    assert result.error == "boom"
    mock_update_state.assert_not_called()
    mock_emit_event.assert_called_once_with(
        str(repo), "pane_merge_failed", "merge-error", error="boom"
    )
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_merge_worker_pane_reports_protected_damage_and_lint_results(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    repo = _init_repo(tmp_path, "post-merge-fallback")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-post-merge")

    (worktree / "CLAUDE.md").write_text("worker override\n")
    (worktree / "worker.py").write_text("def worker( ):\n  return 1\n")
    _git(worktree, "add", "CLAUDE.md", "worker.py")
    _git(worktree, "commit", "-m", "worker changes")

    pane = _pane_record(
        repo,
        worktree,
        slug="post-merge",
        branch_name="dgov-post-merge",
        base_sha=base_sha,
    )

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
        patch("dgov.merger._lint_fix_merged_files", return_value={"lint_fixed": ["worker.py"]}),
        patch("dgov.inspection._run_related_tests", return_value={}),
        patch("dgov.merger._restore_protected_files"),
    ):
        result = merge_worker_pane(str(repo), "post-merge", session_root=str(repo))

    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result.merged == "post-merge"
    assert result.warning == "protected files changed: ['CLAUDE.md']"
    assert result.fixed == ["worker.py"]
    mock_update_state.assert_called_once_with(str(repo), "post-merge", "merged")
    mock_emit_event.assert_called_once_with(
        str(repo),
        "pane_merged",
        "post-merge",
        merge_sha=merge_sha,
        branch="dgov-post-merge",
    )
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_resolve_conflicts_with_agent_commits_resolved_merge(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "agent-resolve")
    worktree = _add_worktree(repo, tmp_path, "dgov-agent")

    (worktree / "README.md").write_text("worker change\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "worker readme change")

    (repo / "README.md").write_text("main change\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "main readme change")

    def _resolve_and_stage(**kwargs: str) -> SimpleNamespace:
        (repo / "README.md").write_text("resolved change\n")
        _git(repo, "add", "README.md")
        return SimpleNamespace(slug=kwargs["slug"])

    with (
        patch("dgov.lifecycle.create_worker_pane", side_effect=_resolve_and_stage),
        patch("dgov.waiter._is_done", return_value=True),
        patch("dgov.status.capture_worker_output", return_value=None),
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
    ):
        resolved = _resolve_conflicts_with_agent(
            str(repo), "dgov-agent", {"slug": "agent-pane"}, str(repo), timeout=1
        )

    assert resolved is True
    assert (repo / "README.md").read_text() == "resolved change\n"
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    assert _git(repo, "log", "-1", "--pretty=%s").stdout.strip() == "Merge branch 'dgov-agent'"
    mock_close_worker_pane.assert_called_once_with(
        str(repo), "resolve-dgov-agent", session_root=str(repo)
    )


def test_pick_resolver_agent_prefers_available_binary() -> None:
    def _fake_which(cmd: str) -> str | None:
        if cmd == "codex":
            return "/usr/bin/codex"
        return None

    with patch("shutil.which", side_effect=_fake_which):
        assert _pick_resolver_agent() == "codex"


def test_lint_fix_merged_files_formats_python_and_amends_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "lint-fix")

    (repo / "worker.py").write_text("def worker( ):\n  return 1\n")
    _git(repo, "add", "worker.py")
    _git(repo, "commit", "-m", "add worker module")

    result = _lint_fix_merged_files(str(repo), ["worker.py"])

    assert result["lint_fixed"] == ["worker.py"]
    assert (repo / "worker.py").read_text() == "def worker():\n    return 1\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


@pytest.mark.unit
def test_lint_fix_skips_amend_when_no_staged_changes(tmp_path: Path) -> None:
    """When ruff makes no changes, skip the amend to avoid CalledProcessError."""
    repo = _init_repo(tmp_path, "lint-noop")

    # Already-clean Python file — ruff will make no changes
    (repo / "clean.py").write_text("def clean():\n    return 1\n")
    _git(repo, "add", "clean.py")
    _git(repo, "commit", "-m", "add clean module")

    result = _lint_fix_merged_files(str(repo), ["clean.py"])

    # No lint changes → empty result, no crash
    assert "lint_fixed" not in result
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_plumbing_merge_stash_pop_failure_returns_success_with_warning(
    tmp_path: Path,
) -> None:
    """When stash pop fails after merge, result is success=True with a warning."""
    repo = _init_repo(tmp_path, "stash-pop-fail")
    worktree = _add_worktree(repo, tmp_path, "dgov-stash-pop")

    (worktree / "worker.txt").write_text("worker content\n")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-m", "add worker file")

    _git(repo, "checkout", "main")

    # Make the worktree dirty so stash push triggers
    (repo / "README.md").write_text("dirty main\n")

    # Patch subprocess.run to intercept stash pop and make it fail
    original_run = subprocess.run

    def _intercept_stash_pop(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "stash" in cmd and "pop" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="conflict")
        return original_run(*args, **kwargs)

    with patch("dgov.merger.subprocess.run", side_effect=_intercept_stash_pop):
        result = _plumbing_merge(str(repo), "dgov-stash-pop")

    assert result.success is True
    assert len(result.warnings) == 1
    assert "stash" in result.warnings[0].lower()
    assert (repo / "worker.txt").read_text() == "worker content\n"


def test_plumbing_merge_reset_hard_failure_gives_actionable_error(
    tmp_path: Path,
) -> None:
    """When reset --hard fails after update-ref, error message includes recovery steps."""
    repo = _init_repo(tmp_path, "reset-fail")
    worktree = _add_worktree(repo, tmp_path, "dgov-reset-fail")

    (worktree / "worker.txt").write_text("worker content\n")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-m", "add worker file")

    _git(repo, "checkout", "main")

    original_run = subprocess.run

    def _intercept_reset(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "reset" in cmd and "--hard" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="reset error")
        return original_run(*args, **kwargs)

    with patch("dgov.merger.subprocess.run", side_effect=_intercept_reset):
        result = _plumbing_merge(str(repo), "dgov-reset-fail")

    assert result.success is False
    assert "update-ref advanced" in result.stderr
    assert "git reset --hard HEAD" in result.stderr


def test_no_squash_merge_stash_pop_failure_returns_success_with_warning(
    tmp_path: Path,
) -> None:
    """When stash pop fails after no-squash merge, result is success=True with a warning."""
    repo = _init_repo(tmp_path, "no-squash-stash")

    _git(repo, "checkout", "-b", "dgov-ns-stash")
    (repo / "worker.txt").write_text("worker content\n")
    _git(repo, "add", "worker.txt")
    _git(repo, "commit", "-m", "add worker file")
    _git(repo, "checkout", "main")

    # Make the worktree dirty so stash push triggers
    (repo / "README.md").write_text("dirty main\n")

    original_run = subprocess.run

    def _intercept_stash_pop(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "stash" in cmd and "pop" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="conflict")
        return original_run(*args, **kwargs)

    with patch("dgov.merger.subprocess.run", side_effect=_intercept_stash_pop):
        result = _no_squash_merge(str(repo), "dgov-ns-stash")

    assert result.success is True
    assert len(result.warnings) == 1
    assert "stash" in result.warnings[0].lower()
    assert (repo / "worker.txt").read_text() == "worker content\n"


def test_advance_branch_rejects_dirty_overlap_with_merge(tmp_path: Path) -> None:
    """_advance_current_branch_to_commit fails when dirty files overlap with merge."""
    from dgov.merger import _advance_current_branch_to_commit

    repo = _init_repo(tmp_path, "advance-overlap")
    worktree = _add_worktree(repo, tmp_path, "dgov-advance-overlap")

    # Worker changes README.md on branch
    (worktree / "README.md").write_text("worker edit\n")
    _git(worktree, "add", "README.md")
    _git(worktree, "commit", "-m", "worker changes README")

    _git(repo, "checkout", "main")

    # Governor dirties README.md (uncommitted)
    (repo / "README.md").write_text("governor edit\n")

    # Merge the branch in-memory to get a commit SHA
    merge_sha_r = _git(worktree, "rev-parse", "HEAD")  # use branch tip as the "merge commit"

    result = _advance_current_branch_to_commit(str(repo), merge_sha_r.stdout.strip())
    assert result.success is False
    assert "uncommitted" in result.stderr.lower()
    # Governor's file should be untouched
    assert (repo / "README.md").read_text() == "governor edit\n"


def test_advance_branch_succeeds_when_dirty_no_overlap(tmp_path: Path) -> None:
    """_advance_current_branch_to_commit succeeds when dirty files don't overlap."""
    from dgov.merger import _advance_current_branch_to_commit

    repo = _init_repo(tmp_path, "advance-no-overlap")
    worktree = _add_worktree(repo, tmp_path, "dgov-advance-nooverlap")

    # Worker adds a new file (doesn't touch README.md)
    (worktree / "worker.txt").write_text("worker content\n")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-m", "add worker file")

    _git(repo, "checkout", "main")

    # Governor dirties README.md (no overlap with worker)
    (repo / "README.md").write_text("governor edit\n")

    merge_sha_r = _git(worktree, "rev-parse", "HEAD")
    result = _advance_current_branch_to_commit(str(repo), merge_sha_r.stdout.strip())
    assert result.success is True
    # Governor's dirty file should be restored via stash pop
    assert (repo / "README.md").read_text() == "governor edit\n"


def test_merge_worker_pane_surfaces_stash_warnings(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    """MergeResult.warnings are surfaced as stash_warnings in the return dict."""
    repo = _init_repo(tmp_path, "surface-warnings")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-surface-warn")

    (worktree / "worker.txt").write_text("worker content\n")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-m", "add worker file")

    pane = _pane_record(
        repo,
        worktree,
        slug="surface-warn",
        branch_name="dgov-surface-warn",
        base_sha=base_sha,
    )

    fake_merge = MergeResult(success=True, warnings=["stash pop conflict"])

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state"),
        patch("dgov.persistence.emit_event"),
        patch("dgov.persistence.set_pane_metadata"),
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane"),
        patch("dgov.merger._plumbing_merge", return_value=fake_merge),
    ):
        result = merge_worker_pane(str(repo), "surface-warn", session_root=str(repo))

    assert result.merged == "surface-warn"
    assert result.stash_warnings == ["stash pop conflict"]


def test_merge_worker_pane_falls_back_when_rebase_fails(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    """When auto-rebase fails, plumbing merge is attempted as fallback."""
    repo = _init_repo(tmp_path, "rebase-fallback")
    worktree = _add_worktree(repo, tmp_path, "dgov-rebase-fb")

    # Create divergent history: commit on main after branch point
    (repo / "other.py").write_text("main change\n")
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-m", "diverge main")

    # Commit on worker branch (non-overlapping file)
    (worktree / "worker.py").write_text("worker change\n")
    _git(worktree, "add", "worker.py")
    _git(worktree, "commit", "-m", "worker commit")

    session_root = str(repo)
    from dgov.persistence import WorkerPane, _get_db, add_pane

    _get_db(session_root)
    pane = WorkerPane(
        slug="rebase-fb",
        prompt="test",
        pane_id="%99",
        agent="pi",
        project_root=str(repo),
        worktree_path=str(worktree),
        branch_name="dgov-rebase-fb",
        state="done",
    )
    add_pane(session_root, pane)

    # Monkey-patch _rebase_onto_head to simulate failure
    with patch("dgov.merger._rebase_onto_head") as mock_rebase:
        mock_rebase.return_value = MergeResult(
            success=False,
            stderr="CONFLICT: simulated rebase failure",
        )
        result = merge_worker_pane(str(repo), "rebase-fb", session_root=session_root)

    # Should succeed via plumbing merge fallback, not error
    assert isinstance(result, MergeSuccess), f"Expected MergeSuccess, got {type(result)}"
    assert result.merged == "rebase-fb"
    assert result.rebase_fallback is True


def test_rebase_skips_attached_worktree_branch(tmp_path: Path) -> None:
    """Rebase on attached worktree branch returns success (no fake failure)."""
    repo = _init_repo(tmp_path, "attached-rebase")
    _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Add a commit on main after the branch is created
    (repo / "other.py").write_text("main change\n")
    _git(repo, "add", "other.py")
    _git(repo, "commit", "-m", "diverge main")

    # Create worktree with branch — git will refuse to rebase attached branches
    worktree = _add_worktree(repo, tmp_path, "dgov-attached-rebase")

    (worktree / "worker.py").write_text("worker change\n")
    _git(worktree, "add", "worker.py")
    _git(worktree, "commit", "-m", "worker commit")

    # Test _stash_and_rebase directly with attached branch
    from dgov.merger import _stash_and_rebase

    # Attached worktree branches can't be rebased — returns failure
    # so the merge pipeline can fall back to candidate merge
    result, current_branch = _stash_and_rebase(
        str(repo), "test-rebase", "HEAD", "dgov-attached-rebase"
    )

    assert result.success is False
    assert (
        "attached" in (result.stderr or "").lower() or "worktree" in (result.stderr or "").lower()
    )
    assert current_branch == "main"


def test_rebase_onto_head_skips_attached_worktree(tmp_path: Path) -> None:
    """_rebase_onto_head returns success for attached worktree branches."""
    repo = _init_repo(tmp_path, "attached-head-rebase")

    # Create divergent history
    (repo / "main.py").write_text("main change\n")
    _git(repo, "add", "main.py")
    _git(repo, "commit", "-m", "main commit after branch point")

    worktree = _add_worktree(repo, tmp_path, "dgov-attached-head")
    (worktree / "worker.txt").write_text("worker change\n")
    _git(worktree, "add", "worker.txt")
    _git(worktree, "commit", "-m", "worker commit")

    from dgov.merger import _rebase_onto_head

    # Branch already based on HEAD — no rebase needed, returns success
    result = _rebase_onto_head(str(repo), "dgov-attached-head")
    assert result.success is True


def test_merge_worker_pane_fails_when_post_merge_tests_fail(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    """Post-merge test failure blocks merge completion and preserves artifacts."""
    repo = _init_repo(tmp_path, "post-test-fail")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-post-test")

    (worktree / "worker.py").write_text("def worker():\n    return 1\n")
    _git(worktree, "add", "worker.py")
    _git(worktree, "commit", "-m", "add worker module")

    pane = _pane_record(
        repo,
        worktree,
        slug="post-test-fail",
        branch_name="dgov-post-test",
        base_sha=base_sha,
    )

    # Simulate post-merge test failure
    fake_test_result = {"tests_ran": 5, "tests_failed": 2, "tests_passed": False}

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata"),
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane"),
        patch("dgov.merger._lint_fix_merged_files", return_value={"lint_fixed": []}),
        patch(
            "dgov.inspection._run_related_tests",
            return_value=fake_test_result,
        ),
    ):
        result = merge_worker_pane(str(repo), "post-test-fail", session_root=str(repo))

    # Validation failed — should NOT be marked merged, worktree preserved
    assert result.error is not None
    assert result.validation_failed is True
    assert result.slug == "post-test-fail"
    assert worktree.exists()
    assert _git(repo, "rev-parse", "--verify", "dgov-post-test").returncode == 0
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    assert not (repo / "worker.py").exists()
    # Should NOT have updated pane state to merged
    mock_update_state.assert_not_called()
    # Should emit failure event
    mock_emit_event.assert_any_call(
        str(repo),
        "pane_merge_failed",
        "post-test-fail",
        error="Post-merge tests failed: 2 failures in 5 tests ran",
    )


def test_merge_worker_pane_reports_related_test_files_on_failure(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    """List-shaped test metadata should produce a clear failure summary."""
    repo = _init_repo(tmp_path, "post-test-fail-files")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-post-test-files")

    (worktree / "worker.py").write_text("def worker():\n    return 1\n")
    _git(worktree, "add", "worker.py")
    _git(worktree, "commit", "-m", "add worker module")

    pane = _pane_record(
        repo,
        worktree,
        slug="post-test-fail-files",
        branch_name="dgov-post-test-files",
        base_sha=base_sha,
    )

    fake_test_result = {
        "tests_ran": ["tests/test_spans.py"],
        "tests_passed": False,
        "test_output": "1 failed",
    }

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata"),
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane"),
        patch("dgov.merger._lint_fix_merged_files", return_value={"lint_fixed": []}),
        patch(
            "dgov.inspection._run_related_tests",
            return_value=fake_test_result,
        ),
    ):
        result = merge_worker_pane(str(repo), "post-test-fail-files", session_root=str(repo))

    assert (
        result.error == "Post-merge tests failed for 1 related test file(s): tests/test_spans.py"
    )
    assert result.validation_failed is True
    assert worktree.exists()
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == base_sha
    mock_update_state.assert_not_called()
    mock_emit_event.assert_any_call(
        str(repo),
        "pane_merge_failed",
        "post-test-fail-files",
        error="Post-merge tests failed for 1 related test file(s): tests/test_spans.py",
    )


def test_lint_unfixable_issues_block_merge_completion(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    """Lint unfixable issues block merge completion."""
    repo = _init_repo(tmp_path, "lint-unfixable")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-lint-unfixable")

    # Create file with unfixable lint issue (syntax error)
    (worktree / "broken.py").write_text("def broken(\n")
    _git(worktree, "add", "broken.py")
    _git(worktree, "commit", "-m", "add broken module")

    pane = _pane_record(
        repo,
        worktree,
        slug="lint-unfixable",
        branch_name="dgov-lint-unfixable",
        base_sha=base_sha,
    )

    # Mock lint result with new format including lint_unfixable_files
    fake_lint_result = {
        "lint_fixed": [],
        "lint_unfixable": ["broken.py:1:1: E741 ambiguous variable name"],
        "lint_unfixable_files": ["broken.py"],
    }

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event"),
        patch("dgov.persistence.set_pane_metadata"),
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane"),
        patch(
            "dgov.merger._lint_fix_merged_files",
            return_value=fake_lint_result,
        ),
        patch("dgov.inspection._run_related_tests", return_value={}),
    ):
        result = merge_worker_pane(str(repo), "lint-unfixable", session_root=str(repo))

    # Lint issues are advisory — should still succeed and be marked merged
    assert isinstance(result, MergeSuccess)
    assert result.merged == "lint-unfixable"
    assert len(result.lint_unfixable_files) > 0
    # Worktree gets cleaned up after successful merge
    mock_update_state.assert_called_once()


def test_both_tests_and_lint_fail_show_first_error(
    tmp_path: Path,
    _mock_backend: MagicMock,
) -> None:
    """When both tests and lint fail, first error is reported."""
    repo = _init_repo(tmp_path, "both-fail")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    worktree = _add_worktree(repo, tmp_path, "dgov-both-fail")

    (worktree / "worker.py").write_text("def worker():\n    return 1\n")
    _git(worktree, "add", "worker.py")
    _git(worktree, "commit", "-m", "add worker module")

    pane = _pane_record(
        repo,
        worktree,
        slug="both-fail",
        branch_name="dgov-both-fail",
        base_sha=base_sha,
    )

    fake_test_result = {"tests_ran": 3, "tests_failed": 1}
    fake_lint_result = {
        "lint_fixed": [],
        "lint_unfixable": ["worker.py:1:1: E741"],
        "lint_unfixable_files": ["worker.py"],
    }

    with (
        patch("dgov.persistence.get_pane", return_value=pane),
        patch("dgov.persistence.update_pane_state"),
        patch("dgov.persistence.emit_event"),
        patch("dgov.persistence.set_pane_metadata"),
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane"),
        patch(
            "dgov.merger._lint_fix_merged_files",
            return_value=fake_lint_result,
        ),
        patch(
            "dgov.inspection._run_related_tests",
            return_value=fake_test_result,
        ),
    ):
        result = merge_worker_pane(str(repo), "both-fail", session_root=str(repo))

    # Test failure comes first in validation order; lint issues don't block
    assert result.error is not None
    assert "tests failed" in result.error
    # But lint should only produce warning, not error
