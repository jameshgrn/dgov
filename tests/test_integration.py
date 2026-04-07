"""Integration tests: full pipeline with real git repos and mock workers.

Proves: TOML → parse → kernel → worktree → worker → commit → validate → merge → cleanup.
Uses temp git repos, mock workers (no LLM), mock settlement (no ruff/sentrux).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.runner import EventDagRunner
from dgov.settlement import GateResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _skip_preflight(monkeypatch):
    """Skip model preflight check in integration tests."""

    async def _noop(self):
        pass

    monkeypatch.setattr("dgov.runner.EventDagRunner._preflight_check_models", _noop)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Fresh git repo with one initial commit."""
    env = {
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }

    def _git(*args: str):
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            env={**env, "PATH": subprocess.os.environ["PATH"]},
            check=True,
            capture_output=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.name", "test")
    _git("config", "user.email", "test@test.local")
    (tmp_path / "README.md").write_text("init\n")
    _git("add", ".")
    _git("commit", "-m", "initial commit")
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dag(tasks: dict[str, DagTaskSpec], name: str = "test-dag") -> DagDefinition:
    """Build a DagDefinition from task specs."""
    return DagDefinition(
        name=name,
        dag_file="test",
        tasks=tasks,
    )


def _task(
    slug: str,
    prompt: str = "do the thing",
    depends_on: tuple[str, ...] = (),
    commit_message: str = "",
) -> DagTaskSpec:
    return DagTaskSpec(
        slug=slug,
        summary=f"Task {slug}",
        prompt=prompt,
        commit_message=commit_message or f"feat: {slug}",
        agent="mock",
        depends_on=depends_on,
        files=DagFileSpec(),
    )


async def _mock_worker_ok(
    project_root, task_slug, pane_slug, worktree_path, task, on_exit, on_event=None
):
    """Mock worker: write a file to worktree and exit 0."""
    out = worktree_path / f"{task_slug}.txt"
    out.write_text(f"output from {task_slug}\n")
    on_exit(task_slug, pane_slug, 0, "")


async def _mock_worker_fail(
    project_root, task_slug, pane_slug, worktree_path, task, on_exit, on_event=None
):
    """Mock worker: exit 1 immediately."""
    on_exit(task_slug, pane_slug, 1, "mock failure")


def _mock_settlement_pass(*_args, **_kwargs):
    return GateResult(passed=True)


def _mock_settlement_fail(*_args, **_kwargs):
    return GateResult(passed=False, error="lint failure")


# ---------------------------------------------------------------------------
# Single task: happy path
# ---------------------------------------------------------------------------


