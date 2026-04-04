"""Tests for EventDagRunner — the async governor loop.

Mocks: worktree ops, headless worker, persistence.
Tests: the kernel<->runner contract under happy, failure, and concurrent scenarios.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.kernel import DagState
from dgov.runner import EventDagRunner
from dgov.types import Worktree

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dag(tasks: dict[str, DagTaskSpec]) -> DagDefinition:
    return DagDefinition(
        name="test-dag",
        dag_file="test.toml",
        project_root="/tmp/test-project",
        session_root="/tmp/test-project",
        tasks=tasks,
    )


def _task(slug: str, depends_on: tuple[str, ...] = (), agent: str = "test-agent") -> DagTaskSpec:
    return DagTaskSpec(
        slug=slug,
        summary=f"Test task {slug}",
        prompt=f"Do {slug}",
        commit_message=f"feat: {slug}",
        agent=agent,
        depends_on=depends_on,
        files=DagFileSpec(create=(f"{slug}.py",)),
    )


def _single_dag() -> DagDefinition:
    return _dag({"a": _task("a")})


def _chain_dag() -> DagDefinition:
    return _dag({"a": _task("a"), "b": _task("b", depends_on=("a",))})


def _parallel_dag() -> DagDefinition:
    return _dag({"a": _task("a"), "b": _task("b")})


def _mock_create_worktree(project_root: str, slug: str, base_ref: str = "HEAD") -> Worktree:
    return Worktree(path=Path(f"/tmp/wt-{slug}"), branch=f"dgov/{slug}", commit="abc123")


def _mock_review_pass(wt_path, claimed_files=None, max_diff_lines=100):
    from dgov.settlement import ReviewResult

    return ReviewResult(passed=True, verdict="ok", actual_files=frozenset({"test.py"}))


def _mock_review_fail(wt_path, claimed_files=None, max_diff_lines=100):
    from dgov.settlement import ReviewResult

    return ReviewResult(passed=False, verdict="scope_violation", error="touched unclaimed files")


def _mock_gate_pass(wt_path, base_commit, project_root):
    from dgov.settlement import GateResult

    return GateResult(passed=True)


def _mock_gate_fail(wt_path, base_commit, project_root):
    from dgov.settlement import GateResult

    return GateResult(passed=False, error="lint failure")


# Patch targets — runner imports at top level
_P_CREATE_WT = "dgov.runner.create_worktree"
_P_MERGE_WT = "dgov.runner.merge_worktree"
_P_REMOVE_WT = "dgov.runner.remove_worktree"
_P_COMMIT_WT = "dgov.runner.commit_in_worktree"
_P_AUTOFIX = "dgov.runner.autofix_sandbox"
_P_VALIDATE = "dgov.runner.validate_sandbox"
_P_ADD_TASK = "dgov.runner.add_task"
_P_EMIT_EVENT = "dgov.runner.emit_event"
_P_HEADLESS = "dgov.runner.run_headless_worker"
# _run_review does local imports — patch at source
_P_GET_TASK = "dgov.persistence.tasks.get_task"
_P_REVIEW = "dgov.settlement.review_sandbox"


async def _fake_worker_success(project_root, task_slug, pane_slug, wt_path, task, on_exit):
    await asyncio.sleep(0.01)
    on_exit(task_slug, pane_slug, 0)


async def _fake_worker_fail(project_root, task_slug, pane_slug, wt_path, task, on_exit):
    await asyncio.sleep(0.01)
    on_exit(task_slug, pane_slug, 1)


async def _fake_worker_slow(project_root, task_slug, pane_slug, wt_path, task, on_exit):
    await asyncio.sleep(0.5)
    on_exit(task_slug, pane_slug, 0)


def _make_runner(dag: DagDefinition) -> EventDagRunner:
    runner = EventDagRunner(dag, session_root="/tmp/test-project")
    runner._setup_signal_handlers = lambda: None
    return runner


def _io_patches(
    headless=_fake_worker_success,
    review=_mock_review_pass,
    validate=_mock_gate_pass,
    create_wt=_mock_create_worktree,
    merge_wt=None,
):
    """Return a context manager that patches all I/O boundaries."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        with (
            patch(_P_CREATE_WT, side_effect=create_wt),
            patch(_P_MERGE_WT, side_effect=merge_wt),
            patch(_P_REMOVE_WT),
            patch(_P_COMMIT_WT, return_value="deadbeef"),
            patch(_P_AUTOFIX),
            patch(_P_VALIDATE, side_effect=validate),
            patch(_P_ADD_TASK),
            patch(_P_EMIT_EVENT),
            patch(_P_HEADLESS, side_effect=headless),
            patch(_P_GET_TASK, return_value={"file_claims": ["test.py"]}),
            patch(_P_REVIEW, side_effect=review),
        ):
            yield

    return _ctx()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSingleTaskHappy:
    def test_single_task_merges(self):
        with _io_patches():
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"

    def test_kernel_reaches_completed(self):
        with _io_patches():
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())
            assert runner.kernel.status == DagState.COMPLETED

    def test_worktree_cleaned_up(self):
        with _io_patches():
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())
            assert len(runner._worktrees) == 0


class TestChain:
    def test_chain_completes_in_order(self):
        with _io_patches():
            runner = _make_runner(_chain_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"
            assert results["b"] == "merged"


class TestParallel:
    def test_parallel_both_merge(self):
        with _io_patches():
            runner = _make_runner(_parallel_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"
            assert results["b"] == "merged"


class TestWorkerFailure:
    def test_worker_fail_exhausts_retries(self):
        with _io_patches(headless=_fake_worker_fail):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"
            assert runner.kernel.attempts.get("a", 0) >= 1


class TestReviewFailure:
    def test_review_fail_marks_failed(self):
        with _io_patches(review=_mock_review_fail):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"


class TestMergeFailure:
    def test_merge_error_marks_failed(self):
        with _io_patches(merge_wt=MagicMock(side_effect=Exception("conflict"))):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"


class TestValidationFailure:
    def test_validation_gate_rejects(self):
        with _io_patches(validate=_mock_gate_fail):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"


class TestDispatchFailure:
    def test_worktree_creation_fails_isolates(self):
        """One task's worktree creation fails, other task still succeeds."""

        def _flaky_create(project_root, slug, base_ref="HEAD"):
            if slug == "a":
                raise OSError("disk full")
            return _mock_create_worktree(project_root, slug, base_ref)

        with _io_patches(create_wt=_flaky_create):
            runner = _make_runner(_parallel_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"
            assert results["b"] == "merged"


class TestPartialDAG:
    def test_partial_success(self):
        async def _alternating_worker(project_root, task_slug, pane_slug, wt_path, task, on_exit):
            await asyncio.sleep(0.01)
            on_exit(task_slug, pane_slug, 0 if task_slug == "a" else 1)

        with _io_patches(headless=_alternating_worker):
            runner = _make_runner(_parallel_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"
            assert results["b"] == "failed"
            assert runner.kernel.status == DagState.PARTIAL


class TestCleanup:
    def test_cleanup_cancels_workers_and_removes_worktrees(self):
        with _io_patches(headless=_fake_worker_slow):
            runner = _make_runner(_single_dag())

            async def _run_with_shutdown():
                async def _trigger():
                    await asyncio.sleep(0.05)
                    runner._shutdown_event.set()

                runner._setup_signal_handlers = lambda: None
                asyncio.create_task(_trigger())
                return await runner.run()

            asyncio.run(_run_with_shutdown())
            assert len(runner._worker_tasks) == 0
            assert len(runner._worktrees) == 0
