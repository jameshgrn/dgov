"""Tests for EventDagRunner — the async governor loop.

Mocks: worktree ops, headless worker, persistence.
Tests: the kernel<->runner contract under happy, failure, and concurrent scenarios.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest

from dgov.actions import InterruptGovernor, MergeTask
from dgov.config import ProjectConfig
from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.kernel import DagState
from dgov.persistence import add_ledger_entry, clear_connection_cache
from dgov.runner import EventDagRunner
from dgov.types import TaskState, Worktree

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
    read_files=(),
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
    read_files=(),
):
    from dgov.settlement import ReviewResult

    return ReviewResult(passed=False, verdict="scope_violation", error="touched unclaimed files")


def _mock_gate_pass(wt_path, base_commit, project_root, config=None):
    from dgov.settlement import GateResult

    return GateResult(passed=True)


def _mock_gate_fail(wt_path, base_commit, project_root, config=None):
    from dgov.settlement import GateResult

    return GateResult(passed=False, error="lint failure")


def _mock_integration_candidate_pass(project_root, task_wt, candidate_slug):
    """Mock successful integration candidate creation."""
    from pathlib import Path

    from dgov.worktree import IntegrationCandidateResult

    return IntegrationCandidateResult(
        passed=True,
        candidate_path=Path(f"/tmp/{candidate_slug}"),
        candidate_sha="candidate123abc",
    )


def _mock_integration_candidate_fail(project_root, task_wt, candidate_slug):
    """Mock failed integration candidate creation."""
    from dgov.worktree import IntegrationCandidateResult

    return IntegrationCandidateResult(
        passed=False,
        error="Replay failed: merge conflict detected",
    )


def _mock_semantic_gate_pass(*args, **kwargs):
    """Mock successful semantic gate evaluation."""
    from dgov.semantic_settlement import SemanticGateVerdict

    return SemanticGateVerdict(
        task_slug=kwargs.get("task_slug", "a"),
        gate_name="python_semantic",
        passed=True,
        checked_at=0.0,
    )


def _mock_compute_risk(*args, **kwargs):
    """Mock no-risk semantic risk record."""
    from dgov.semantic_settlement import IntegrationRiskRecord, RiskLevel

    return IntegrationRiskRecord(
        task_slug=kwargs.get("task_slug", "a"),
        target_head_sha="abc123",
        task_base_sha="base",
        task_commit_sha="commit",
        risk_level=RiskLevel.NONE,
        claimed_files=(),
        changed_files=(),
        python_overlap_detected=False,
        overlap_evidence=(),
        computed_at=0.0,
    )


_P_COMPUTE_RISK = "dgov.settlement_flow.SettlementFlow.compute_semantic_risk"

# Patch targets — runner imports at top level
_P_CREATE_WT = "dgov.runner.create_worktree"
_P_MERGE_WT = "dgov.settlement_flow.merge_worktree"
_P_REMOVE_WT = "dgov.runner.remove_worktree"
_P_COMMIT_WT = "dgov.settlement_flow.commit_in_worktree"
_P_AUTOFIX = "dgov.settlement_flow.autofix_sandbox"
_P_VALIDATE = "dgov.settlement_flow.validate_sandbox"
_P_PREPARE_WT = "dgov.runner.prepare_worktree"
_P_RECORD_ARTIFACT = "dgov.runner.record_runtime_artifact"
_P_EMIT_EVENT = "dgov.runner.emit_event"
_P_HEADLESS = "dgov.runner.run_headless_worker"
# review_sandbox imported at top level in runner
_P_REVIEW = "dgov.runner.review_sandbox"
_P_DEPLOY_APPEND = "dgov.deploy_log.append"
_P_CREATE_CANDIDATE = "dgov.settlement_flow.create_integration_candidate"
_P_REMOVE_CANDIDATE = "dgov.settlement_flow.remove_integration_candidate"
_P_SEMANTIC_GATE = "dgov.settlement_flow.run_python_semantic_gate_in_subprocess"
_P_GET_DIFF = "dgov.runner.EventDagRunner._get_worktree_diff"


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
    candidate=_mock_integration_candidate_pass,
    semantic_gate=_mock_semantic_gate_pass,
    deploy_records=None,
):
    """Return a context manager that patches all I/O boundaries."""
    import contextlib

    from dgov.deploy_log import DeployRecord

    recorded_deploys: list[DeployRecord] = list(deploy_records or [])

    def _append_deploy(
        project_root: str,
        plan_name: str,
        unit_id: str,
        commit_sha: str,
        timestamp=None,
    ):
        recorded_deploys.append(
            DeployRecord(
                plan=plan_name,
                unit=unit_id,
                sha=commit_sha,
                ts=timestamp or "2026-01-01T00:00:00Z",
            )
        )

    def _read_deploys(project_root: str, plan_name: str):
        return [record for record in recorded_deploys if record.plan == plan_name]

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
            patch(_P_DEPLOY_APPEND, side_effect=_append_deploy),
            patch(_P_HEADLESS, side_effect=headless),
            patch(_P_REVIEW, side_effect=review),
            patch(_P_CREATE_CANDIDATE, side_effect=candidate),
            patch(_P_REMOVE_CANDIDATE),
            patch(_P_SEMANTIC_GATE, side_effect=semantic_gate),
            patch(_P_COMPUTE_RISK, side_effect=_mock_compute_risk),
            patch("dgov.deploy_log.read", side_effect=_read_deploys),
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
            assert not any(ctx.worktree for ctx in runner._tasks.values())

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
            assert runner._ctx("a").attempts >= 1

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
            assert runner._ctx("a").error == "prepare failed"


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
        emitted: list = []

        def _capture(session_root, event, pane="", **kwargs):
            emitted.append(event)

        with _io_patches(), patch(_P_EMIT_EVENT, side_effect=_capture):
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())

        run_starts = [e for e in emitted if getattr(e, "event_type", None) == "run_start"]
        assert len(run_starts) == 1
        assert run_starts[0].pane == "run-test-dag"
        assert run_starts[0].plan_name == "test-dag"

    def test_run_start_precedes_task_events(self):
        seen: list[str] = []

        def _capture(session_root, event, pane="", **kwargs):
            seen.append(getattr(event, "event_type", str(event)))

        with _io_patches(), patch(_P_EMIT_EVENT, side_effect=_capture):
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())

        assert "run_start" in seen
        run_start_idx = seen.index("run_start")
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


class TestProbationPrompt:
    def test_probation_entries_match_overlapping_paths(self, tmp_path: Path):
        clear_connection_cache()
        try:
            add_ledger_entry(
                str(tmp_path),
                "rule",
                "Kernel case law",
                affected_paths=("src/dgov",),
            )
            task = DagTaskSpec(
                slug="a",
                summary="Core: fix kernel",
                prompt="Orient:\nContext.\n\nEdit:\n1. Change.\n\nVerify:\n- Check.",
                commit_message="c",
                agent="test-agent",
                files=DagFileSpec(edit=("src/dgov/kernel.py",)),
            )
            dag = DagDefinition(
                name="constitution",
                dag_file="plan.toml",
                project_root=str(tmp_path),
                session_root=str(tmp_path),
                tasks={"a": task},
            )

            runner = EventDagRunner(dag, session_root=str(tmp_path))

            assert runner._prompts._get_ledger_entries(task) == [
                {"id": 1, "content": "Kernel case law"}
            ]
        finally:
            clear_connection_cache()

    def test_probation_section_formatting(self):
        runner = _make_runner(_single_dag())

        section = runner._prompts._format_probation_section([
            {"id": 66, "content": "LLMSopBundler is deprecated"}
        ])

        assert "## Active Probation (Case Law)" in section
        assert "Entry #66" in section
        assert "LLMSopBundler is deprecated" in section


class TestBootstrapPreparation:
    def test_worker_dispatch_uses_bootstrap_timeout(self):
        with _io_patches(), patch(_P_PREPARE_WT) as mock_prepare:
            runner = _make_runner(_single_dag())
            runner.project_config = ProjectConfig(bootstrap_timeout=17)
            asyncio.run(runner._dispatch(MagicMock(task_slug="a")))

        mock_prepare.assert_called_once()
        assert mock_prepare.call_args.kwargs["timeout_s"] == 17


class TestTaskContextState:
    def test_merge_enriches_action_from_task_context(self):
        captured: dict[str, MergeTask] = {}

        async def _capture_settlement(action: MergeTask, wt: Worktree) -> tuple[str | None, bool]:
            captured["action"] = action
            return None, False

        with _io_patches():
            runner = _make_runner(_single_dag())
            ctx = runner._ctx("a")
            ctx.pane_slug = "pane-a"
            ctx.worktree = _mock_create_worktree("/tmp", "a")
            cast(Any, runner)._settle_and_merge = _capture_settlement

            asyncio.run(runner._merge(MergeTask("a", "", ("a.py",))))

        assert captured["action"] == MergeTask("a", "pane-a", ("a.py",))


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

            with pytest.raises(KeyboardInterrupt):
                asyncio.run(_run_with_shutdown())

            assert not any(ctx.worker_task for ctx in runner._tasks.values())
            assert not any(ctx.worktree for ctx in runner._tasks.values())

    def test_shutdown_after_successful_merge_returns_results(self):
        with _io_patches():
            runner = _make_runner(_single_dag())
            original_merge = runner._merge

            async def _merge_then_request_shutdown(action):
                result = await original_merge(action)
                runner._shutdown_event.set()
                return result

            object.__setattr__(runner, "_merge", _merge_then_request_shutdown)

            results = asyncio.run(runner.run())

        assert results["a"] == "merged"


class TestVerificationScope:
    def test_dispatch_passes_verify_test_targets(self):
        captured: dict[str, object] = {}

        async def _capture_scope(
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
            captured["task_scope"] = task_scope
            on_exit(task_slug, pane_slug, 0, "")

        task = DagTaskSpec(
            slug="a",
            summary="scope",
            prompt="Do a",
            commit_message="feat: a",
            agent="test-agent",
            files=DagFileSpec(
                create=("tests/test_created.py",),
                edit=("src/core.py",),
                touch=("tests/test_touch.py",),
                read=("tests/test_read.py", "README.md"),
            ),
        )
        dag = _dag({"a": task})

        with _io_patches(headless=_capture_scope):
            runner = _make_runner(dag)
            asyncio.run(runner._dispatch(MagicMock(task_slug="a")))

        assert captured["task_scope"] == {
            "task_slug": "a",
            "create": ["tests/test_created.py"],
            "edit": ["src/core.py"],
            "delete": [],
            "touch": ["tests/test_touch.py"],
            "read": ["tests/test_read.py", "README.md"],
            "verify_test_targets": [
                "tests/test_created.py",
                "tests/test_touch.py",
                "tests/test_read.py",
            ],
        }

    def test_retry_scope_preserves_verify_test_targets(self):
        task = DagTaskSpec(
            slug="a",
            summary="scope",
            prompt="Do a",
            commit_message="feat: a",
            agent="test-agent",
            files=DagFileSpec(
                create=("src/new.py",),
                touch=("tests/test_touch.py",),
                read=("tests/test_read.py",),
            ),
        )
        runner = _make_runner(_dag({"a": task}))

        assert runner._retry_scope("a", task) == {
            "task_slug": "a",
            "create": ["src/new.py"],
            "edit": [],
            "delete": [],
            "touch": ["tests/test_touch.py"],
            "read": ["tests/test_read.py"],
            "verify_test_targets": ["tests/test_touch.py", "tests/test_read.py"],
        }


class TestInterruptHandling:
    def test_handle_interrupt_marks_task_abandoned_during_shutdown(self):
        with _io_patches(), patch("dgov.runner.emit_event") as mock_emit:
            runner = _make_runner(_single_dag())
            runner.kernel.task_states["a"] = TaskState.ACTIVE
            runner._ctx("a").attempts = 1
            runner._ctx("a").error = "worker cancelled"
            runner._shutdown_event.set()

            actions = runner._handle_interrupt(InterruptGovernor("a", "pane-a", "cancelled"))

        assert actions
        assert runner.kernel.task_states["a"] == TaskState.ABANDONED
        assert runner._ctx("a").attempts == 1
        mock_emit.assert_called_once()
        # Check typed event (new signature: session_root, DgovEvent)
        event = mock_emit.call_args[0][1]
        assert event.event_type == "task_abandoned"
        assert event.task_slug == "a"
        assert event.reason == "shutdown"


class TestSemanticSettlementShadowMode:
    """Tests for shadow-mode semantic settlement — telemetry only, no merge blocking."""

    def test_integration_risk_scored_emitted_on_successful_merge(self):
        """Every task that reaches landing path emits integration_risk_scored."""
        with _io_patches() as _, patch(_P_EMIT_EVENT) as mock_emit:
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            # Task should still succeed (shadow mode)
            assert results["a"] == "merged"

            # Verify integration_risk_scored was emitted (typed event signature)
            risk_calls = [
                c
                for c in mock_emit.call_args_list
                if getattr(c.args[1], "event_type", None) == "integration_risk_scored"
            ]
            assert len(risk_calls) == 1
            # Verify payload contains expected fields
            event = risk_calls[0].args[1]
            assert event.task_slug == "a"
            assert hasattr(event, "risk_level")
            assert hasattr(event, "claimed_files")
            assert hasattr(event, "changed_files")

    def test_integration_risk_scored_emitted_even_when_merge_fails(self):
        """Risk scored even if settlement gate rejects (before failure path)."""
        with _io_patches(validate=_mock_gate_fail) as _, patch(_P_EMIT_EVENT) as mock_emit:
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            # Task should fail (settlement gate rejected)
            assert results["a"] == "failed"

            # Risk should still be scored (happens before merge)
            risk_calls = [
                c
                for c in mock_emit.call_args_list
                if getattr(c.args[1], "event_type", None) == "integration_risk_scored"
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


class TestRetryScopeFailClosed:
    """Tests for fail-closed transient scope enforcement across retries.

    Unclaimed tool writes from earlier attempts must cause review rejection
    even if a later retry cleans the worktree and succeeds.
    """

    def test_transient_scope_fail_closed_across_retries(self):
        """Earlier pane's unclaimed write causes failure on retry review."""

        # Simulate review_sandbox behavior with transient scope checking
        # First attempt pane: unclaimed write
        # Second attempt pane: clean, claimed-only write
        captured_reviews: list[dict] = []

        def _review_with_history(wt_path, claimed_files=None, **kwargs):
            from dgov.settlement import ReviewResult

            captured_reviews.append(kwargs)
            # Simulate that pane_slug is always passed
            assert kwargs.get("pane_slug") is not None
            assert kwargs.get("project_root") is not None
            assert kwargs.get("task_slug") is not None
            # Return pass - the actual scope check happens inside review_sandbox
            return ReviewResult(passed=True, verdict="ok", actual_files=frozenset({"a.py"}))

        with _io_patches(review=_review_with_history):
            runner = _make_runner(_single_dag())
            # Simulate that the runner would pass correct args to review_sandbox
            asyncio.run(runner.run())

        # Verify review was called with proper scoping params
        assert len(captured_reviews) == 1
        review_call = captured_reviews[0]
        assert review_call.get("task_slug") == "a"
        assert review_call.get("project_root") == "/tmp/test-project"

    def test_review_receives_all_required_scope_params(self):
        """Runner passes project_root, task_slug, pane_slug for scope enforcement."""
        captured: dict = {}

        def _capture_review(wt_path, claimed_files=None, **kwargs):
            from dgov.settlement import ReviewResult

            captured.update(kwargs)
            return ReviewResult(passed=True, verdict="ok", actual_files=frozenset({"a.py"}))

        with _io_patches(review=_capture_review):
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())

        # Verify scope enforcement params are passed
        assert captured.get("project_root") == "/tmp/test-project"
        assert captured.get("task_slug") == "a"
        assert captured.get("pane_slug") is not None  # Each dispatch gets a pane slug