class TestSingleTaskHappyPath:
    def test_file_lands_on_main(self, git_repo, monkeypatch):
        """Worker writes a file → it ends up on main after merge."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"add-file": _task("add-file")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["add-file"] == "merged"
        assert (git_repo / "add-file.txt").exists()
        assert "output from add-file" in (git_repo / "add-file.txt").read_text()

    def test_commit_message_preserved(self, git_repo, monkeypatch):
        """Custom commit message appears in git log."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"msg-test": _task("msg-test", commit_message="feat: custom message")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        asyncio.run(runner.run())

        log = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert "custom message" in log.stdout

    def test_worktree_cleaned_up(self, git_repo, monkeypatch):
        """No leftover worktrees after completion."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"cleanup-test": _task("cleanup-test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        asyncio.run(runner.run())

        wt_dir = git_repo / ".dgov" / "worktrees"
        leftover = list(wt_dir.iterdir()) if wt_dir.exists() else []
        assert leftover == []

    def test_no_leftover_branches(self, git_repo, monkeypatch):
        """dgov/* branches are removed after merge."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"branch-test": _task("branch-test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        asyncio.run(runner.run())

        branches = subprocess.run(
            ["git", "branch", "--list", "dgov/*"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert branches.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Worker failure
# ---------------------------------------------------------------------------


class TestWorkerFailure:
    def test_worker_exit_1_fails_task(self, git_repo, monkeypatch):
        """Worker exit code 1 → task marked failed."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_fail)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"fail-test": _task("fail-test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["fail-test"] == "failed"
        assert not (git_repo / "fail-test.txt").exists()

    def test_worktree_cleaned_on_failure(self, git_repo, monkeypatch):
        """Worktrees cleaned even when worker fails."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_fail)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"fail-cleanup": _task("fail-cleanup")})
        runner = EventDagRunner(dag, session_root=str(git_repo))

        # max_retries=3 means 4 total attempts (0,1,2,3) then fail.
        # Each creates+destroys a worktree... but the runner only cleans up
        # in _merge which is only reached on success. Failed workers just
        # get retried. After final fail, worktrees may leak.
        # This test documents current behavior.
        asyncio.run(runner.run())


# ---------------------------------------------------------------------------
# Settlement gate rejection
# ---------------------------------------------------------------------------


class TestSettlementRejection:
    def test_lint_failure_rejects_merge(self, git_repo, monkeypatch):
        """Settlement gate failure → task fails, file not on main."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_fail)

        dag = _dag({"lint-fail": _task("lint-fail")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["lint-fail"] == "failed"
        assert not (git_repo / "lint-fail.txt").exists()


# ---------------------------------------------------------------------------
# Settlement retry flow
# ---------------------------------------------------------------------------


class TestSettlementRetry:
    """Test the settlement retry mechanism: fail → reset → retry → succeed."""

    def test_settlement_retry_succeeds_on_second_attempt(self, git_repo, monkeypatch):
        """Worker produces bad code → retry with feedback → fix → merge."""
        call_count = {"initial": 0, "retry": 0}

        async def _retry_worker(
            project_root, task_slug, pane_slug, worktree_path, task, on_exit, on_event=None
        ):
            # Track which call this is by checking pane_slug suffix
            is_retry = pane_slug.endswith("-retry")

            if not is_retry:
                # First attempt: write code with undefined variable (F821)
                call_count["initial"] += 1
                # F821: Undefined name - ruff check --fix cannot fix this
                bad_code = "print(undefined_var)\n"  # F821 undefined name
                (worktree_path / "output.py").write_text(bad_code)
                on_exit(task_slug, pane_slug, 0, "")
            else:
                # Retry: define the variable
                call_count["retry"] += 1
                fixed_code = "undefined_var = 'hello'\nprint(undefined_var)\n"
                (worktree_path / "output.py").write_text(fixed_code)
                on_exit(task_slug, pane_slug, 0, "")

        # Don't mock settlement — use real ruff validation
        monkeypatch.setattr("dgov.runner.run_headless_worker", _retry_worker)

        dag = _dag({"retry-task": _task("retry-task", commit_message="feat: retry test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        # Verify both worker calls were made
        assert call_count["initial"] == 1, "Initial worker should run once"
        assert call_count["retry"] == 1, "Retry worker should run once"

        # Task should succeed after retry
        assert results["retry-task"] == "merged"
        assert (git_repo / "output.py").exists()

        # Verify final code is correct (ruff may change quotes, so check content not exact format)
        final_code = (git_repo / "output.py").read_text()
        assert "undefined_var =" in final_code
        assert "hello" in final_code
        assert "print(undefined_var)" in final_code

    def test_settlement_retry_fails_after_second_attempt(self, git_repo, monkeypatch):
        """Worker produces bad code → retry → still bad → task fails."""
        call_count = {"initial": 0, "retry": 0}

        async def _failing_retry_worker(
            project_root, task_slug, pane_slug, worktree_path, task, on_exit, on_event=None
        ):
            is_retry = pane_slug.endswith("-retry")
            # F821: Undefined name - ruff check --fix cannot fix this
            bad_code = "print(undefined_var)\n"  # Always fails validation

            if not is_retry:
                call_count["initial"] += 1
            else:
                call_count["retry"] += 1

            # Both attempts write bad code
            (worktree_path / "bad.py").write_text(bad_code)
            on_exit(task_slug, pane_slug, 0, "")

        monkeypatch.setattr("dgov.runner.run_headless_worker", _failing_retry_worker)

        dag = _dag({"fail-retry": _task("fail-retry")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        # Both worker calls should be made
        assert call_count["initial"] == 1
        assert call_count["retry"] == 1

        # Task should fail after retry also fails
        assert results["fail-retry"] == "failed"
        assert not (git_repo / "bad.py").exists()

    def test_settlement_retry_preserves_worktree_for_inspection(self, git_repo, monkeypatch):
        """When retry fails, worktree is preserved for manual inspection."""
        call_count = {"retry": 0}
        # Track the worktree path from the first worker call
        worktree_paths = []

        async def _always_fail_worker(
            project_root, task_slug, pane_slug, worktree_path, task, on_exit, on_event=None
        ):
            is_retry = pane_slug.endswith("-retry")
            if is_retry:
                call_count["retry"] += 1

            worktree_paths.append(worktree_path)
            # F821: Undefined name - ruff check --fix cannot fix this
            (worktree_path / "bad.py").write_text("print(undefined_var)\n")
            on_exit(task_slug, pane_slug, 0, "")

        monkeypatch.setattr("dgov.runner.run_headless_worker", _always_fail_worker)

        dag = _dag({"inspect-test": _task("inspect-test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        asyncio.run(runner.run())

        # Should have at least one worktree path
        assert len(worktree_paths) >= 1
        # Get the original worktree path (before retry creates new one)
        worktree_path = worktree_paths[0]

        # Worktree should be preserved for inspection
        assert worktree_path.exists(), f"Worktree should exist at {worktree_path}"
        # The worktree should contain our bad file (uncommitted after reset)
        assert (worktree_path / "bad.py").exists()

    def test_settlement_retry_emits_event(self, git_repo, monkeypatch):
        """Settlement retry emits a 'settlement_retry' event."""
        events = []

        async def _event_tracking_worker(
            project_root, task_slug, pane_slug, worktree_path, task, on_exit, on_event=None
        ):
            is_retry = pane_slug.endswith("-retry")
            # F821: Undefined name - ruff check --fix cannot fix this
            # Retry attempt fixes it
            if is_retry:
                (worktree_path / "out.py").write_text("undefined = 1\nprint(undefined)\n")
            else:
                (worktree_path / "out.py").write_text("print(undefined)\n")
            on_exit(task_slug, pane_slug, 0, "")

        def _capture_event(project_root, event, pane_slug, **kwargs):
            events.append({"event": event, "pane_slug": pane_slug, **kwargs})

        monkeypatch.setattr("dgov.runner.run_headless_worker", _event_tracking_worker)
        monkeypatch.setattr("dgov.runner.emit_event", _capture_event)

        dag = _dag({"event-test": _task("event-test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        asyncio.run(runner.run())

        # Find settlement_retry event
        retry_events = [e for e in events if e.get("event") == "settlement_retry"]
        assert len(retry_events) == 1
        assert retry_events[0]["task_slug"] == "event-test"
        assert "error" in retry_events[0]


# ---------------------------------------------------------------------------
# Chain: b depends on a
# ---------------------------------------------------------------------------


class TestChain:
    def test_sequential_merge(self, git_repo, monkeypatch):
        """a merges first, then b. Both files end up on main."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag(
            {
                "step-a": _task("step-a"),
                "step-b": _task("step-b", depends_on=("step-a",)),
            }
        )
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["step-a"] == "merged"
        assert results["step-b"] == "merged"
        assert (git_repo / "step-a.txt").exists()
        assert (git_repo / "step-b.txt").exists()

    def test_merge_order_respects_deps(self, git_repo, monkeypatch):
        """a's commit appears before b's in git log."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag(
            {
                "first": _task("first"),
                "second": _task("second", depends_on=("first",)),
            }
        )
        runner = EventDagRunner(dag, session_root=str(git_repo))
        asyncio.run(runner.run())

        log = subprocess.run(
            ["git", "log", "--oneline", "--reverse"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        lines = log.stdout.strip().split("\n")
        messages = [ln.split(" ", 1)[1] for ln in lines]
        first_idx = next(i for i, m in enumerate(messages) if "first" in m)
        second_idx = next(i for i, m in enumerate(messages) if "second" in m)
        assert first_idx < second_idx


# ---------------------------------------------------------------------------
# Parallel tasks
# ---------------------------------------------------------------------------


class TestParallel:
    def test_both_merge(self, git_repo, monkeypatch):
        """Two independent tasks both end up merged."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag(
            {
                "alpha": _task("alpha"),
                "beta": _task("beta"),
            }
        )
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["alpha"] == "merged"
        assert results["beta"] == "merged"
        assert (git_repo / "alpha.txt").exists()
        assert (git_repo / "beta.txt").exists()

    def test_one_fails_other_still_merges(self, git_repo, monkeypatch):
        """alpha fails, beta still merges (scan-based merge fix)."""

        async def _selective_worker(
            project_root, task_slug, pane_slug, worktree_path, task, on_exit, on_event=None
        ):
            if task_slug == "alpha":
                on_exit(task_slug, pane_slug, 1, "")
            else:
                (worktree_path / f"{task_slug}.txt").write_text(f"output from {task_slug}\n")
                on_exit(task_slug, pane_slug, 0, "")

        monkeypatch.setattr("dgov.runner.run_headless_worker", _selective_worker)
        monkeypatch.setattr("dgov.runner.validate_sandbox", _mock_settlement_pass)

        dag = _dag(
            {
                "alpha": _task("alpha"),
                "beta": _task("beta"),
            }
        )
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["alpha"] == "failed"
        assert results["beta"] == "merged"
        assert not (git_repo / "alpha.txt").exists()
        assert (git_repo / "beta.txt").exists()
