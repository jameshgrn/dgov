"""Integration tests: full pipeline with real git repos and mock workers.

Proves: TOML → parse → kernel → worktree → worker → commit → validate → merge → cleanup.
Uses temp git repos, mock workers (no LLM), mock settlement (no ruff/sentrux).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.event_types import EvtTaskDispatched
from dgov.runner import EventDagRunner
from dgov.settlement import GateResult

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_with_api_key(monkeypatch):
    """Set FIREWORKS_API_KEY for preflight check."""
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key-fake")


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
            env={**env, "PATH": os.environ["PATH"]},
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
    project_root,
    plan_name,
    task_slug,
    pane_slug,
    worktree_path,
    task,
    task_scope,
    on_exit,
    on_event=None,
):
    """Mock worker: write a file to worktree and exit 0."""
    out = worktree_path / f"{task_slug}.txt"
    out.write_text(f"output from {task_slug}\n")
    on_exit(task_slug, pane_slug, 0, "")


async def _mock_worker_fail(
    project_root,
    plan_name,
    task_slug,
    pane_slug,
    worktree_path,
    task,
    task_scope,
    on_exit,
    on_event=None,
):
    """Mock worker: exit 1 immediately."""
    on_exit(task_slug, pane_slug, 1, "mock failure")


def _mock_settlement_pass(*_args, **_kwargs):
    return GateResult(passed=True)


def _mock_settlement_fail(*_args, **_kwargs):
    return GateResult(passed=False, error="lint failure")


_SEMANTIC_EVENT_FIELDS = (
    "task_slug",
    "error",
    "pane",
    "failure_class",
    "gate_name",
    "risk_level",
    "python_overlap_detected",
)


def _semantic_gate_worker(code: str, filename: str = "module.py"):
    async def _worker(
        project_root,
        plan_name,
        task_slug,
        pane_slug,
        worktree_path,
        task,
        task_scope,
        on_exit,
        on_event=None,
    ):
        (worktree_path / filename).write_text(code)
        on_exit(task_slug, pane_slug, 0, "")

    return _worker


def _semantic_event_capture(events: list[dict]):
    def _capture_event(project_root, event, pane_slug="", **kwargs):
        events.append(_semantic_event_record(event, pane_slug, kwargs))

    return _capture_event


def _semantic_event_record(event, pane_slug: str, kwargs: dict) -> dict:
    if isinstance(event, str):
        return {"event": event, "pane_slug": pane_slug, **kwargs}
    return {
        "event": getattr(event, "event_type", ""),
        **{field: getattr(event, field, None) for field in _SEMANTIC_EVENT_FIELDS},
    }


def _same_symbol_edit_verdict(*_args, **_kwargs):
    from dgov.semantic_settlement import FailureClass, SemanticGateVerdict, SymbolOverlap

    return SemanticGateVerdict(
        task_slug="same-edit-test",
        gate_name="same_symbol_edit",
        passed=False,
        failure_class=FailureClass.SAME_SYMBOL_EDIT,
        evidence=(
            SymbolOverlap(
                symbol_name="process",
                symbol_type="function",
                file_path="module.py",
                task_line_range=(1, 2),
                target_line_range=(1, 2),
            ),
        ),
        error_message="Both sides modified 'process'",
        checked_at=0.0,
    )


def _signature_drift_verdict(*_args, **_kwargs):
    from dgov.semantic_settlement import FailureClass, SemanticGateVerdict, SignatureDrift

    return SemanticGateVerdict(
        task_slug="drift-test",
        gate_name="signature_drift",
        passed=False,
        failure_class=FailureClass.SIGNATURE_DRIFT,
        evidence=(
            SignatureDrift(
                symbol_name="helper",
                file_path="module.py",
                base_signature="def helper()",
                integrated_signature="def helper(x)",
            ),
        ),
        error_message="Signature changed for 'helper'",
        checked_at=0.0,
    )


def _semantic_gate_rejections(events: list[dict]) -> list[dict]:
    return [event for event in events if event.get("event") == "semantic_gate_rejected"]


def _retry_worker(initial_code: str, retry_code: str, filename: str = "out.py"):
    """Factory for a worker that writes different code on initial vs retry attempts.

    Detects retry by checking if pane_slug ends with '-retry'.
    """

    async def _worker(
        project_root,
        plan_name,
        task_slug,
        pane_slug,
        worktree_path,
        task,
        task_scope,
        on_exit,
        on_event=None,
    ):
        is_retry = pane_slug.endswith("-retry")
        code = retry_code if is_retry else initial_code
        (worktree_path / filename).write_text(code)
        on_exit(task_slug, pane_slug, 0, "")

    return _worker


def _retry_worker_with_counts(initial_code: str, retry_code: str, filename: str = "out.py"):
    """Factory for a worker that writes different code on initial vs retry attempts.

    Detects retry by checking if pane_slug ends with '-retry'.
    Returns the worker and a call_count dict for assertions.
    """
    call_count = {"initial": 0, "retry": 0}

    async def _worker(
        project_root,
        plan_name,
        task_slug,
        pane_slug,
        worktree_path,
        task,
        task_scope,
        on_exit,
        on_event=None,
    ):
        is_retry = pane_slug.endswith("-retry")
        code = retry_code if is_retry else initial_code
        call_count["retry" if is_retry else "initial"] += 1
        (worktree_path / filename).write_text(code)
        on_exit(task_slug, pane_slug, 0, "")

    return _worker, call_count


def _assert_retry_success(
    git_repo: Path,
    results: dict,
    task_slug: str,
    call_count: dict,
    filename: str,
    expected_content: str,
):
    """Assert retry call counts and final merged output content."""
    assert call_count["initial"] == 1, "Initial worker should run once"
    assert call_count["retry"] == 1, "Retry worker should run once"
    assert results[task_slug] == "merged"
    assert (git_repo / filename).exists()
    final_code = (git_repo / filename).read_text()
    assert expected_content in final_code


def _assert_settlement_retry_event(events: list[dict], task_slug: str) -> dict:
    """Assert exactly one settlement_retry event exists for the given task_slug.

    Returns the matched event for additional assertions.
    """
    retry_events = [e for e in events if e.get("event") == "settlement_retry"]
    assert len(retry_events) == 1, (
        f"Expected exactly one settlement_retry event, got {len(retry_events)}"
    )
    event = retry_events[0]
    actual_slug = event.get("task_slug")
    assert actual_slug == task_slug, f"Expected task_slug={task_slug}, got {actual_slug}"
    assert "error" in event, "Expected 'error' field in settlement_retry event"
    return event


def _record_active_orphan(session_root: str, git_repo: Path, dag, slug: str, pane: str):
    """Simulate a crashed run: record an ACTIVE task and emit dispatched event."""
    from dgov.persistence import WorkerTask, emit_event, record_runtime_artifact
    from dgov.types import TaskState

    record = WorkerTask(
        slug=slug,
        prompt="do the thing",
        agent="mock",
        project_root=session_root,
        worktree_path=str(git_repo / ".dgov" / "worktrees" / slug),
        branch_name=f"dgov/{slug}",
        state=TaskState.ACTIVE,
        plan_name=dag.name,
    )
    record_runtime_artifact(session_root, record)
    emit_event(
        session_root,
        EvtTaskDispatched(
            pane=pane,
            plan_name=dag.name,
            task_slug=slug,
            agent="mock",
        ),
    )


# ---------------------------------------------------------------------------
# Single task: happy path
# ---------------------------------------------------------------------------


class TestSingleTaskHappyPath:
    def test_file_lands_on_main(self, git_repo, monkeypatch):
        """Worker writes a file → it ends up on main after merge."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"add-file": _task("add-file")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["add-file"] == "merged"
        assert (git_repo / "add-file.txt").exists()
        assert "output from add-file" in (git_repo / "add-file.txt").read_text()

    def test_commit_message_preserved(self, git_repo, monkeypatch):
        """Custom commit message appears in git log."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

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
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"cleanup-test": _task("cleanup-test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        asyncio.run(runner.run())

        wt_dir = git_repo / ".dgov" / "worktrees"
        leftover = list(wt_dir.iterdir()) if wt_dir.exists() else []
        assert leftover == []

    def test_no_leftover_branches(self, git_repo, monkeypatch):
        """dgov/* branches are removed after merge."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

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
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"fail-test": _task("fail-test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["fail-test"] == "failed"
        assert not (git_repo / "fail-test.txt").exists()

    def test_worktree_cleaned_on_failure(self, git_repo, monkeypatch):
        """Worktrees cleaned even when worker fails and max retries reached."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_fail)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"fail-cleanup": _task("fail-cleanup")})
        runner = EventDagRunner(dag, session_root=str(git_repo))

        asyncio.run(runner.run())

        # Now worktrees should be cleaned up even on terminal failure
        wt_dir = git_repo.parent / f".dgov-worktrees-{git_repo.name}"
        leftover = list(wt_dir.iterdir()) if wt_dir.exists() else []
        assert leftover == []


# ---------------------------------------------------------------------------
# Settlement gate rejection
# ---------------------------------------------------------------------------


class TestSettlementRejection:
    def test_lint_failure_rejects_merge(self, git_repo, monkeypatch):
        """Settlement gate failure → task fails, file not on main."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_fail)

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
        bad_code = "print(undefined_var)\n"  # F821: Undefined name
        fixed_code = "undefined_var = 'hello'\nprint(undefined_var)\n"

        worker, call_count = _retry_worker_with_counts(
            initial_code=bad_code,
            retry_code=fixed_code,
            filename="output.py",
        )

        monkeypatch.setattr("dgov.runner.run_headless_worker", worker)

        dag = _dag({"retry-task": _task("retry-task", commit_message="feat: retry test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        _assert_retry_success(
            git_repo,
            results,
            task_slug="retry-task",
            call_count=call_count,
            filename="output.py",
            expected_content="undefined_var",
        )

    def test_settlement_retry_fails_after_second_attempt(self, git_repo, monkeypatch):
        """Worker produces bad code → retry → still bad → task fails."""
        call_count = {"initial": 0, "retry": 0}

        async def _failing_retry_worker(
            project_root,
            plan_name,
            task_slug,
            pane_slug,
            worktree_path,
            task,
            task_scope,
            on_exit,
            on_event=None,
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
            project_root,
            plan_name,
            task_slug,
            pane_slug,
            worktree_path,
            task,
            task_scope,
            on_exit,
            on_event=None,
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

        # Worker writes bad code initially, fixed code on retry
        initial_code = "print(undefined)\n"  # F821: undefined name
        retry_code = "undefined = 1\nprint(undefined)\n"
        worker = _retry_worker(initial_code, retry_code, filename="out.py")

        monkeypatch.setattr("dgov.runner.run_headless_worker", worker)
        monkeypatch.setattr("dgov.runner.emit_event", _semantic_event_capture(events))

        dag = _dag({"event-test": _task("event-test")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        asyncio.run(runner.run())

        _assert_settlement_retry_event(events, task_slug="event-test")


# ---------------------------------------------------------------------------
# Chain: b depends on a
# ---------------------------------------------------------------------------


class TestChain:
    def test_sequential_merge(self, git_repo, monkeypatch):
        """a merges first, then b. Both files end up on main."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({
            "step-a": _task("step-a"),
            "step-b": _task("step-b", depends_on=("step-a",)),
        })
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["step-a"] == "merged"
        assert results["step-b"] == "merged"
        assert (git_repo / "step-a.txt").exists()
        assert (git_repo / "step-b.txt").exists()

    def test_downstream_worktree_sees_upstream_output(self, git_repo, monkeypatch):
        """A dependent task starts from a snapshot that includes upstream output."""

        async def _worker_with_dependency_visibility(
            project_root,
            plan_name,
            task_slug,
            pane_slug,
            worktree_path,
            task,
            task_scope,
            on_exit,
            on_event=None,
        ):
            if task_slug == "step-a":
                (worktree_path / "upstream.txt").write_text("from upstream\n")
            else:
                assert (worktree_path / "upstream.txt").exists()
                (worktree_path / "step-b.txt").write_text(
                    (worktree_path / "upstream.txt").read_text()
                )
            on_exit(task_slug, pane_slug, 0, "")

        monkeypatch.setattr("dgov.runner.run_headless_worker", _worker_with_dependency_visibility)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({
            "step-a": _task("step-a"),
            "step-b": _task("step-b", depends_on=("step-a",)),
        })
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["step-a"] == "merged"
        assert results["step-b"] == "merged"
        assert (git_repo / "step-b.txt").read_text() == "from upstream\n"

    def test_merge_order_respects_deps(self, git_repo, monkeypatch):
        """a's commit appears before b's in git log."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({
            "first": _task("first"),
            "second": _task("second", depends_on=("first",)),
        })
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
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({
            "alpha": _task("alpha"),
            "beta": _task("beta"),
        })
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["alpha"] == "merged"
        assert results["beta"] == "merged"
        assert (git_repo / "alpha.txt").exists()
        assert (git_repo / "beta.txt").exists()

    def test_one_fails_other_still_merges(self, git_repo, monkeypatch):
        """alpha fails, beta still merges (scan-based merge fix)."""

        async def _selective_worker(
            project_root,
            plan_name,
            task_slug,
            pane_slug,
            worktree_path,
            task,
            task_scope,
            on_exit,
            on_event=None,
        ):
            if task_slug == "alpha":
                on_exit(task_slug, pane_slug, 1, "")
            else:
                (worktree_path / f"{task_slug}.txt").write_text(f"output from {task_slug}\n")
                on_exit(task_slug, pane_slug, 0, "")

        monkeypatch.setattr("dgov.runner.run_headless_worker", _selective_worker)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({
            "alpha": _task("alpha"),
            "beta": _task("beta"),
        })
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["alpha"] == "failed"
        assert results["beta"] == "merged"
        assert not (git_repo / "alpha.txt").exists()
        assert (git_repo / "beta.txt").exists()


# ---------------------------------------------------------------------------
# DB state sync: task states written to SQLite after run
# ---------------------------------------------------------------------------


class TestDbStateSync:
    """Validate that task states are persisted to DB during and after a run."""

    def test_failed_task_state_in_db(self, git_repo, monkeypatch):
        """Worker exits 1 → DB record shows 'failed' immediately after run."""
        from dgov.persistence import get_runtime_artifact

        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_fail)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"db-fail": _task("db-fail")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["db-fail"] == "failed"
        record = get_runtime_artifact(str(git_repo), "db-fail")
        assert record is not None, "Task record missing from DB after run"
        assert record["state"] == "failed", f"Expected 'failed', got {record['state']!r}"

    def test_merged_task_state_in_db(self, git_repo, monkeypatch):
        """Worker exits 0 + settlement passes → DB record shows 'merged'."""
        from dgov.persistence import get_runtime_artifact

        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"db-ok": _task("db-ok")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["db-ok"] == "merged"
        record = get_runtime_artifact(str(git_repo), "db-ok")
        assert record is not None, "Task record missing from DB after run"
        assert record["state"] == "merged", f"Expected 'merged', got {record['state']!r}"

    def test_downstream_skipped_after_failure_in_db(self, git_repo, monkeypatch):
        """Upstream fails → downstream task is skipped. Snapshot reflects that."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_fail)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({
            "upstream": _task("upstream"),
            "downstream": _task("downstream", depends_on=("upstream",)),
        })
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        assert results["upstream"] == "failed"
        assert results["downstream"] == "skipped"


# ---------------------------------------------------------------------------
# Orphan abandon: re-run after crash shows abandoned, not complete
# ---------------------------------------------------------------------------


class TestOrphanAbandon:
    """Simulate a prior crashed run (orphaned ACTIVE tasks) and verify correct behavior."""

    def test_orphaned_task_becomes_abandoned_on_rerun(self, git_repo, monkeypatch):
        """After a crash, a bare re-run abandons orphaned tasks."""
        from dgov.persistence import clear_connection_cache

        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        slug = "orphan-task"
        dag = _dag({slug: _task(slug)})
        session_root = str(git_repo)

        # Simulate a crashed run: add task as ACTIVE + emit dispatched event
        _record_active_orphan(session_root, git_repo, dag, slug, "pane-crashed")

        # Re-run bare (no --continue, no --restart)
        clear_connection_cache()
        runner = EventDagRunner(dag, session_root=session_root)
        results = asyncio.run(runner.run())

        assert results[slug] == "abandoned", (
            f"Expected 'abandoned', got {results[slug]!r}. "
            "Bare re-run after crash must surface abandoned state, not silently complete."
        )

    def test_orphan_rerun_kernel_status_is_not_completed(self, git_repo, monkeypatch):
        """kernel.status must be FAILED (not COMPLETED) when all tasks are abandoned."""
        from dgov.kernel import DagState
        from dgov.persistence import clear_connection_cache

        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        slug = "status-orphan"
        dag = _dag({slug: _task(slug)})
        session_root = str(git_repo)

        _record_active_orphan(session_root, git_repo, dag, slug, "pane-crashed-2")

        clear_connection_cache()
        runner = EventDagRunner(dag, session_root=session_root)
        asyncio.run(runner.run())

        assert runner.kernel.status != DagState.COMPLETED, (
            "kernel.status must not be COMPLETED when all tasks were abandoned"
        )
        assert runner.kernel.status == DagState.FAILED


class TestSemanticSettlementIntegration:
    """Integration tests for shadow-mode semantic settlement events."""

    def test_integration_risk_scored_event_has_required_fields(self, git_repo, monkeypatch):
        """integration_risk_scored event contains all required fields for review."""
        from dgov.persistence import read_events

        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        dag = _dag({"risk-test": _task("risk-test")})
        session_root = str(git_repo)
        runner = EventDagRunner(dag, session_root=session_root)
        asyncio.run(runner.run())

        # Read events and verify integration_risk_scored was emitted
        events = read_events(session_root, plan_name=dag.name)
        risk_events = [e for e in events if e["event"] == "integration_risk_scored"]
        assert len(risk_events) == 1

        event = risk_events[0]
        assert event["task_slug"] == "risk-test"
        assert "target_head_sha" in event
        assert "task_base_sha" in event
        assert "risk_level" in event
        assert "claimed_files" in event
        assert "changed_files" in event
        assert "python_overlap_detected" in event
        assert "overlap_evidence" in event

    def test_merge_succeeds_even_with_risk_detected(self, git_repo, monkeypatch):
        """Shadow mode: merge succeeds regardless of risk level (telemetry only)."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        monkeypatch.setattr("dgov.settlement_flow.validate_sandbox", _mock_settlement_pass)

        # Create a task with files that will change
        task = _task("risky-task")
        dag = _dag({"risky-task": task})
        session_root = str(git_repo)

        # Add a file to the repo first (so there's something to change)
        (git_repo / "existing.py").write_text("# original\n")
        subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "add existing"],
            cwd=git_repo,
            check=True,
            capture_output=True,
        )

        runner = EventDagRunner(dag, session_root=session_root)
        results = asyncio.run(runner.run())

        # Should still merge successfully (shadow mode)
        assert results["risky-task"] == "merged"

    def test_reviewer_task_skips_risk_scoring(self, git_repo, monkeypatch):
        """Reviewer tasks (read-only) don't trigger settlement or risk scoring."""
        from dgov.persistence import read_events

        async def _mock_reviewer_worker(
            project_root,
            plan_name,
            task_slug,
            pane_slug,
            worktree_path,
            task,
            task_scope,
            on_exit,
            on_event=None,
        ):
            """Mock reviewer: no file changes, just exits 0."""
            on_exit(task_slug, pane_slug, 0, "")

        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_reviewer_worker)

        task = DagTaskSpec(
            slug="review",
            summary="Review task",
            prompt="Review code",
            commit_message="review: notes",
            agent="mock",
            role="reviewer",
            files=DagFileSpec(),
        )
        dag = _dag({"review": task})
        session_root = str(git_repo)

        runner = EventDagRunner(dag, session_root=session_root)
        results = asyncio.run(runner.run())

        # Reviewer should complete
        assert results["review"] == "merged"

        # No integration_risk_scored event for reviewer
        events = read_events(session_root, plan_name=dag.name)
        risk_events = [e for e in events if e["event"] == "integration_risk_scored"]
        assert len(risk_events) == 0


# -----------------------------------------------------------------------------
# Integration Candidate Helpers
# -----------------------------------------------------------------------------


async def _ic_conflict_worker(
    project_root,
    plan_name,
    task_slug,
    pane_slug,
    worktree_path,
    task,
    task_scope,
    on_exit,
    on_event=None,
):
    """Worker that writes a file that conflicts with main."""
    (worktree_path / "new_file.py").write_text("x = 1\n")
    on_exit(task_slug, pane_slug, 0, "")


def _ic_failing_candidate_result(project_root, task_wt, candidate_slug):
    """Return a failing integration candidate result simulating a conflict."""
    from dgov.worktree import IntegrationCandidateResult

    return IntegrationCandidateResult(
        passed=False,
        error="Simulated: file exists on main causing conflict",
    )


def _ic_conflict_task_dag():
    """Return a DAG with a task that creates a conflicting file."""
    return _dag({
        "conflict-task": DagTaskSpec(
            slug="conflict-task",
            summary="Task that creates file",
            prompt="Create file",
            commit_message="feat: add file",
            agent="mock",
            files=DagFileSpec(create=("new_file.py",)),
        )
    })


def _assert_ic_task_failed(results, task_slug):
    """Assert that the task failed due to integration candidate conflict."""
    assert results[task_slug] == "failed"


def _assert_ic_failed_events_for_task(events, task_slug):
    """Assert that integration_candidate_failed events were emitted for the task."""
    failed_events = [e for e in events if e["event"] == "integration_candidate_failed"]
    assert len(failed_events) >= 1  # At least one failure event
    assert all(e["task_slug"] == task_slug for e in failed_events)


def _assert_ic_worktree_preserved(wt_dir, task_slug):
    """Assert that the conflict task worktree was preserved for inspection."""
    preserved_wts = list(wt_dir.glob(f"{task_slug}*")) if wt_dir.exists() else []
    assert len(preserved_wts) >= 1


class TestIntegrationCandidate:
    """Integration tests for integration candidate validation with real git."""

    def test_clean_replay_lands_successfully(self, git_repo, monkeypatch):
        """When task replay is clean against HEAD, it lands successfully."""
        from dgov.persistence import read_events

        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)
        # Use real validation

        dag = _dag({"clean-task": _task("clean-task")})
        session_root = str(git_repo)

        runner = EventDagRunner(dag, session_root=session_root)
        results = asyncio.run(runner.run())

        assert results["clean-task"] == "merged"
        assert (git_repo / "clean-task.txt").exists()

        # Verify integration_candidate_passed was emitted
        events = read_events(session_root, plan_name=dag.name)
        passed_events = [e for e in events if e["event"] == "integration_candidate_passed"]
        assert len(passed_events) == 1
        assert passed_events[0]["task_slug"] == "clean-task"

    def test_moved_head_replay_succeeds(self, git_repo, monkeypatch):
        """Task can replay cleanly even when HEAD moved forward."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)

        dag = _dag({"task-a": _task("task-a")})
        session_root = str(git_repo)

        # First task runs and merges
        runner_a = EventDagRunner(dag, session_root=session_root)
        results_a = asyncio.run(runner_a.run())
        assert results_a["task-a"] == "merged"

        # Now run another task - its integration candidate should replay on top of task-a
        dag_b = _dag({"task-b": _task("task-b")})
        runner_b = EventDagRunner(dag_b, session_root=session_root)
        results_b = asyncio.run(runner_b.run())

        assert results_b["task-b"] == "merged"
        assert (git_repo / "task-a.txt").exists()
        assert (git_repo / "task-b.txt").exists()

    def test_candidate_rejection_preserves_worktree(self, git_repo, monkeypatch):
        """When integration candidate fails, original worktree is preserved."""

        from dgov.persistence import read_events
        from dgov.worktree import _worktrees_dir

        monkeypatch.setattr("dgov.runner.run_headless_worker", _ic_conflict_worker)
        monkeypatch.setattr(
            "dgov.settlement_flow.create_integration_candidate",
            _ic_failing_candidate_result,
        )

        dag = _ic_conflict_task_dag()
        session_root = str(git_repo)

        runner = EventDagRunner(dag, session_root=session_root)
        results = asyncio.run(runner.run())

        _assert_ic_task_failed(results, task_slug="conflict-task")

        events = read_events(session_root, plan_name=dag.name)
        _assert_ic_failed_events_for_task(events, task_slug="conflict-task")

        wt_dir = _worktrees_dir(session_root)
        _assert_ic_worktree_preserved(wt_dir, task_slug="conflict-task")

    def test_candidate_passed_before_merge_event(self, git_repo, monkeypatch):
        """integration_candidate_passed event precedes merge_completed."""
        from dgov.persistence import read_events

        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)

        dag = _dag({"ordered-task": _task("ordered-task")})
        session_root = str(git_repo)

        runner = EventDagRunner(dag, session_root=session_root)
        results = asyncio.run(runner.run())

        assert results["ordered-task"] == "merged"

        # Verify event order: candidate_passed before merge_completed
        events = read_events(session_root, plan_name=dag.name)
        event_order = [e["event"] for e in events]

        if "integration_candidate_passed" in event_order and "merge_completed" in event_order:
            candidate_idx = event_order.index("integration_candidate_passed")
            merge_idx = event_order.index("merge_completed")
            assert candidate_idx < merge_idx, "candidate_passed should precede merge_completed"


# ---------------------------------------------------------------------------
# Python Semantic Gate Integration Tests
# ---------------------------------------------------------------------------


class TestPythonSemanticGateIntegration:
    """Integration tests for deterministic Python semantic gate."""

    def test_semantic_gate_rejects_same_symbol_edit(self, git_repo, monkeypatch):
        """Python semantic gate rejects when both sides edit same symbol."""
        from dgov.semantic_settlement import FailureClass

        events = []
        worker = _semantic_gate_worker("""def process():
    return "task version"
""")

        monkeypatch.setattr("dgov.runner.run_headless_worker", worker)
        monkeypatch.setattr("dgov.runner.emit_event", _semantic_event_capture(events))
        monkeypatch.setattr(
            "dgov.settlement_flow.run_python_semantic_gate_in_subprocess",
            _same_symbol_edit_verdict,
        )

        dag = _dag({
            "same-edit-test": _task("same-edit-test", commit_message="feat: edit process")
        })
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        # Task should fail due to same symbol edit
        assert results["same-edit-test"] == "failed"

        # Verify semantic_gate_rejected was emitted
        rejected_events = _semantic_gate_rejections(events)
        assert len(rejected_events) >= 1
        assert rejected_events[0].get("failure_class") == FailureClass.SAME_SYMBOL_EDIT.value

    def test_semantic_gate_rejects_signature_drift(self, git_repo, monkeypatch):
        """Python semantic gate rejects when signature drift detected."""
        from dgov.semantic_settlement import FailureClass

        events = []
        worker = _semantic_gate_worker("""def helper(x: int) -> str:
    return str(x)
""")

        monkeypatch.setattr("dgov.runner.run_headless_worker", worker)
        monkeypatch.setattr("dgov.runner.emit_event", _semantic_event_capture(events))
        monkeypatch.setattr(
            "dgov.settlement_flow.run_python_semantic_gate_in_subprocess",
            _signature_drift_verdict,
        )

        dag = _dag({"drift-test": _task("drift-test", commit_message="feat: change signature")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        # Task should fail due to signature drift
        assert results["drift-test"] == "failed"

        # Verify semantic_gate_rejected was emitted
        rejected_events = _semantic_gate_rejections(events)
        assert len(rejected_events) >= 1
        assert rejected_events[0].get("failure_class") == FailureClass.SIGNATURE_DRIFT.value

    def test_semantic_gate_passes_clean_python(self, git_repo, monkeypatch):
        """Python semantic gate passes for clean, valid Python code."""
        events = []

        clean_code = """class Processor:
    def process(self, data: str) -> str:
        return data.upper()

def helper(x: int) -> int:
    return x * 2
"""
        monkeypatch.setattr(
            "dgov.runner.run_headless_worker",
            _semantic_gate_worker(clean_code, filename="clean.py"),
        )
        monkeypatch.setattr(
            "dgov.runner.emit_event",
            _semantic_event_capture(events),
        )

        dag = _dag({"clean-test": _task("clean-test", commit_message="feat: clean code")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        # Task should succeed
        assert results["clean-test"] == "merged"

        # Verify no semantic_gate_rejected was emitted
        assert len(_semantic_gate_rejections(events)) == 0

        # Verify file was merged
        assert (git_repo / "clean.py").exists()

    def test_semantic_gate_bypasses_non_python_files(self, git_repo, monkeypatch):
        """Non-Python tasks skip Python semantic gate and proceed normally."""
        monkeypatch.setattr("dgov.runner.run_headless_worker", _mock_worker_ok)

        dag = _dag({"docs-task": _task("docs-task", commit_message="docs: update readme")})
        runner = EventDagRunner(dag, session_root=str(git_repo))
        results = asyncio.run(runner.run())

        # Non-Python task should succeed
        assert results["docs-task"] == "merged"
        assert (git_repo / "docs-task.txt").exists()