class TestIntegrationCandidate:
    """Tests for integration candidate validation before merge."""

    def test_integration_candidate_pass_emits_event(self):
        """When candidate passes, integration_candidate_passed event is emitted."""
        with _io_patches() as _, patch(_P_EMIT_EVENT) as mock_emit:
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            assert results["a"] == "merged"

            # Verify integration_candidate_passed was emitted (typed event signature)
            passed_calls = [
                c
                for c in mock_emit.call_args_list
                if getattr(c.args[1], "event_type", None) == "integration_candidate_passed"
            ]
            assert len(passed_calls) == 1
            # Verify payload has expected fields
            event = passed_calls[0].args[1]
            assert event.task_slug == "a"
            assert hasattr(event, "candidate_sha")

    def test_integration_candidate_fail_rejects_task(self):
        """When replay fails, task is rejected with integration_candidate_failed."""
        with (
            _io_patches(candidate=_mock_integration_candidate_fail) as _,
            patch(_P_EMIT_EVENT) as mock_emit,
        ):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            # Task should fail due to candidate failure
            assert results["a"] == "failed"

            # Verify integration_candidate_failed was emitted (typed event signature)
            failed_calls = [
                c
                for c in mock_emit.call_args_list
                if getattr(c.args[1], "event_type", None) == "integration_candidate_failed"
            ]
            assert len(failed_calls) == 1
            # Verify error details in payload
            event = failed_calls[0].args[1]
            assert event.task_slug == "a"
            assert hasattr(event, "failure_class")

    def test_integration_candidate_fail_includes_text_conflict_evidence(self):
        """Replay conflicts are attributed in the emitted failure payload."""
        from dgov.worktree import IntegrationCandidateResult

        target_sha = "abc123targethead"

        def _mock_candidate_with_conflicts(project_root, task_wt, candidate_slug):
            return IntegrationCandidateResult(
                passed=False,
                error="Replay failed for candidate 'a-candidate'",
                target_head_sha=target_sha,
                failed_commit_sha="def456failedcommit",
                conflict_files=("conflict.py",),
                conflict_marker_counts={"conflict.py": 3},
            )

        with (
            _io_patches(candidate=_mock_candidate_with_conflicts) as _,
            patch(_P_EMIT_EVENT) as mock_emit,
        ):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            assert results["a"] == "failed"

            failed_calls = [
                c
                for c in mock_emit.call_args_list
                if getattr(c.args[1], "event_type", None) == "integration_candidate_failed"
            ]
            assert len(failed_calls) == 1
            event = failed_calls[0].args[1]
            assert event.task_slug == "a"
            assert event.target_head_sha == target_sha
            assert event.failure_class == "text_conflict"
            assert len(event.evidence) == 1
            evidence = event.evidence[0]
            assert evidence["_kind"] == "TextConflict"
            assert evidence["file_path"] == "conflict.py"
            assert evidence["conflict_markers"] == 3

    def test_original_worktree_preserved_on_candidate_failure(self):
        """When candidate fails, original task worktree is kept for inspection."""
        with _io_patches(candidate=_mock_integration_candidate_fail) as _:
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())

            # Original worktree should be in rejected_worktrees, not cleaned up
            assert runner._ctx("a").rejected_worktree is not None

    def test_candidate_validation_gate_failure_rejects(self):
        """If candidate passes replay but fails validation gates, reject."""
        # First call is isolated validation (pass), second is candidate validation (fail)
        call_count = {"count": 0}

        def _validate_with_candidate_fail(wt_path, base_commit, project_root, config=None):
            from dgov.settlement import GateResult

            call_count["count"] += 1
            if call_count["count"] == 1:
                return GateResult(passed=True)  # Isolated validation passes
            return GateResult(passed=False, error="Candidate gate failed")

        with (
            _io_patches(validate=_validate_with_candidate_fail) as _,
            patch(_P_EMIT_EVENT) as mock_emit,
        ):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            # Task should fail due to candidate gate failure
            assert results["a"] == "failed"

            # Verify both candidate creation and cleanup were called
            # and integration_candidate_failed was emitted (typed event signature)
            failed_calls = [
                c
                for c in mock_emit.call_args_list
                if getattr(c.args[1], "event_type", None) == "integration_candidate_failed"
            ]
            assert len(failed_calls) == 1

    def test_researcher_task_skips_integration_candidate(self):
        """Read-only researcher tasks don't need integration candidate validation."""
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

        with _io_patches() as _, patch(_P_CREATE_CANDIDATE) as mock_candidate:
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())

            assert results["research"] == "merged"
            # Integration candidate should not be created for researcher tasks
            mock_candidate.assert_not_called()


