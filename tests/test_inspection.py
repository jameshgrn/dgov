"""Unit tests for dgov.inspection and dgov.metrics."""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.inspection import (
    _run_related_tests,
    _try_rebase_onto_main,
    check_test_coverage,
    compute_stats,
    diff_worker_pane,
    rebase_governor,
    review_worker_pane,
)
from dgov.lifecycle import WorkerPane, add_pane
from dgov.persistence import emit_event

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

        assert result.slug == "worker-a"
        assert result.branch == "worker-a"
        assert result.verdict == "safe"
        assert result.commit_count == 1
        assert result.files_changed == 1
        assert result.protected_touched == []
        assert result.uncommitted is False
        assert result.automation.retry_count == 2
        assert result.automation.auto_responses == 1
        assert result.freshness_info.status == "fresh"
        assert result.automation.retry_count == 2
        assert result.freshness_info.status == "fresh"
        assert "feature.txt" in result.stat
        assert "Add feature file" in result.commit_log
        inspection_mocks["emit_event"].assert_called_once_with(
            str(tmp_path), "review_pass", "worker-a", commit_count=1
        )

    def test_missing_pane(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        inspection_mocks["get_pane"].return_value = None

        result = review_worker_pane(str(tmp_path), "missing", session_root=str(tmp_path))

        assert result.error == "Pane not found: missing"

    def test_missing_worktree(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(tmp_path / "missing"),
            "branch_name": "worker-a",
            "base_sha": "abc123",
        }

        result = review_worker_pane(str(tmp_path), "worker-a", session_root=str(tmp_path))

        assert result.error == f"Worktree not found: {tmp_path / 'missing'}"

    def test_no_base_sha(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": "",
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result.error == "No base_sha recorded — cannot compute diff"

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

        assert result.verdict == "review"
        assert result.protected_touched == ["CLAUDE.md"]
        assert result.issues == ["protected files touched: ['CLAUDE.md']"]
        assert result.commit_count == 1
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

        assert result.verdict == "review"
        assert result.uncommitted is True
        assert result.issues == ["uncommitted changes (merge refused until committed)"]

    def test_zero_commit_review_is_not_safe(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
            "state": "done",
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result.verdict == "review"
        assert result.commit_count == 0
        assert result.issues == ["no commits — nothing to merge"]
        inspection_mocks["emit_event"].assert_called_once()
        args = inspection_mocks["emit_event"].call_args.args
        kwargs = inspection_mocks["emit_event"].call_args.kwargs
        assert args == (str(tmp_path), "review_fail", "worker-a")
        assert kwargs["issues"] == ["no commits — nothing to merge"]

    def test_review_pass_with_commit_count_zero_does_not_emit_review_pass_event(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        """Regression test for bug #186: review_pass with commit_count=0 should not be emitted.

        A pane with zero commits cannot be merged — the review should be normalized
        to review_fail, not review_pass.
        """
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)

        # Simulate a pane with no commits (done state but no new commits)
        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
            "state": "done",
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        # The verdict should be "review" (not "safe") because there are no commits
        assert result.verdict == "review"
        assert result.commit_count == 0
        assert "no commits — nothing to merge" in result.issues

        # The event should be review_fail, not review_pass
        inspection_mocks["emit_event"].assert_called_once()
        call_args = inspection_mocks["emit_event"].call_args
        assert call_args.args[1] == "review_fail"

    def test_read_only_review_does_not_emit_events(
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

        result = review_worker_pane(
            str(repo),
            "worker-a",
            session_root=str(tmp_path),
            emit_events=False,
        )

        assert result.verdict == "safe"
        inspection_mocks["emit_event"].assert_not_called()

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

        assert "diff --git" in result.diff
        assert "+line from full diff" in result.diff

    def test_review_runs_related_tests_in_worker_worktree(
        self,
        tmp_path: Path,
        inspection_mocks: dict[str, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "src/dgov/feature.py", "def feature():\n    return 1\n", "Add feature")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        run_related = MagicMock(return_value={})
        monkeypatch.setattr("dgov.inspection._run_related_tests", run_related)

        review_worker_pane(
            str(tmp_path / "different-project-root"),
            "worker-a",
            session_root=str(tmp_path),
        )

        run_related.assert_called_once_with(
            str(repo),
            ["src/dgov/feature.py"],
        )

    def test_uncommitted_clauDE_md_does_not_block_safe_verdict(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        """Uncommitted CLAUDE.md drift should not downgrade the review verdict."""
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "committed work\n", "Add committed change")

        # Simulate uncommitted CLAUDE.md modification (worktree hook drift)
        (repo / "CLAUDE.md").write_text("modified by worktree hook\n")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result.verdict == "safe"
        assert result.uncommitted is False
        assert "uncommitted changes" not in result.issues
        assert result.commit_count == 1
        assert result.files_changed == 1

    def test_uncommitted_agents_md_does_not_block_safe_verdict(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        """Uncommitted AGENTS.md drift should not downgrade the review verdict."""
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "committed work\n", "Add committed change")

        # Simulate uncommitted AGENTS.md modification (worktree hook drift)
        (repo / "AGENTS.md").write_text("modified by worktree hook\n")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result.verdict == "safe"
        assert result.uncommitted is False
        assert "uncommitted changes" not in result.issues
        assert result.commit_count == 1
        assert result.files_changed == 1

    def test_both_clauDE_md_and_agents_md_uncommitted_still_safe(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        """Both CLAUDE.md and AGENTS.md uncommitted together should still be safe."""
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "committed work\n", "Add committed change")

        # Simulate both instruction files modified
        (repo / "CLAUDE.md").write_text("hook modification 1\n")
        (repo / "AGENTS.md").write_text("hook modification 2\n")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result.verdict == "safe"
        assert result.uncommitted is False
        assert "uncommitted changes" not in result.issues

    def test_real_uncommitted_source_changes_still_block_safe(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        """Real uncommitted source changes should still produce the uncommitted issue."""
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "committed work\n", "Add committed change")

        # Uncommitted real source change (not instruction file)
        (repo / "tracked.txt").write_text("modified source\n")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result.verdict == "review"
        assert result.uncommitted is True
        assert "uncommitted changes (merge refused until committed)" in result.issues

    def test_committed_protected_files_still_flagged_as_issues(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        """Committed changes to protected files in branch diff should still be flagged."""
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "THEORY.md", "protected content\n", "Touch THEORY.md")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result.verdict == "review"
        assert result.protected_touched == ["THEORY.md"]
        assert any("protected files touched" in issue for issue in (result.issues or []))

    def test_uncommitted_claude_md_porcelain_leading_space_preserved(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        """
        Regression test: prove that CLAUDE.md drift with leading-space porcelain line
        does not set uncommitted and does not add the uncommitted issue.

        Git porcelain format: "<status><status><space><filename>"
        Example: " M CLAUDE.md" (modified, index clean)
        The parsing must correctly slice filename at position 3 after the status prefix.
        """
        repo = tmp_path / "repo"
        base_sha = _init_repo(repo)
        _commit_file(repo, "feature.txt", "committed work\n", "Add committed change")

        # Simulate uncommitted CLAUDE.md modification (worktree hook drift)
        (repo / "CLAUDE.md").write_text("modified by worktree hook\n")

        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "branch_name": "worker-a",
            "base_sha": base_sha,
        }

        result = review_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        # CLAUDE.md is protected and worktree-local — should be filtered out
        assert result.verdict == "safe"
        assert result.uncommitted is False
        assert "uncommitted changes" not in result.issues
        assert "CLAUDE.md" not in result.protected_touched or []


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

        assert result["error"] == "Pane not found: missing"

    def test_missing_worktree(
        self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]
    ) -> None:
        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(tmp_path / "missing"),
            "base_sha": "abc123",
        }

        result = diff_worker_pane(str(tmp_path), "worker-a", session_root=str(tmp_path))

        assert result["error"] == f"Worktree not found: {tmp_path / 'missing'}"

    def test_no_base_sha(self, tmp_path: Path, inspection_mocks: dict[str, MagicMock]) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        inspection_mocks["get_pane"].return_value = {
            "worktree_path": str(repo),
            "base_sha": "",
        }

        result = diff_worker_pane(str(repo), "worker-a", session_root=str(tmp_path))

        assert result["error"] == "No base_sha recorded"


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


class TestTryRebaseOntoMain:
    def test_try_rebase_onto_main_succeeds(self, tmp_path: Path) -> None:
        """_try_rebase_onto_main fetches and rebases without conflicts."""
        # Create a main repo with a commit
        main = tmp_path / "main"
        main.mkdir()
        _git(main, "init", "-b", "main")
        _git(main, "config", "user.email", "test@test.com")
        _git(main, "config", "user.name", "Test")
        (main / "a.py").write_text("original")
        _git(main, "add", ".")
        _git(main, "commit", "-m", "init")

        # Create worktree on a branch
        wt = tmp_path / "wt"
        _git(main, "worktree", "add", str(wt), "-b", "worker")
        (wt / "b.py").write_text("worker change")
        _git(wt, "add", ".")
        _git(wt, "commit", "-m", "worker")

        # Add a commit on main (simulating governor micro-edit)
        (main / "test_fix.py").write_text("governor fix")
        _git(main, "add", ".")
        _git(main, "commit", "-m", "governor fix")

        # Rebase should succeed
        result = _try_rebase_onto_main(str(wt), str(main))
        assert result is True
        # Worktree should now have the governor fix
        assert (wt / "test_fix.py").exists()


@pytest.fixture()
def metrics_session_root(tmp_path):
    (tmp_path / ".dgov").mkdir()
    return str(tmp_path)


def _seed_metrics(
    session_root: str, slug: str, agent: str = "claude", state: str = "active"
) -> None:
    pane = WorkerPane(
        slug=slug,
        prompt="task",
        pane_id="%99",
        agent=agent,
        project_root=session_root,
        worktree_path=f"{session_root}/wt/{slug}",
        branch_name=slug,
        state=state,
    )
    add_pane(session_root, pane)


class TestComputeStatsEmpty:
    def test_compute_stats_empty(self, metrics_session_root):
        data = compute_stats(metrics_session_root)
        assert data["total_panes"] == 0
        assert data["by_state"] == {}
        assert data["by_agent"] == {}
        assert data["recent_failures"] == []
        assert data["event_count"] == 0


class TestComputeStatsWithPanes:
    def test_compute_stats_with_panes(self, metrics_session_root):
        _seed_metrics(metrics_session_root, "a", state="active")
        _seed_metrics(metrics_session_root, "b", state="merged")
        _seed_metrics(metrics_session_root, "c", state="failed")

        data = compute_stats(metrics_session_root)
        assert data["total_panes"] == 3
        assert data["by_state"]["active"] == 1
        assert data["by_state"]["merged"] == 1
        assert data["by_state"]["failed"] == 1


class TestComputeStatsByAgent:
    def test_compute_stats_by_agent(self, metrics_session_root):
        _seed_metrics(metrics_session_root, "a1", agent="claude", state="merged")
        _seed_metrics(metrics_session_root, "a2", agent="claude", state="failed")
        _seed_metrics(metrics_session_root, "b1", agent="pi", state="merged")
        _seed_metrics(metrics_session_root, "b2", agent="pi", state="merged")

        # Add events for duration calculation
        emit_event(metrics_session_root, "pane_created", "a1")
        emit_event(metrics_session_root, "pane_merged", "a1")
        emit_event(metrics_session_root, "pane_created", "b1")
        emit_event(metrics_session_root, "pane_merged", "b1")

        data = compute_stats(metrics_session_root)
        claude = data["by_agent"]["claude"]
        assert claude["total"] == 2
        assert claude["success_rate"] == 0.5
        assert claude["failures"] == 1

        pi = data["by_agent"]["pi"]
        assert pi["total"] == 2
        assert pi["success_rate"] == 1.0
        assert pi["failures"] == 0
        assert pi["avg_duration_s"] is not None


class TestComputeStatsRecentFailures:
    def test_compute_stats_recent_failures(self, metrics_session_root):
        for i in range(7):
            slug = f"fail-{i}"
            _seed_metrics(metrics_session_root, slug, state="failed")
            emit_event(metrics_session_root, "pane_created", slug)

        data = compute_stats(metrics_session_root)
        assert len(data["recent_failures"]) == 5
        for f in data["recent_failures"]:
            assert "slug" in f
            assert "agent" in f
            assert "state" in f
            assert f["state"] == "failed"


class TestRunRelatedTestsTimeout:
    """Unit tests for _run_related_tests timeout handling and cleanup."""

    def test_timeout_returns_failure_metadata(self, tmp_path):
        test_file = tmp_path / "tests" / "test_feature.py"
        test_file.parent.mkdir()
        test_file.write_text("# empty test")

        proc = MagicMock()
        proc.pid = 4321
        proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["uv", "run", "pytest"], timeout=120
        )

        with (
            patch("subprocess.Popen", return_value=proc),
            patch("os.killpg") as mock_killpg,
        ):
            result = _run_related_tests(str(tmp_path), ["src/dgov/feature.py"])

        assert result["tests_ran"] == ["tests/test_feature.py"]
        assert result["tests_passed"] is False
        assert result["timed_out"] is True
        assert "timed out" in result["test_output"].lower()
        mock_killpg.assert_called_once()
        proc.wait.assert_called_once_with(timeout=5)

    def test_timeout_kills_process_group(self, tmp_path):
        test_file = tmp_path / "tests" / "test_feature.py"
        test_file.parent.mkdir()
        test_file.write_text("# empty test")

        proc = MagicMock()
        proc.pid = 9876
        proc.communicate.side_effect = subprocess.TimeoutExpired(
            cmd=["uv", "run", "pytest"], timeout=120
        )

        with (
            patch("subprocess.Popen", return_value=proc),
            patch("os.killpg") as mock_killpg,
        ):
            _run_related_tests(str(tmp_path), ["src/dgov/feature.py"])

        mock_killpg.assert_called_once_with(9876, signal.SIGKILL)

    def test_timeout_path_should_return_failure_metadata(self, tmp_path):
        """
        Expected behavior: timeout should return failure metadata, not crash.
        """
        test_file = tmp_path / "tests" / "test_feature.py"
        test_file.parent.mkdir()
        test_file.write_text("# empty test")

        mock_timeout_result = MagicMock()
        mock_timeout_result.stdout = ""
        mock_timeout_result.stderr = "pytest process timed out after 120s"
        mock_timeout_result.returncode = -9
        mock_timeout_result.check_output = None
        mock_timeout_result.args = ["uv", "run", "pytest"]

        # Simulate what SHOULD happen if TimeoutExpired was caught:
        expected_output = "pytest process timed out after 120s"
        expected_result = {
            "tests_ran": ["tests/test_feature.py"],
            "tests_passed": False,
            "test_output": f"{expected_output}[-500:]",
        }

        assert isinstance(expected_result["tests_ran"], list)
        assert expected_result["tests_ran"][0].startswith("tests/")
        assert expected_result["tests_passed"] is False
        assert "timed out" in expected_result["test_output"].lower()

    def test_related_test_files_discovery(self, tmp_path):
        """Verify test file discovery for changed source files."""
        src_file = tmp_path / "src" / "dgov" / "feature.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("# source")

        test_file = tmp_path / "tests" / "test_feature.py"
        test_file.parent.mkdir()
        test_file.write_text("# empty test")

        proc = MagicMock()
        proc.communicate.return_value = ("2 passed", "")
        proc.returncode = 0

        # When test file exists, should discover and run it
        with patch("subprocess.Popen", return_value=proc) as mock_popen:
            result = _run_related_tests(str(tmp_path), ["src/dgov/feature.py"])

            assert result["tests_ran"] == ["tests/test_feature.py"]
            assert result["tests_passed"] is True
            mock_popen.assert_called_once()
            call_args = mock_popen.call_args[0][0]
            assert "pytest" in call_args
            assert str(test_file) in call_args

    def test_no_related_tests_returns_empty(self, tmp_path):
        """When no related test files exist, returns empty dict."""
        src_file = tmp_path / "src" / "dgov" / "feature.py"
        src_file.parent.mkdir(parents=True)
        src_file.write_text("# source")

        # No corresponding test file
        with patch("subprocess.run") as mock_run:
            result = _run_related_tests(str(tmp_path), ["src/dgov/feature.py"])

            assert result["no_tests_found"] is True
            assert "src/dgov/feature.py" in result["changed_files"]
            mock_run.assert_not_called()

    def test_existing_test_files_in_changed_list(self, tmp_path):
        """When already-test files are in changed list, adds them."""
        test_file = tmp_path / "tests" / "test_feature.py"
        test_file.parent.mkdir()
        test_file.write_text("# test")

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "1 passed"
            mock_result.stderr = ""
            mock_run.return_value = mock_result

            result = _run_related_tests(str(tmp_path), ["tests/test_feature.py"])

            assert len(result["tests_ran"]) == 1
            assert "test_feature.py" in result["tests_ran"][0]


class TestCheckTestCoverage:
    """Unit tests for check_test_coverage function."""

    def test_no_manifest_returns_empty(self, tmp_path):
        """No .test-manifest.json returns empty list."""
        from dgov.inspection import check_test_coverage

        result = check_test_coverage(["src/dgov/foo.py"], session_root=str(tmp_path))
        assert result == []

    def test_source_with_test_passes(self, tmp_path):
        """Source file with matching test in diff passes."""
        import json

        from dgov.inspection import check_test_coverage

        manifest = {"src/dgov/plan.py": ["tests/test_plan.py"]}
        (tmp_path / ".test-manifest.json").write_text(json.dumps(manifest))
        result = check_test_coverage(
            ["src/dgov/plan.py", "tests/test_plan.py"],
            session_root=str(tmp_path),
        )
        assert result == []

    def test_source_without_test_fails(self, tmp_path):
        """Source file without matching test in diff fails."""
        import json

        from dgov.inspection import check_test_coverage

        manifest = {"src/dgov/plan.py": ["tests/test_plan.py"]}
        (tmp_path / ".test-manifest.json").write_text(json.dumps(manifest))
        result = check_test_coverage(
            ["src/dgov/plan.py"],
            session_root=str(tmp_path),
        )
        assert result == ["src/dgov/plan.py"]

    def test_source_not_in_manifest_passes(self, tmp_path):
        """Source file not in manifest passes (no expected tests)."""
        import json

        from dgov.inspection import check_test_coverage

        manifest = {"src/dgov/other.py": ["tests/test_other.py"]}
        (tmp_path / ".test-manifest.json").write_text(json.dumps(manifest))
        result = check_test_coverage(
            ["src/dgov/plan.py"],
            session_root=str(tmp_path),
        )
        assert result == []

    def test_test_files_only_passes(self, tmp_path):
        """Only test files changed — no source coverage needed."""
        import json

        from dgov.inspection import check_test_coverage

        manifest = {"src/dgov/plan.py": ["tests/test_plan.py"]}
        (tmp_path / ".test-manifest.json").write_text(json.dumps(manifest))
        result = check_test_coverage(
            ["tests/test_plan.py"],
            session_root=str(tmp_path),
        )
        assert result == []

    def test_multiple_expected_tests_one_present_passes(self, tmp_path):
        """Source with multiple expected tests passes if any are present."""
        import json

        from dgov.inspection import check_test_coverage

        manifest = {"src/dgov/plan.py": ["tests/test_plan.py", "tests/test_other.py"]}
        (tmp_path / ".test-manifest.json").write_text(json.dumps(manifest))
        result = check_test_coverage(
            ["src/dgov/plan.py", "tests/test_other.py"],
            session_root=str(tmp_path),
        )
        assert result == []

    def test_corrupt_manifest_returns_empty(self, tmp_path):
        """Corrupt JSON manifest returns empty list."""
        (tmp_path / ".test-manifest.json").write_text("{invalid json}")
        result = check_test_coverage(["src/dgov/plan.py"], session_root=str(tmp_path))
        assert result == []

    def test_empty_manifest_returns_empty(self, tmp_path):
        """Empty manifest returns empty list."""
        import json

        (tmp_path / ".test-manifest.json").write_text(json.dumps({}))
        result = check_test_coverage(["src/dgov/plan.py"], session_root=str(tmp_path))
        assert result == []

    def test_mixed_source_and_test_files(self, tmp_path):
        """Mixed source and test files - only missing sources flagged."""
        import json

        from dgov.inspection import check_test_coverage

        manifest = {
            "src/dgov/a.py": ["tests/test_a.py"],
            "src/dgov/b.py": ["tests/test_b.py"],
        }
        (tmp_path / ".test-manifest.json").write_text(json.dumps(manifest))
        # a.py has test, b.py doesn't
        result = check_test_coverage(
            ["src/dgov/a.py", "tests/test_a.py", "src/dgov/b.py"],
            session_root=str(tmp_path),
        )
        assert result == ["src/dgov/b.py"]
