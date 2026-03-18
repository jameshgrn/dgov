"""Unit tests for dgov.inspection."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dgov.inspection import diff_worker_pane, rebase_governor, review_worker_pane

pytestmark = pytest.mark.unit


def _git(
    repo: Path, *args: str, check: bool = True, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


def _init_repo(repo: Path) -> str:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")

    (repo / ".gitignore").write_text(".dgov/\n")
    (repo / "tracked.txt").write_text("base\n")
    (repo / "dirty.txt").write_text("clean\n")
    _git(repo, "add", ".gitignore", "tracked.txt", "dirty.txt")
    _git(repo, "commit", "-m", "Initial commit")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _commit_file(repo: Path, relpath: str, content: str, message: str) -> str:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", relpath)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def inspection_mocks(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    get_pane = MagicMock()
    emit_event = MagicMock()
    read_events = MagicMock(return_value=[])
    freshness = MagicMock(return_value={"freshness": "fresh"})
    count_retries = MagicMock(return_value=0)

    monkeypatch.setattr("dgov.persistence.get_pane", get_pane)
    monkeypatch.setattr("dgov.inspection.get_pane", get_pane)
    monkeypatch.setattr("dgov.persistence.emit_event", emit_event)
    monkeypatch.setattr("dgov.inspection.emit_event", emit_event)
    monkeypatch.setattr("dgov.persistence.read_events", read_events)
    monkeypatch.setattr("dgov.inspection.read_events", read_events)
    monkeypatch.setattr("dgov.status._compute_freshness", freshness)
    monkeypatch.setattr("dgov.inspection._compute_freshness", freshness)
    monkeypatch.setattr("dgov.recovery._count_retries", count_retries)

    return {
        "get_pane": get_pane,
        "emit_event": emit_event,
        "read_events": read_events,
        "freshness": freshness,
        "count_retries": count_retries,
    }


class TestReviewWorkerPane:
    def test_happy_path_with_git_commits(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "feature work\n", "Add feature file")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }
        inspection_mocks["read_events"].return_value = [
            {"event": "pane_auto_responded", "pane": "worker-a"},
            {"event": "pane_auto_responded", "pane": "other-worker"},
        ]
        inspection_mocks["count_retries"].return_value = 2

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result["slug"] == "worker-a"
        assert result["branch"] == "worker-a"
        assert result["verdict"] == "safe"
        assert result["commit_count"] == 1
        assert result["files_changed"] == 1
        assert result["protected_touched"] == []
        assert result["uncommitted"] is False
        assert result["retry_count"] == 2
        assert result["auto_responses"] == 1
        assert result["freshness"] == "fresh"
        assert "feature.txt" in result["stat"]
        assert "Add feature file" in result["commit_log"]
        inspection_mocks["emit_event"].assert_called_once_with(
            str(tmp_path), "review_pass", "worker-a"
        )

    def test_missing_pane(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        inspection_mocks["get_pane"].return_value = None

        result = review_worker_pane(str(tmp_path), "missing", session_root=str(tmp_path))

        assert result == {"error": "Pane not found: missing"}

    def test_missing_worktree(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(tmp_path / "missing"),
            "branch_name": "worker-a",
            "base_sha": "abc123",
        }

        result = review_worker_pane(str(tmp_path), "worker-a", session_root=str(tmp_path))

        assert result == {"error": f"Worktree not found: {tmp_path / 'missing'}"}

    def test_no_base_sha(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": "",
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result == {"error": "No base_sha recorded — cannot compute diff"}

    def test_protected_files_touched(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "CLAUDE.md", "protected\n", "Touch protected file")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result["verdict"] == "review"
        assert result["protected_touched"] == ["CLAUDE.md"]
        assert result["issues"] == ["protected files touched: ['CLAUDE.md']"]
        assert result["commit_count"] == 1
        inspection_mocks["emit_event"].assert_called_once()
        args = inspection_mocks["emit_event"].call_args.args
        kwargs = inspection_mocks["emit_event"].call_args.kwargs
        assert args == (str(tmp_path), "review_fail", "worker-a")
        assert kwargs["issues"] == ["protected files touched: ['CLAUDE.md']"]

    def test_uncommitted_changes(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "committed\n", "Add committed change")
        (repo / "tracked.txt").write_text("base\nmodified but not committed\n")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result["verdict"] == "review"
        assert result["uncommitted"] is True
        assert result["issues"] == ["uncommitted changes (will be auto-committed on merge)"]

    def test_full_true_includes_diff_output(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "full.txt", "line from full diff\n", "Add full diff file")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path), full=True)

        assert "diff --git" in result["diff"]
        assert "+line from full diff" in result["diff"]


class TestDiffWorkerPane:
    def test_happy_path(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "feature work\n", "Add feature file")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "base_sha": base_sha,
        }

        result = diff_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result["slug"] == "worker-a"
        assert result["base_sha"] == base_sha
        assert "diff --git" in result["diff"]
        assert "+feature work" in result["diff"]

    def test_stat_mode(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "feature work\n", "Add feature file")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "base_sha": base_sha,
        }

        result = diff_worker_pane(str(repo), "worker-a", session_root=str(tmp_path), stat=True)

        assert "feature.txt" in result["diff"]
        assert "1 file changed" in result["diff"]

    def test_name_only_mode(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "feature work\n", "Add feature file")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "base_sha": base_sha,
        }

        result = diff_worker_pane(
            str(repo), "worker-a", session_root=str(tmp_path), name_only=True
        )

        assert result["diff"] == "feature.txt\n"

    def test_missing_pane(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        inspection_mocks["get_pane"].return_value = None

        result = diff_worker_pane(str(tmp_path), "missing", session_root=str(tmp_path))

        assert result == {"error": "Pane not found: missing"}

    def test_missing_worktree(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(tmp_path / "missing"),
            "base_sha": "abc123",
        }

        result = diff_worker_pane(str(tmp_path), "worker-a", session_root=str(tmp_path))

        assert result == {"error": f"Worktree not found: {tmp_path / 'missing'}"}

    def test_no_base_sha(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "base_sha": "",
        }

        result = diff_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result == {"error": "No base_sha recorded"}


class TestRebaseGovernor:
    def test_clean_rebase(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _git(repo, "checkout", "-b", "feature")
        _commit_file(repo, "feature.txt", "feature\n", "Add feature work")
        _git(repo, "checkout", "main")
        _commit_file(repo, "main.txt", "main\n", "Advance main")
        _git(repo, "checkout", "feature")

        result = rebase_governor(str(repo), onto="main")

        assert result == {"rebased": True, "base": "main", "stashed": False}
        assert (
            _git(repo, "merge-base", "--is-ancestor", "main", "HEAD", check=False).returncode == 0
        )
        assert _git(repo, "status", "--porcelain").stdout == ""

    def test_dirty_repo_stashes_then_rebases(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _git(repo, "checkout", "-b", "feature")
        _commit_file(repo, "feature.txt", "feature\n", "Add feature work")
        _git(repo, "checkout", "main")
        _commit_file(repo, "main.txt", "main\n", "Advance main")
        _git(repo, "checkout", "feature")
        (repo / "feature.txt").write_text("feature\nuncommitted\n")

        result = rebase_governor(str(repo), onto="main")

        assert result == {"rebased": True, "base": "main", "stashed": True}
        assert (repo / "feature.txt").read_text() == "feature\nuncommitted\n"
        assert _git(repo, "status", "--porcelain").stdout.strip() == "M feature.txt"
        assert _git(repo, "stash", "list").stdout.strip() == ""

    def test_conflict_aborts_and_pops_stash(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit_file(repo, "conflict.txt", "base\n", "Add conflict file")
        _git(repo, "checkout", "-b", "feature")
        _commit_file(repo, "conflict.txt", "feature\n", "Feature edits conflict")
        _git(repo, "checkout", "main")
        _commit_file(repo, "conflict.txt", "main\n", "Main edits conflict")
        _git(repo, "checkout", "feature")
        (repo / "dirty.txt").write_text("dirty\n")

        result = rebase_governor(str(repo), onto="main")

        assert result["rebased"] is False
        assert result["base"] == "main"
        assert result["stashed"] is True
        assert result["error"]
        assert (repo / "conflict.txt").read_text() == "feature\n"
        assert (repo / "dirty.txt").read_text() == "dirty\n"
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "feature"
        assert not (repo / ".git" / "rebase-merge").exists()
        assert _git(repo, "status", "--porcelain").stdout.strip() == "M dirty.txt"
        assert _git(repo, "stash", "list").stdout.strip() == ""