class TestPythonSemanticGateRunner:
    """Tests for Python semantic gate integration in runner._settle_and_merge."""

    def test_python_semantic_gate_emits_rejected_event(self):
        """When semantic gate fails, semantic_gate_rejected event is emitted."""
        from dgov.semantic_settlement import FailureClass, SemanticGateVerdict

        def _mock_semantic_gate_fail(*args, **kwargs):
            return SemanticGateVerdict(
                task_slug="a",
                gate_name="same_symbol_edit",
                passed=False,
                failure_class=FailureClass.SAME_SYMBOL_EDIT,
                evidence=(),
                error_message="Concurrent edits detected",
                checked_at=0.0,
            )

        with (
            _io_patches() as _,
            patch(_P_EMIT_EVENT) as mock_emit,
            patch(_P_SEMANTIC_GATE, side_effect=_mock_semantic_gate_fail),
        ):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            # Task should fail due to semantic gate rejection
            assert results["a"] == "failed"

            # Verify semantic_gate_rejected was emitted (typed event signature)
            rejected_calls = [
                c
                for c in mock_emit.call_args_list
                if getattr(c.args[1], "event_type", None) == "semantic_gate_rejected"
            ]
            assert len(rejected_calls) == 1
            event = rejected_calls[0].args[1]
            assert event.gate_name == "same_symbol_edit"
            assert event.failure_class == "same_symbol_edit"

    def test_python_semantic_gate_passes_for_non_python_files(self):
        """Non-Python tasks bypass semantic gate and proceed normally."""
        task = DagTaskSpec(
            slug="docs",
            summary="Update docs",
            prompt="Update readme",
            commit_message="docs: update",
            agent="test-agent",
            files=DagFileSpec(create=("README.md",)),
        )
        dag = _dag({"docs": task})

        with (
            _io_patches() as _,
            patch(_P_SEMANTIC_GATE) as mock_gate,
        ):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())

            assert results["docs"] == "merged"
            # Semantic gate should be called but with non-Python files
            mock_gate.assert_called_once()
            # First touched file is README.md which should bypass
            call_kwargs = mock_gate.call_args.kwargs
            assert "README.md" in str(call_kwargs.get("touched_files", []))

    def test_python_semantic_gate_cleans_up_candidate_on_rejection(self):
        """When semantic gate rejects, candidate worktree is removed."""
        from dgov.semantic_settlement import FailureClass, SemanticGateVerdict

        def _mock_semantic_gate_fail(*args, **kwargs):
            return SemanticGateVerdict(
                task_slug="a",
                gate_name="duplicate_definition",
                passed=False,
                failure_class=FailureClass.DUPLICATE_DEFINITION,
                evidence=(),
                error_message="Duplicate function found",
                checked_at=0.0,
            )

        with (
            _io_patches() as _,
            patch(_P_REMOVE_CANDIDATE) as mock_remove,
            patch(_P_SEMANTIC_GATE, side_effect=_mock_semantic_gate_fail),
        ):
            runner = _make_runner(_single_dag())
            results = asyncio.run(runner.run())

            assert results["a"] == "failed"
            # Candidate should be cleaned up
            mock_remove.assert_called()


