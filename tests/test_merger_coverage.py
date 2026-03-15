from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.backend import set_backend
from dgov.merger import _no_squash_merge, merge_worker_pane
from dgov.persistence import IllegalTransitionError, _close_cached_connections

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
    }


@pytest.fixture(autouse=True)
def _mock_backend() -> MagicMock:
    backend = MagicMock()
    backend.is_alive.return_value = False
    set_backend(backend)
    yield backend
    set_backend(None)  # type: ignore[arg-type]
    _close_cached_connections()


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

    with (
        patch("dgov.persistence.get_pane", return_value=pane) as mock_get_pane,
        patch("dgov.persistence.update_pane_state") as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.backend.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.get_backend", return_value=_mock_backend),
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
        patch("dgov.panes._trigger_hook", side_effect=[False, True]),
    ):
        result = merge_worker_pane(str(repo), "protected", session_root=str(repo))

    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result["merged"] == "protected"
    assert result["branch"] == "dgov-protected"
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
    _mock_backend.select_layout.assert_called_once_with("tiled")


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
        patch("dgov.panes._trigger_hook", return_value=True),
    ):
        result = merge_worker_pane(str(repo), "success", session_root=str(repo))

    merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result["merged"] == "success"
    assert result["branch"] == "dgov-success"
    assert result["files_changed"] == 1
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
    _mock_backend.select_layout.assert_called_once_with("tiled")


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

    assert result == {"error": "Pane not found: missing"}
    mock_get_pane.assert_called_once_with(str(repo), "missing")
    mock_update_state.assert_not_called()
    mock_emit_event.assert_not_called()
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()


def test_merge_worker_pane_raises_for_illegal_transition_from_active_state(
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
        patch(
            "dgov.persistence.update_pane_state",
            side_effect=IllegalTransitionError("active", "merged", "active-pane"),
        ) as mock_update_state,
        patch("dgov.persistence.emit_event") as mock_emit_event,
        patch("dgov.persistence.set_pane_metadata") as mock_set_metadata,
        patch("dgov.lifecycle.close_worker_pane") as mock_close_worker_pane,
        patch("dgov.panes._trigger_hook", return_value=True),
    ):
        with pytest.raises(IllegalTransitionError, match="active -> merged"):
            merge_worker_pane(str(repo), "active-pane", session_root=str(repo))

    assert (repo / "worker.txt").read_text() == "merged before state update\n"
    assert worktree.exists()
    assert _git(repo, "rev-parse", "--verify", "dgov-active").returncode == 0
    mock_update_state.assert_called_once_with(str(repo), "active-pane", "merged")
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
        patch("dgov.panes._trigger_hook", return_value=True),
    ):
        result = merge_worker_pane(str(repo), "conflict-pane", session_root=str(repo))

    assert result["error"] == "Merge conflict in dgov-conflict"
    assert result["slug"] == "conflict-pane"
    assert result["branch"] == "dgov-conflict"
    assert result["conflicts"]
    assert result["hint"] == "Re-run with --resolve agent or --resolve manual."
    assert (repo / "README.md").read_text() == "main change\n"
    assert worktree.exists()
    assert _git(repo, "rev-parse", "--verify", "dgov-conflict").returncode == 0
    assert not (repo / ".git" / "MERGE_HEAD").exists()
    mock_update_state.assert_called_once_with(str(repo), "conflict-pane", "merge_conflict")
    mock_emit_event.assert_not_called()
    mock_set_metadata.assert_not_called()
    mock_close_worker_pane.assert_not_called()
