"""Tests for EventDagRunner — the async governor loop.

Mocks: worktree ops, headless worker, persistence.
Tests: the kernel<->runner contract under happy, failure, and concurrent scenarios.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

from dgov.config import ProjectConfig
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


def _mock_review_pass(
    wt_path,
    claimed_files=None,
    max_diff_lines=100,
    project_root=None,
    task_slug=None,
    pane_slug=None,
    scope_ignore_files=(),
):
    from dgov.settlement import ReviewResult

    return ReviewResult(passed=True, verdict="ok", actual_files=frozenset({"test.py"}))


def _mock_review_fail(
    wt_path,
    claimed_files=None,
    max_diff_lines=100,
    project_root=None,
    task_slug=None,
    pane_slug=None,
    scope_ignore_files=(),
):
    from dgov.settlement import ReviewResult

    return ReviewResult(passed=False, verdict="scope_violation", error="touched unclaimed files")


def _mock_gate_pass(wt_path, base_commit, project_root, config=None):
    from dgov.settlement import GateResult

    return GateResult(passed=True)


def _mock_gate_fail(wt_path, base_commit, project_root, config=None):
    from dgov.settlement import GateResult

    return GateResult(passed=False, error="lint failure")


# Patch targets — runner imports at top level
_P_CREATE_WT = "dgov.runner.create_worktree"
_P_MERGE_WT = "dgov.runner.merge_worktree"
_P_REMOVE_WT = "dgov.runner.remove_worktree"
_P_COMMIT_WT = "dgov.runner.commit_in_worktree"
_P_AUTOFIX = "dgov.runner.autofix_sandbox"
_P_VALIDATE = "dgov.runner.validate_sandbox"
_P_PREPARE_WT = "dgov.runner.prepare_worktree"
_P_RECORD_ARTIFACT = "dgov.runner.record_runtime_artifact"
_P_EMIT_EVENT = "dgov.runner.emit_event"
_P_HEADLESS = "dgov.runner.run_headless_worker"
# review_sandbox imported at top level in runner
_P_REVIEW = "dgov.runner.review_sandbox"
_P_DEPLOY_APPEND = "dgov.deploy_log.append"


async def _fake_worker_success(
    project_root,
    plan_name,
    task_slug,
    pane_slug,
    wt_path,
    task,
    task_scope,
    on_exit,
    on_event=None,
):
    await asyncio.sleep(0.01)
    on_exit(task_slug, pane_slug, 0, "")


async def _fake_worker_fail(
    project_root,
    plan_name,
    task_slug,
    pane_slug,
    wt_path,
    task,
    task_scope,
    on_exit,
    on_event=None,
):
    await asyncio.sleep(0.01)
    on_exit(task_slug, pane_slug, 1, "test failure")


async def _fake_worker_slow(
    project_root,
    plan_name,
    task_slug,
    pane_slug,
    wt_path,
    task,
    task_scope,
    on_exit,
    on_event=None,
):
    await asyncio.sleep(0.5)
    on_exit(task_slug, pane_slug, 0, "")


def _make_runner(dag: DagDefinition) -> EventDagRunner:
    runner = EventDagRunner(dag, session_root="/tmp/test-project")
    runner._setup_signal_handlers = lambda: None  # type: ignore

    async def _noop() -> None:
        pass

    runner._check_model_env = _noop  # type: ignore
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
            patch(_P_MERGE_WT, side_effect=merge_wt, return_value="abc123merge"),
            patch(_P_REMOVE_WT),
            patch(_P_COMMIT_WT, return_value="deadbeef"),
            patch(_P_AUTOFIX),
            patch(_P_VALIDATE, side_effect=validate),
            patch(_P_PREPARE_WT),
            patch(_P_RECORD_ARTIFACT),
            patch(_P_EMIT_EVENT),
            patch(_P_HEADLESS, side_effect=headless),
            patch(_P_REVIEW, side_effect=review),
        ):
            yield

    return _ctx()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTouchFileClaims:
    def test_touch_included_in_task_files(self):
        """Touch field is flattened into runner.task_files for scope enforcement."""
        task = DagTaskSpec(
            slug="t",
            summary="s",
            prompt="p",
            commit_message="c",
            agent="test-agent",
            files=DagFileSpec(touch=("src/a.py", "tests/test_a.py")),
        )
        dag = _dag({"t": task})
        with _io_patches():
            runner = _make_runner(dag)
            assert runner.task_files["t"] == ("src/a.py", "tests/test_a.py")

    def test_touch_merged_with_create_in_task_files(self):
        """Touch + create are deduplicated in task_files."""
        task = DagTaskSpec(
            slug="t",
            summary="s",
            prompt="p",
            commit_message="c",
            agent="test-agent",
            files=DagFileSpec(create=("new.py",), touch=("src/a.py", "new.py")),
        )
        dag = _dag({"t": task})
        with _io_patches():
            runner = _make_runner(dag)
            assert set(runner.task_files["t"]) == {"new.py", "src/a.py"}


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

    def test_task_test_cmd_override_reaches_settlement(self):
        captured: dict[str, object] = {}

        def _capture_validate(wt_path, base_commit, project_root, config=None):
            from dgov.settlement import GateResult

            captured["test_cmd"] = getattr(config, "test_cmd", "")
            return GateResult(passed=True)

        task = _task("a").model_copy(
            update={"test_cmd": "./scripts/qgis-python.sh -m pytest tests/plugin/test_a.py"}
        )
        with _io_patches(validate=_capture_validate):
            runner = _make_runner(_dag({"a": task}))
            results = asyncio.run(runner.run())

        assert results["a"] == "merged"
        assert captured["test_cmd"] == "./scripts/qgis-python.sh -m pytest tests/plugin/test_a.py"


class TestChain:
    def test_chain_completes_in_order(self):
        with _io_patches():
            runner = _make_runner(_chain_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"
            assert results["b"] == "merged"

    def test_dependent_dispatch_uses_latest_upstream_sha(self):
        captured: list[str] = []

        def _capture_create(project_root: str, slug: str, base_ref: str = "HEAD") -> Worktree:
            captured.append(f"{slug}:{base_ref}")
            return _mock_create_worktree(project_root, slug, base_ref)

        with (
            _io_patches(create_wt=_capture_create),
            patch(
                "dgov.deploy_log.read",
                return_value=[
                    MagicMock(unit="a", sha="dep-sha", ts="2026-04-18T12:00:00Z"),
                ],
            ),
        ):
            runner = _make_runner(_chain_dag())
            asyncio.run(runner._dispatch(MagicMock(task_slug="b")))

        assert captured == ["b:dep-sha"]


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

    def test_runner_honors_dag_retry_budget(self):
        dag = DagDefinition(
            name="retry-budget",
            dag_file="test.toml",
            project_root="/tmp/test-project",
            session_root="/tmp/test-project",
            tasks={"a": _task("a")},
            default_max_retries=0,
        )
        with _io_patches(headless=_fake_worker_fail):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"
            assert runner.kernel.max_retries == 0


class TestPreflight:
    def test_preflight_uses_configured_api_key_env(self, tmp_path: Path, monkeypatch) -> None:
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\nllm_api_key_env = "OPENAI_API_KEY"\n')
        dag = DagDefinition(
            name="preflight",
            dag_file="test.toml",
            project_root=str(tmp_path),
            session_root=str(tmp_path),
            tasks={"a": _task("a")},
        )
        runner = EventDagRunner(dag, session_root=str(tmp_path))
        monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        asyncio.run(runner._check_model_env())


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

    def test_worktree_prepare_failure_sets_task_error(self):
        with (
            _io_patches(),
            patch(_P_PREPARE_WT, side_effect=RuntimeError("prepare failed")),
        ):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"
            assert runner._task_errors["a"] == "prepare failed"


class TestPartialDAG:
    def test_partial_success(self):
        async def _alternating_worker(
            project_root,
            plan_name,
            task_slug,
            pane_slug,
            wt_path,
            task,
            task_scope,
            on_exit,
            on_event=None,
        ):
            await asyncio.sleep(0.01)
            on_exit(task_slug, pane_slug, 0 if task_slug == "a" else 1, "")

        with _io_patches(headless=_alternating_worker):
            runner = _make_runner(_parallel_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"
            assert results["b"] == "failed"
            assert runner.kernel.status == DagState.PARTIAL


class TestTimeout:
    def test_worker_timeout_marks_failed(self):
        """Task with short timeout_s should fail when worker exceeds it."""

        async def _forever_worker(
            project_root,
            plan_name,
            task_slug,
            pane_slug,
            wt_path,
            task,
            task_scope,
            on_exit,
            on_event=None,
        ):
            await asyncio.sleep(999)  # will be cancelled by timeout
            on_exit(task_slug, pane_slug, 0, "")

        # Build a dag with a 1-second timeout
        short_task = DagTaskSpec(
            slug="a",
            summary="Test timeout",
            prompt="Do a",
            commit_message="feat: a",
            agent="test-agent",
            files=DagFileSpec(create=("a.py",)),
            timeout_s=1,
        )
        dag = _dag({"a": short_task})

        with _io_patches(headless=_forever_worker):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"


class TestDeployLog:
    def test_deploy_log_called_on_merge(self):
        """Successful merge should record to deploy log."""
        with _io_patches() as _, patch(_P_DEPLOY_APPEND) as mock_append:
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())
            mock_append.assert_called_once_with(
                "/tmp/test-project", "test-dag", "a", "abc123merge"
            )

    def test_deploy_log_not_called_on_failure(self):
        """Failed merge should not record to deploy log."""
        with (
            _io_patches(merge_wt=MagicMock(side_effect=Exception("conflict"))) as _,
            patch(_P_DEPLOY_APPEND) as mock_append,
        ):
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())
            mock_append.assert_not_called()

    def test_deploy_log_not_called_on_gate_reject(self):
        """Validation gate rejection should not record to deploy log."""
        with _io_patches(validate=_mock_gate_fail) as _, patch(_P_DEPLOY_APPEND) as mock_append:
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())
            mock_append.assert_not_called()


class TestRunStartMarker:
    """dgov plan review needs a lower bound on events per run.

    runner.run() emits a run_start event with plan_name set so review can
    scope to the latest invocation, regardless of whether --restart reset
    prior events.
    """

    def test_run_start_emitted_once_per_run(self):
        emitted: list[tuple[str, str, dict]] = []

        def _capture(session_root, event, pane, **kwargs):
            emitted.append((event, pane, kwargs))

        with _io_patches(), patch(_P_EMIT_EVENT, side_effect=_capture):
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())

        run_starts = [(pane, kw) for ev, pane, kw in emitted if ev == "run_start"]
        assert len(run_starts) == 1
        pane, kw = run_starts[0]
        assert pane == "run-test-dag"
        assert kw.get("plan_name") == "test-dag"

    def test_run_start_precedes_task_events(self):
        seen: list[str] = []

        def _capture(session_root, event, pane, **kwargs):
            seen.append(event)

        with _io_patches(), patch(_P_EMIT_EVENT, side_effect=_capture):
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())

        assert "run_start" in seen
        run_start_idx = seen.index("run_start")
        # Every task-lifecycle event comes after the run_start marker.
        for i, ev in enumerate(seen):
            if ev in {"dag_task_dispatched", "merge_completed", "dag_completed"}:
                assert i > run_start_idx, f"{ev} emitted before run_start"


class TestResearcherRole:
    """Regression for ledger bug #27: researcher tasks must not run settlement.

    Researcher tasks are read-only by construction and produce no commit.
    Without role-aware settlement, commit_in_worktree hits 'nothing to commit'
    and the task fails despite the researcher calling `done` successfully.
    """

    @staticmethod
    def _researcher_dag() -> DagDefinition:
        task = DagTaskSpec(
            slug="a",
            summary="investigate",
            prompt="Investigate foo and return a summary.",
            commit_message="unused",
            agent="test-agent",
            role="researcher",
            files=DagFileSpec(read=("src/foo.py",)),
        )
        return _dag({"a": task})

    def test_researcher_merges_without_commit_or_validate(self):
        with (
            _io_patches() as _,
            patch(_P_COMMIT_WT) as mock_commit,
            patch(_P_AUTOFIX) as mock_autofix,
            patch(_P_VALIDATE) as mock_validate,
            patch(_P_MERGE_WT) as mock_merge,
            patch(_P_PREPARE_WT) as mock_prepare,
        ):
            runner = _make_runner(self._researcher_dag())
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"
            mock_prepare.assert_not_called()
            mock_commit.assert_not_called()
            mock_autofix.assert_not_called()
            mock_validate.assert_not_called()
            mock_merge.assert_not_called()

    def test_researcher_records_head_sha_to_deploy_log(self):
        with _io_patches() as _, patch(_P_DEPLOY_APPEND) as mock_append:
            runner = _make_runner(self._researcher_dag())
            asyncio.run(runner.run())
            # Researcher deploy records use the HEAD sha captured at worktree
            # creation (Worktree.commit == "abc123" per the mock helper).
            mock_append.assert_called_once_with("/tmp/test-project", "test-dag", "a", "abc123")


class TestBootstrapPreparation:
    def test_worker_dispatch_uses_bootstrap_timeout(self):
        with _io_patches(), patch(_P_PREPARE_WT) as mock_prepare:
            runner = _make_runner(_single_dag())
            runner.project_config = ProjectConfig(bootstrap_timeout=17)
            asyncio.run(runner._dispatch(MagicMock(task_slug="a")))

        mock_prepare.assert_called_once()
        assert mock_prepare.call_args.kwargs["timeout_s"] == 17


class TestCleanup:
    def test_cleanup_cancels_workers_and_removes_worktrees(self):
        with _io_patches(headless=_fake_worker_slow):
            runner = _make_runner(_single_dag())

            async def _run_with_shutdown():
                async def _trigger():
                    await asyncio.sleep(0.05)
                    runner._shutdown_event.set()

                runner._setup_signal_handlers = lambda: None  # type: ignore
                trigger_task = asyncio.create_task(_trigger())
                try:
                    return await runner.run()
                finally:
                    trigger_task.cancel()

            asyncio.run(_run_with_shutdown())
            assert len(runner._worker_tasks) == 0
            assert len(runner._worktrees) == 0


class TestSemanticSettlementShadowMode:
    """Tests for shadow-mode semantic settlement — telemetry only, no merge blocking."""

    def test_integration_risk_scored_emitted_on_successful_merge(self):
        """Every task that reaches landing path emits integration_risk_scored."""
        with _io_patches() as _, patch(_P_EMIT_EVENT) as mock_emit:
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            # Task should still succeed (shadow mode)
            assert results["a"] == "merged"

            # Verify integration_risk_scored was emitted
            risk_calls = [
                c for c in mock_emit.call_args_list if c.args[1] == "integration_risk_scored"
            ]
            assert len(risk_calls) == 1
            # Verify payload contains expected fields
            call = risk_calls[0]
            assert call.kwargs.get("task_slug") == "a"
            assert "risk_level" in call.kwargs
            assert "claimed_files" in call.kwargs
            assert "changed_files" in call.kwargs

    def test_integration_risk_scored_emitted_even_when_merge_fails(self):
        """Risk scored even if settlement gate rejects (before failure path)."""
        with _io_patches(validate=_mock_gate_fail) as _, patch(_P_EMIT_EVENT) as mock_emit:
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            # Task should fail (settlement gate rejected)
            assert results["a"] == "failed"

            # Risk should still be scored (happens before merge)
            risk_calls = [
                c for c in mock_emit.call_args_list if c.args[1] == "integration_risk_scored"
            ]
            assert len(risk_calls) == 1

    def test_success_behavior_unchanged_in_shadow_mode(self):
        """Shadow mode: risky tasks still land if gates would accept them."""
        # Simulate a scenario with changed files but passing gates
        with _io_patches() as _:
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            # Verify unchanged success behavior
            assert results["a"] == "merged"
            assert runner.kernel.status.name == "COMPLETED"

    def test_researcher_task_skips_semantic_risk_scoring(self):
        """Read-only researcher tasks don't trigger settlement path."""
        task = DagTaskSpec(
            slug="research",
            summary="Research task",
            prompt="Research something",
            commit_message="research: notes",
            agent="test-agent",
            role="researcher",
            files=DagFileSpec(),
        )
        dag = _dag({"research": task})

        with _io_patches() as _, patch(_P_EMIT_EVENT) as mock_emit:
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())

            assert results["research"] == "merged"

            # No integration_risk_scored for researcher (no settlement path)
            risk_calls = [
                c for c in mock_emit.call_args_list if c.args[1] == "integration_risk_scored"
            ]
            assert len(risk_calls) == 0