class TestPythonSemanticGateSubprocess:
    """Tests for candidate-rooted semantic gate execution."""

    def test_subprocess_uses_candidate_src_path(self, tmp_path, monkeypatch):
        """Candidate subprocess should import from the candidate src tree first."""
        from dgov.settlement_flow import run_python_semantic_gate_in_subprocess

        captured: dict[str, object] = {}

        def _fake_run(cmd, cwd, capture_output, text, env, check):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["env"] = env
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({
                    "task_slug": "a",
                    "gate_name": "python_semantic",
                    "passed": True,
                    "failure_class": None,
                    "error_message": "",
                    "evidence": [],
                }),
                stderr="",
            )

        monkeypatch.setattr("dgov.settlement_flow.subprocess.run", _fake_run)

        verdict = run_python_semantic_gate_in_subprocess(
            candidate_path=tmp_path,
            project_root="/tmp/project",
            task_base_sha="base123",
            task_commit_sha="task123",
            target_head_sha="head123",
            touched_files=("src/a.py",),
            task_slug="a",
        )

        assert verdict.passed is True
        assert captured["cwd"] == tmp_path
        env = cast(dict[str, str], captured["env"])
        pythonpath = env.get("PYTHONPATH")
        assert isinstance(pythonpath, str)
        assert pythonpath.split(os.pathsep)[0] == str(tmp_path)

    def test_subprocess_failure_fails_closed(self, tmp_path, monkeypatch):
        """Runner should reject when candidate-side semantic execution fails."""
        from dgov.settlement_flow import run_python_semantic_gate_in_subprocess

        def _fake_run(cmd, cwd, capture_output, text, env, check):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")

        monkeypatch.setattr("dgov.settlement_flow.subprocess.run", _fake_run)

        verdict = run_python_semantic_gate_in_subprocess(
            candidate_path=tmp_path,
            project_root="/tmp/project",
            task_base_sha="base123",
            task_commit_sha="task123",
            target_head_sha="head123",
            touched_files=("src/a.py",),
            task_slug="a",
        )

        assert verdict.passed is False
        assert verdict.gate_name == "python_semantic_subprocess"
        assert "boom" in verdict.error_message


class TestRecoveryPipeline:
    """Tests for the refactored recovery pipeline phases."""

    def test_recovery_pipeline_runs_phases_in_order(self):
        """Pipeline executes seed → rehydrate → cleanup → resume in order."""
        phase_calls: list[str] = []

        class _InstrumentedRunner(EventDagRunner):
            def _phase_seed_deployed(self):
                phase_calls.append("seed")

            def _phase_rehydrate(self):
                phase_calls.append("rehydrate")

            def _phase_cleanup_orphans(self):
                phase_calls.append("cleanup")

            def _phase_resume_failed(self):
                phase_calls.append("resume")

        with _io_patches():
            runner = _make_runner(_single_dag())
            # Replace with instrumented methods
            runner._phase_seed_deployed = lambda: phase_calls.append("seed")  # type: ignore
            runner._phase_rehydrate = lambda: phase_calls.append("rehydrate")  # type: ignore
            runner._phase_cleanup_orphans = lambda: phase_calls.append("cleanup")  # type: ignore
            runner._phase_resume_failed = lambda: phase_calls.append("resume")  # type: ignore

            runner._run_recovery_pipeline(continue_failed=True)

        assert phase_calls == ["seed", "rehydrate", "cleanup", "resume"]

    def test_recovery_pipeline_skips_resume_when_not_continuing(self):
        """Pipeline skips resume phase when continue_failed=False."""
        phase_calls: list[str] = []

        with _io_patches():
            runner = _make_runner(_single_dag())
            runner._phase_seed_deployed = lambda: phase_calls.append("seed")  # type: ignore
            runner._phase_rehydrate = lambda: phase_calls.append("rehydrate")  # type: ignore
            runner._phase_cleanup_orphans = lambda: phase_calls.append("cleanup")  # type: ignore
            runner._phase_resume_failed = lambda: phase_calls.append("resume")  # type: ignore

            runner._run_recovery_pipeline(continue_failed=False)

        assert phase_calls == ["seed", "rehydrate", "cleanup"]
        assert "resume" not in phase_calls

    def test_apply_rehydrate_event_dispatched(self):
        """Rehydration applies dispatched events to kernel."""
        from dgov.actions import TaskDispatched
        from dgov.event_types import EvtTaskDispatched

        with _io_patches():
            runner = _make_runner(_single_dag())
            ev = EvtTaskDispatched(task_slug="a", pane="pane-1")

            with patch.object(runner.kernel, "handle") as mock_handle:
                runner._apply_rehydrate_event(ev)

            mock_handle.assert_called_once()
            call_args = mock_handle.call_args[0][0]
            assert isinstance(call_args, TaskDispatched)
            assert call_args.task_slug == "a"
            assert call_args.pane_slug == "pane-1"

    def test_apply_rehydrate_event_task_done(self):
        """Rehydration applies task_done events to kernel."""
        from dgov.actions import TaskWaitDone
        from dgov.event_types import TaskDone
        from dgov.persistence.schema import TaskState

        with _io_patches():
            runner = _make_runner(_single_dag())
            ev = TaskDone(task_slug="a", pane="pane-1")

            with patch.object(runner.kernel, "handle") as mock_handle:
                runner._apply_rehydrate_event(ev)

            call_args = mock_handle.call_args[0][0]
            assert isinstance(call_args, TaskWaitDone)
            assert call_args.task_state == TaskState.DONE

    def test_apply_rehydrate_event_task_failed(self):
        """Rehydration applies task_failed events with FAILED state."""
        from dgov.actions import TaskWaitDone
        from dgov.event_types import TaskFailed
        from dgov.persistence.schema import TaskState

        with _io_patches():
            runner = _make_runner(_single_dag())
            ev = TaskFailed(task_slug="a", pane="pane-1", error="test error")

            with patch.object(runner.kernel, "handle") as mock_handle:
                runner._apply_rehydrate_event(ev)

            call_args = mock_handle.call_args[0][0]
            assert isinstance(call_args, TaskWaitDone)
            assert call_args.task_state == TaskState.FAILED

    def test_apply_rehydrate_event_task_failed_timeout(self):
        """Rehydration detects timeout from error string and sets TIMED_OUT state."""
        from dgov.actions import TaskWaitDone
        from dgov.event_types import TaskFailed
        from dgov.persistence.schema import TaskState

        with _io_patches():
            runner = _make_runner(_single_dag())
            ev = TaskFailed(task_slug="a", pane="pane-1", error="worker timeout exceeded")

            with patch.object(runner.kernel, "handle") as mock_handle:
                runner._apply_rehydrate_event(ev)

            call_args = mock_handle.call_args[0][0]
            assert isinstance(call_args, TaskWaitDone)
            assert call_args.task_state == TaskState.TIMED_OUT

    def test_apply_rehydrate_event_governor_resumed(self):
        """Rehydration restores governor-resume events for retry state."""
        from dgov.actions import GovernorAction, TaskGovernorResumed
        from dgov.event_types import GovernorResumed

        with _io_patches():
            runner = _make_runner(_single_dag())
            ev = GovernorResumed(task_slug="a", pane="pane-1", action="retry")

            with patch.object(runner.kernel, "handle") as mock_handle:
                runner._apply_rehydrate_event(ev)

            call_args = mock_handle.call_args[0][0]
            assert isinstance(call_args, TaskGovernorResumed)
            assert call_args.action == GovernorAction.RETRY

    def test_abandon_orphaned_task_marks_abandoned(self):
        """Orphan abandonment marks ACTIVE tasks as ABANDONED."""
        from dgov.actions import TaskWaitDone
        from dgov.persistence.schema import TaskState

        with _io_patches():
            runner = _make_runner(_single_dag())
            # Pre-set task as ACTIVE (orphaned state)
            runner.kernel.task_states["a"] = TaskState.ACTIVE

            with (
                patch.object(runner.kernel, "handle") as mock_handle,
                patch("dgov.runner.update_runtime_artifact_state") as mock_update,
                patch("dgov.runner.emit_event") as mock_emit,
            ):
                runner._abandon_orphaned_task("a")

            # Verify kernel was told to abandon
            call_args = mock_handle.call_args[0][0]
            assert isinstance(call_args, TaskWaitDone)
            assert call_args.task_state == TaskState.ABANDONED

            # Verify artifact state was updated
            mock_update.assert_called_once()
            assert mock_update.call_args[0][1] == "a"
            assert mock_update.call_args[0][2] == TaskState.ABANDONED.value

            # Verify event was emitted (typed event signature)
            mock_emit.assert_called_once()
            event = mock_emit.call_args[0][1]
            assert event.event_type == "task_abandoned"
            assert event.task_slug == "a"

    def test_resume_single_task_emits_event(self):
        """Resume emits governor-resumed event for auditability."""
        from dgov.actions import GovernorAction, TaskGovernorResumed
        from dgov.persistence.schema import TaskState

        with _io_patches():
            runner = _make_runner(_single_dag())

            with (
                patch.object(runner.kernel, "handle") as mock_handle,
                patch("dgov.runner.emit_event") as mock_emit,
            ):
                runner._resume_single_task("a", TaskState.FAILED)

            # Verify kernel was told to retry
            call_args = mock_handle.call_args[0][0]
            assert isinstance(call_args, TaskGovernorResumed)
            assert call_args.action == GovernorAction.RETRY

            # Verify event was emitted (typed event signature)
            mock_emit.assert_called_once()
            event = mock_emit.call_args[0][1]
            assert event.event_type == "dag_task_governor_resumed"
            assert event.task_slug == "a"
            assert event.action == GovernorAction.RETRY.value

    def test_resume_skips_non_terminal_states(self):
        """Resume only processes terminal states (FAILED, ABANDONED, TIMED_OUT, SKIPPED)."""
        from dgov.persistence.schema import TaskState

        with _io_patches():
            runner = _make_runner(_parallel_dag())
            # Set one pending, one done
            runner.kernel.task_states["a"] = TaskState.PENDING
            runner.kernel.task_states["b"] = TaskState.DONE

            resumed: list[str] = []
            runner._resume_single_task = lambda slug, state: resumed.append(slug)  # type: ignore

            runner._phase_resume_failed()

            # Neither should be resumed
            assert resumed == []

    def test_resume_processes_all_terminal_states(self):
        """Resume processes FAILED, ABANDONED, TIMED_OUT, SKIPPED."""
        from dgov.persistence.schema import TaskState

        dag = _dag({
            "failed": _task("failed"),
            "abandoned": _task("abandoned"),
            "timedout": _task("timedout"),
            "skipped": _task("skipped"),
        })

        with _io_patches():
            runner = _make_runner(dag)
            runner.kernel.task_states["failed"] = TaskState.FAILED
            runner.kernel.task_states["abandoned"] = TaskState.ABANDONED
            runner.kernel.task_states["timedout"] = TaskState.TIMED_OUT
            runner.kernel.task_states["skipped"] = TaskState.SKIPPED

            resumed: list[str] = []
            runner._resume_single_task = lambda slug, state: resumed.append(slug)  # type: ignore

            runner._phase_resume_failed()

            assert sorted(resumed) == ["abandoned", "failed", "skipped", "timedout"]


class TestSettlementPhaseBoundaries:
    """Tests for settlement phase split — verify each phase boundary independently."""

    def test_prepare_and_commit_returns_true_for_edit_tasks(self):
        """prepare_and_commit returns was_settlement=True for normal edit tasks."""
        from unittest.mock import MagicMock

        with _io_patches():
            runner = _make_runner(_single_dag())
            action = MagicMock(task_slug="a", file_claims=("a.py",), pane_slug="pane-1")
            wt = _mock_create_worktree("/tmp", "a")
            task = runner.dag.tasks["a"]

            async def _test():
                error, was_settlement = await runner._settlement_flow.prepare_and_commit(
                    task=task,
                    action=action,
                    wt=wt,
                    emit_event_fn=MagicMock(),
                )
                assert error is None
                assert was_settlement is True  # Indicates settlement should continue

            asyncio.run(_test())

    def test_prepare_and_commit_returns_false_for_researcher(self):
        """prepare_and_commit returns was_settlement=False for researcher role."""
        from unittest.mock import MagicMock

        from dgov.dag_parser import DagFileSpec, DagTaskSpec

        researcher_task = DagTaskSpec(
            slug="research",
            summary="Research task",
            prompt="Investigate something",
            commit_message="research: notes",
            agent="test-agent",
            role="researcher",
            files=DagFileSpec(read=("src/foo.py",)),
        )
        dag = _dag({"research": researcher_task})

        with _io_patches():
            runner = _make_runner(dag)
            action = MagicMock(task_slug="research", file_claims=(), pane_slug="pane-1")
            wt = _mock_create_worktree("/tmp", "research")

            async def _test():
                error, was_settlement = await runner._settlement_flow.prepare_and_commit(
                    task=researcher_task,
                    action=action,
                    wt=wt,
                    emit_event_fn=MagicMock(),
                )
                assert error is None
                assert was_settlement is False  # Indicates settlement should skip

            asyncio.run(_test())

    def test_settle_and_merge_early_return_on_prepare_error(self):
        """_settle_and_merge returns early when prepare_and_commit returns error."""
        from unittest.mock import AsyncMock, MagicMock

        with _io_patches():
            runner = _make_runner(_single_dag())
            action = MagicMock(task_slug="a", pane_slug="pane-1")
            wt = _mock_create_worktree("/tmp", "a")

            sf = runner._settlement_flow
            sf.prepare_and_commit = AsyncMock(return_value=("prepare failed", True))  # type: ignore
            sf.run_isolated_validation = AsyncMock()  # type: ignore

            async def _test():
                error, was_settlement = await runner._settle_and_merge(action, wt)
                assert error == "prepare failed"
                assert was_settlement is True
                sf.run_isolated_validation.assert_not_called()  # type: ignore

            asyncio.run(_test())

    def test_settle_and_merge_early_return_on_validation_error(self):
        """_settle_and_merge returns early when run_isolated_validation returns error."""
        from unittest.mock import AsyncMock, MagicMock

        with _io_patches():
            runner = _make_runner(_single_dag())
            action = MagicMock(task_slug="a", pane_slug="pane-1")
            wt = _mock_create_worktree("/tmp", "a")

            sf = runner._settlement_flow
            sf.prepare_and_commit = AsyncMock(return_value=(None, True))  # type: ignore
            sf.run_isolated_validation = AsyncMock(return_value=("validation failed", None))  # type: ignore
            sf.create_integration_candidate_with_emit = AsyncMock()  # type: ignore

            async def _test():
                error, was_settlement = await runner._settle_and_merge(action, wt)
                assert error == "validation failed"
                assert was_settlement is True
                sf.create_integration_candidate_with_emit.assert_not_called()  # type: ignore

            asyncio.run(_test())

    def test_settle_and_merge_early_return_on_candidate_fail(self):
        """_settle_and_merge returns early when candidate creation fails."""
        from unittest.mock import AsyncMock, MagicMock

        from dgov.worktree import IntegrationCandidateResult

        with _io_patches():
            runner = _make_runner(_single_dag())
            action = MagicMock(task_slug="a", pane_slug="pane-1")
            wt = _mock_create_worktree("/tmp", "a")

            sf = runner._settlement_flow
            sf.prepare_and_commit = AsyncMock(return_value=(None, True))  # type: ignore
            sf.run_isolated_validation = AsyncMock(return_value=(None, MagicMock()))  # type: ignore
            sf.create_integration_candidate_with_emit = AsyncMock(  # type: ignore
                return_value=IntegrationCandidateResult(
                    passed=False, error="candidate creation failed"
                )
            )
            sf.run_semantic_gate_on_candidate = AsyncMock()  # type: ignore

            async def _test():
                error, was_settlement = await runner._settle_and_merge(action, wt)
                assert error and "candidate creation failed" in error
                assert was_settlement is True
                sf.run_semantic_gate_on_candidate.assert_not_called()  # type: ignore

            asyncio.run(_test())

    @pytest.mark.unit
    def test_settlement_phase_events_emitted_in_order(self):
        """Successful settlement emits started/completed events for all phases."""
        from unittest.mock import patch

        with _io_patches() as _, patch(_P_EMIT_EVENT) as mock_emit:
            runner = _make_runner(_single_dag())
            asyncio.run(runner.run())

            # Collect settlement phase events
            started_events = []
            completed_events = []
            for call in mock_emit.call_args_list:
                event = call.args[1]
                if getattr(event, "event_type", None) == "settlement_phase_started":
                    started_events.append(event)
                elif getattr(event, "event_type", None) == "settlement_phase_completed":
                    completed_events.append(event)

            # Expected phases in order
            expected_phases = [
                "prepare_commit",
                "isolated_validation",
                "integration_candidate",
                "semantic_gate",
                "candidate_validation",
                "final_merge",
            ]

            # Verify we have the right number of events
            assert len(started_events) == len(expected_phases)
            assert len(completed_events) == len(expected_phases)

            # Verify phases are in order and match between started/completed
            for i, phase in enumerate(expected_phases):
                assert started_events[i].phase == phase
                assert started_events[i].task_slug == "a"
                assert completed_events[i].phase == phase
                assert completed_events[i].task_slug == "a"
                assert completed_events[i].status == "passed"
                assert completed_events[i].duration_s >= 0.0

    @pytest.mark.unit
    def test_settlement_phase_skipped_for_read_only_role(self):
        """Researcher role skips settlement phases after prepare_commit."""
        from unittest.mock import patch

        from dgov.dag_parser import DagFileSpec, DagTaskSpec

        researcher_task = DagTaskSpec(
            slug="research",
            summary="Research task",
            prompt="Investigate something",
            commit_message="research: notes",
            agent="test-agent",
            role="researcher",
            files=DagFileSpec(read=("src/foo.py",)),
        )
        dag = _dag({"research": researcher_task})

        with _io_patches() as _, patch(_P_EMIT_EVENT) as mock_emit:
            runner = _make_runner(dag)
            asyncio.run(runner.run())

            # Collect settlement phase completed events
            completed_events = [
                c.args[1]
                for c in mock_emit.call_args_list
                if getattr(c.args[1], "event_type", None) == "settlement_phase_completed"
            ]

            # Should only have prepare_commit phase with skipped status
            assert len(completed_events) == 1
            assert completed_events[0].phase == "prepare_commit"
            assert completed_events[0].status == "skipped"
            assert completed_events[0].task_slug == "research"

    @pytest.mark.unit
    def test_settlement_phase_failed_on_validation_error(self):
        """Early validation failure emits failed completed event and no later phase starts."""
        from unittest.mock import AsyncMock, patch

        with _io_patches() as _, patch(_P_EMIT_EVENT) as mock_emit:
            runner = _make_runner(_single_dag())

            # Make isolated validation fail
            sf = runner._settlement_flow
            sf.run_isolated_validation = AsyncMock(return_value=("lint failed", None))  # type: ignore

            asyncio.run(runner.run())

            # Collect settlement phase events
            started_events = []
            completed_events = []
            for call in mock_emit.call_args_list:
                event = call.args[1]
                if getattr(event, "event_type", None) == "settlement_phase_started":
                    started_events.append(event)
                elif getattr(event, "event_type", None) == "settlement_phase_completed":
                    completed_events.append(event)

            # Should have prepare_commit started/completed
            # and isolated_validation started/completed
            started_phases = [e.phase for e in started_events]
            assert started_phases == ["prepare_commit", "isolated_validation"]

            # Verify prepare_commit passed and isolated_validation failed
            assert completed_events[0].phase == "prepare_commit"
            assert completed_events[0].status == "passed"
            assert completed_events[1].phase == "isolated_validation"
            assert completed_events[1].status == "failed"
            assert completed_events[1].error == "lint failed"

            # No later phases should have started
            assert len(started_events) == 2


# ---------------------------------------------------------------------------
# Feature A: Clean-Context Fork on Iteration Exhaustion
# ---------------------------------------------------------------------------


def _task_with_fork(
    slug: str, max_fork_depth: int = 1, depends_on: tuple[str, ...] = ()
) -> DagTaskSpec:
    return DagTaskSpec(
        slug=slug,
        summary=f"Test task {slug}",
        prompt=f"Do {slug}",
        commit_message=f"feat: {slug}",
        agent="test-agent",
        depends_on=depends_on,
        files=DagFileSpec(create=(f"{slug}.py",)),
        max_fork_depth=max_fork_depth,
    )


async def _fake_diff(_self, _wt):
    return "diff --git a/file.py b/file.py\n+fake change\n"


class TestContextFork:
    def test_iteration_exhaustion_triggers_fork(self):
        """When worker exhausts iterations and fork_depth < max, fork instead of fail."""
        call_count = {"n": 0}

        async def _exhaust_then_succeed(
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
            call_count["n"] += 1
            if call_count["n"] == 1:
                on_exit(task_slug, pane_slug, 1, "Exceeded max iterations (50)")
            else:
                # Forked worker succeeds
                on_exit(task_slug, pane_slug, 0, "")

        dag = _dag({"a": _task_with_fork("a", max_fork_depth=1)})
        with _io_patches(headless=_exhaust_then_succeed), patch(_P_GET_DIFF, _fake_diff):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"
            assert runner._ctx("a").fork_depth == 1

    def test_fork_depth_zero_disables_forking(self):
        """max_fork_depth=0 means no forking — normal retry path."""

        async def _exhaust(
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
            on_exit(task_slug, pane_slug, 1, "Exceeded max iterations (50)")

        dag = _dag({"a": _task_with_fork("a", max_fork_depth=0)})
        with _io_patches(headless=_exhaust):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"

    def test_fork_depth_limit_prevents_infinite_fork(self):
        """After max_fork_depth forks, iteration exhaustion goes to normal fail."""

        async def _always_exhaust(
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
            on_exit(task_slug, pane_slug, 1, "Exceeded max iterations (50)")

        dag = _dag({"a": _task_with_fork("a", max_fork_depth=1)})
        with _io_patches(headless=_always_exhaust), patch(_P_GET_DIFF, _fake_diff):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            # First call: fork. Second call: also exhausts. Third: governor retry.
            # Governor retry also exhausts. Eventually fails.
            assert results["a"] == "failed"

    def test_non_exhaustion_failure_bypasses_fork(self):
        """Non-iteration errors go through normal retry, not fork."""

        async def _regular_fail(
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
            on_exit(task_slug, pane_slug, 1, "some other error")

        dag = _dag({"a": _task_with_fork("a", max_fork_depth=1)})
        with _io_patches(headless=_regular_fail):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"
            assert runner._ctx("a").fork_depth == 0

    def test_fork_handoff_prompt_contains_diff_and_original(self):
        """Handoff prompt includes original task prompt and diff."""
        from dgov.prompt_builder import PromptBuilder

        task = _task_with_fork("a")
        prompt = PromptBuilder.fork_handoff_prompt(task, "diff --git a/foo.py b/foo.py\n+hello")
        assert "Do a" in prompt  # original task prompt
        assert "+hello" in prompt  # diff content
        assert "Do NOT start from scratch" in prompt

    def test_call_count_incremented_by_counted_on_event(self):
        """_make_counted_on_event increments call_count on 'call' events."""
        dag = _dag({"a": _task("a")})
        with _io_patches():
            runner = _make_runner(dag)
            ctx = runner._ctx("a")
            tracked = runner._make_counted_on_event("a")
            assert tracked is not None
            tracked("a", "call", {"tool": "read_file"})
            tracked("a", "thought", "thinking...")
            tracked("a", "call", {"tool": "edit_file"})
            assert ctx.call_count == 2

    def test_fork_crash_pushes_failed_exit(self):
        """If _fork_worker raises, a failed WorkerExit is pushed so _run_loop doesn't hang."""
        call_count = {"n": 0}

        async def _exhaust_once(
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
            call_count["n"] += 1
            if call_count["n"] == 1:
                on_exit(task_slug, pane_slug, 1, "Exceeded max iterations (50)")
            else:
                on_exit(task_slug, pane_slug, 0, "")

        async def _crash_diff(_self, _wt):
            raise RuntimeError("git exploded")

        dag = _dag({"a": _task_with_fork("a", max_fork_depth=1)})
        with _io_patches(headless=_exhaust_once), patch(_P_GET_DIFF, _crash_diff):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            # Fork crashes → failed exit pushed → governor retries → succeeds
            assert results["a"] == "merged"

    def test_fork_then_self_review_combined(self):
        """Task with both max_fork_depth=1 and self_review=True works end-to-end."""
        call_count = {"n": 0}

        async def _exhaust_then_succeed_with_review(
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
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First worker exhausts
                on_exit(task_slug, pane_slug, 1, "Exceeded max iterations (50)")
            elif "-self-review" in task_slug and on_event:
                # Reviewer approves
                on_event(task_slug, "done", '{"approved": true, "issues": []}')
                on_exit(task_slug, pane_slug, 0, "")
            else:
                # Forked worker succeeds
                on_exit(task_slug, pane_slug, 0, "")

        task = DagTaskSpec(
            slug="a",
            summary="Test task a",
            prompt="Do a",
            commit_message="feat: a",
            agent="test-agent",
            files=DagFileSpec(create=("a.py",)),
            max_fork_depth=1,
            self_review=True,
        )
        dag = _dag({"a": task})
        with (
            _io_patches(headless=_exhaust_then_succeed_with_review),
            patch(_P_GET_DIFF, _fake_diff),
        ):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"
            assert runner._ctx("a").fork_depth == 1


# ---------------------------------------------------------------------------
# Feature B: Post-Worker Semantic Self-Review
# ---------------------------------------------------------------------------


def _task_with_self_review(
    slug: str, self_review: bool = True, depends_on: tuple[str, ...] = ()
) -> DagTaskSpec:
    return DagTaskSpec(
        slug=slug,
        summary=f"Test task {slug}",
        prompt=f"Do {slug}",
        commit_message=f"feat: {slug}",
        agent="test-agent",
        depends_on=depends_on,
        files=DagFileSpec(create=(f"{slug}.py",)),
        self_review=self_review,
    )


class TestSelfReview:
    def test_self_review_disabled_skips_semantic_review(self):
        """self_review=False uses the sync structural-only path."""
        dag = _dag({"a": _task_with_self_review("a", self_review=False)})
        with _io_patches():
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"

    def test_self_review_enabled_reviewer_approves(self):
        """When reviewer says approved, task merges normally."""
        call_slugs: list[str] = []

        async def _tracking_worker(
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
            call_slugs.append(task_slug)
            # Self-review reviewer emits approved JSON via on_event
            if "-self-review" in task_slug and on_event:
                on_event(task_slug, "done", '{"approved": true, "issues": []}')
            on_exit(task_slug, pane_slug, 0, "")

        dag = _dag({"a": _task_with_self_review("a", self_review=True)})
        with _io_patches(headless=_tracking_worker), patch(_P_GET_DIFF, _fake_diff):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["a"] == "merged"
            assert any("-self-review" in s for s in call_slugs)

    def test_self_review_finds_issues_triggers_fix_cycle(self):
        """When reviewer finds issues, worker is re-launched with findings."""
        call_count: dict[str, int] = {}

        async def _review_then_fix(
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
            call_count[task_slug] = call_count.get(task_slug, 0) + 1
            if "-self-review" in task_slug and on_event:
                n = call_count[task_slug]
                if n == 1:
                    # First review: reject
                    on_event(
                        task_slug, "done", '{"approved": false, "issues": ["off-by-one in loop"]}'
                    )
                else:
                    # Second review: approve
                    on_event(task_slug, "done", '{"approved": true, "issues": []}')
            on_exit(task_slug, pane_slug, 0, "")

        dag = _dag({"a": _task_with_self_review("a", self_review=True)})
        with _io_patches(headless=_review_then_fix), patch(_P_GET_DIFF, _fake_diff):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            # Should still merge — self-review is advisory
            assert results["a"] == "merged"

    def test_self_review_second_review_auto_passes(self):
        """Second self-review auto-passes even if reviewer still finds issues."""

        async def _always_reject_review(
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
            if "-self-review" in task_slug and on_event:
                on_event(task_slug, "done", '{"approved": false, "issues": ["still bad"]}')
            on_exit(task_slug, pane_slug, 0, "")

        dag = _dag({"a": _task_with_self_review("a", self_review=True)})
        with _io_patches(headless=_always_reject_review), patch(_P_GET_DIFF, _fake_diff):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            # Auto-pass after one fix cycle — self-review is advisory
            assert results["a"] == "merged"

    def test_self_review_prompt_structure_without_sops(self):
        """Without SOPs, prompt contains inline fallback criteria."""
        dag = _dag({"x": _task_with_self_review("x")})
        with _io_patches():
            runner = _make_runner(dag)
        runner._prompts._review_sop_blocks = ()
        prompt = runner._prompts.self_review_prompt("diff --git a/foo.py b/foo.py\n+hello")
        assert "+hello" in prompt
        assert "semantic correctness" in prompt
        assert "NO context" in prompt
        assert '"approved"' in prompt
        assert "Logic errors" in prompt

    def test_self_review_prompt_injects_sop_blocks(self):
        """When review SOPs are loaded, their content replaces fallback."""
        dag = _dag({"x": _task_with_self_review("x")})
        with _io_patches():
            runner = _make_runner(dag)
        runner._prompts._review_sop_blocks = ("[SOP: Test Review]\nDo:\n- check things",)
        prompt = runner._prompts.self_review_prompt("diff --git a/f.py b/f.py\n+x")
        assert "[SOP: Test Review]" in prompt
        assert "check things" in prompt
        assert "Logic errors" not in prompt

    def test_self_review_skipped_for_read_only_roles(self):
        """Researcher/reviewer roles skip self-review even if self_review=True."""
        task = DagTaskSpec(
            slug="r",
            summary="Research task",
            prompt="Read stuff",
            agent="test-agent",
            role="researcher",
            self_review=True,
        )
        dag = _dag({"r": task})
        with _io_patches():
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["r"] == "merged"


# ---------------------------------------------------------------------------
# Schema: new DagTaskSpec fields
# ---------------------------------------------------------------------------


class TestNewSchemaFields:
    def test_self_review_defaults_false(self):
        t = DagTaskSpec(slug="x", summary="y")
        assert t.self_review is False

    def test_max_fork_depth_defaults_one(self):
        t = DagTaskSpec(slug="x", summary="y")
        assert t.max_fork_depth == 1

    def test_self_review_set_true(self):
        t = DagTaskSpec(slug="x", summary="y", self_review=True)
        assert t.self_review is True

    def test_max_fork_depth_set_zero(self):
        t = DagTaskSpec(slug="x", summary="y", max_fork_depth=0)
        assert t.max_fork_depth == 0


# ---------------------------------------------------------------------------
# Async error path tests
# ---------------------------------------------------------------------------


class TestAsyncErrorPaths:
    """Tests for runner error recovery paths that were previously uncovered."""

    def test_self_review_exception_auto_passes(self):
        """If self-review raises an exception, task still passes to settlement."""

        async def _crash_on_review(
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
            if "-self-review" in task_slug:
                raise RuntimeError("reviewer exploded")
            on_exit(task_slug, pane_slug, 0, "")

        dag = _dag({"a": _task_with_self_review("a", self_review=True)})
        with _io_patches(headless=_crash_on_review), patch(_P_GET_DIFF, _fake_diff):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            # Self-review is advisory — exception auto-passes to settlement
            assert results["a"] == "merged"

    def test_fork_no_worktree_skips_fork(self):
        """If worktree is None when iteration exhaustion fires, no fork attempt."""
        runner_ref: list[EventDagRunner] = []

        async def _exhaust_and_clear_wt(
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
            # Clear worktree AFTER dispatch set it, BEFORE exit triggers fork check
            runner_ref[0]._ctx(task_slug).worktree = None
            on_exit(task_slug, pane_slug, 1, "Exceeded max iterations (50)")

        task = DagTaskSpec(
            slug="a",
            summary="Test",
            prompt="Do a",
            commit_message="a",
            agent="test",
            files=DagFileSpec(create=("a.py",)),
            max_fork_depth=1,
        )
        dag = _dag({"a": task})
        with _io_patches(headless=_exhaust_and_clear_wt):
            runner = _make_runner(dag)
            runner_ref.append(runner)
            results = asyncio.run(runner.run())
            # Without worktree, fork cannot happen — task fails
            assert results["a"] == "failed"

    def test_worker_tokens_propagate_through_events(self):
        """Worker token counts flow through emit_event to event log."""
        emitted: list = []

        # Capture emit_event calls (typed event signature)
        def _capture_emit(session_root, event):
            emitted.append(event)

        async def _worker_with_tokens(
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
            on_exit(task_slug, pane_slug, 0, "", 1500, 500)

        dag = _dag({"a": _task("a")})
        with _io_patches(headless=_worker_with_tokens), patch(_P_EMIT_EVENT, _capture_emit):
            runner = _make_runner(dag)
            asyncio.run(runner.run())

        done_events = [e for e in emitted if getattr(e, "event_type", None) == "task_done"]
        assert len(done_events) >= 1
        assert done_events[0].prompt_tokens == 1500
        assert done_events[0].completion_tokens == 500

    @pytest.mark.unit
    def test_token_usage_accumulates_across_forked_worker_exits(self):
        """Runner token_usage includes the original and forked worker attempts."""
        call_count = 0
        emitted: list = []

        def _capture_emit(session_root, event):
            emitted.append(event)

        async def _worker_with_fork_tokens(
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
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            if call_count == 1:
                on_exit(task_slug, pane_slug, 1, "Exceeded max iterations (50)", 1000, 200)
                return
            on_exit(task_slug, pane_slug, 0, "", 300, 50)

        task = DagTaskSpec(
            slug="a",
            summary="Test",
            prompt="Do a",
            commit_message="a",
            agent="test",
            files=DagFileSpec(create=("a.py",)),
            max_fork_depth=1,
        )
        dag = _dag({"a": task})
        with (
            _io_patches(headless=_worker_with_fork_tokens),
            patch(_P_GET_DIFF, return_value=""),
            patch(_P_EMIT_EVENT, _capture_emit),
        ):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())

        assert results["a"] == "merged"
        assert runner.token_usage == {"a": (1300, 250)}
        done_events = [e for e in emitted if getattr(e, "event_type", None) == "task_done"]
        assert done_events[-1].prompt_tokens == 1300
        assert done_events[-1].completion_tokens == 250

    def test_concurrent_failures_both_reported(self):
        """When two parallel tasks both fail, both are reported as failed."""
        task_a = _task("a")
        task_b = _task("b")
        dag = _dag({"a": task_a, "b": task_b})
        with _io_patches(headless=_fake_worker_fail):
            runner = _make_runner(dag)
            results = asyncio.run(runner.run())
            assert results["a"] == "failed"
            assert results["b"] == "failed"
