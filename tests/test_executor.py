from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

import dgov.lifecycle as _lifecycle  # noqa: F401 - bind real persistence symbols before patches
from dgov.executor import (
    CleanupOnlyResult,
    EscalateResult,
    RetryResult,
    derive_prompt_touches,
    resolve_touches,
    review_merge_gate,
    run_cleanup_only,
    run_dispatch_preflight,
    run_escalate_only,
    run_land_only,
    run_post_dispatch_lifecycle,
    run_retry_only,
    run_review_merge,
    run_review_only,
    run_wait_only,
)
from dgov.inspection import ReviewInfo
from dgov.merger import MergeError, MergeSuccess

pytestmark = pytest.mark.unit


def test_derive_prompt_touches_dedupes_paths(monkeypatch):
    monkeypatch.setattr(
        "dgov.strategy.extract_task_context",
        lambda prompt: {
            "primary_files": ["src/a.py", "src/a.py"],
            "also_check": ["src/b.py"],
            "tests": ["tests/test_a.py", "src/b.py"],
            "hints": [],
        },
    )

    assert derive_prompt_touches("fix it") == ["src/a.py"]


def test_run_dispatch_preflight_prefers_explicit_touches(tmp_path):
    fake_report = MagicMock()

    with patch("dgov.preflight.run_preflight", return_value=fake_report) as mock_preflight:
        result = run_dispatch_preflight(
            "/repo",
            "claude",
            prompt="fix src/a.py",
            touches=["src/exact.py", "tests/test_exact.py"],
            session_root=str(tmp_path),
        )

    assert result is fake_report
    mock_preflight.assert_called_once_with(
        project_root="/repo",
        agent="claude",
        touches=["src/exact.py", "tests/test_exact.py"],
        expected_branch=None,
        session_root=str(tmp_path),
        skip_deps=True,
        derived_only=False,
    )


def test_review_merge_gate_blocks_zero_commit(tmp_path):
    with patch(
        "dgov.inspection.review_worker_pane",
        return_value=ReviewInfo(slug="task", verdict="safe", commit_count=0),
    ):
        gate = review_merge_gate("/repo", "task", session_root=str(tmp_path))

    assert gate.passed is False
    assert gate.error == "No commits to merge"


def test_review_merge_gate_blocks_non_safe_verdict(tmp_path):
    with patch(
        "dgov.inspection.review_worker_pane",
        return_value=ReviewInfo(slug="task", verdict="review", commit_count=2),
    ):
        gate = review_merge_gate("/repo", "task", session_root=str(tmp_path))

    assert gate.passed is False
    assert gate.error == "Review verdict is review; refusing to merge"


def test_run_review_only_returns_typed_review_result(tmp_path):
    with patch(
        "dgov.inspection.review_worker_pane",
        return_value=ReviewInfo(slug="task", verdict="safe", commit_count=2),
    ):
        result = run_review_only("/repo", "task", session_root=str(tmp_path))

    assert result.slug == "task"
    assert result.passed is True
    assert result.verdict == "safe"
    assert result.commit_count == 2
    assert result.error is None
    assert result.review_record is not None
    assert "inspection-review" in result.review_record.provider_id
    assert result.review_record.decision.commit_count == 2


def test_run_review_only_can_skip_review_events(tmp_path):
    with patch(
        "dgov.inspection.review_worker_pane",
        return_value=ReviewInfo(slug="task", verdict="safe", commit_count=2),
    ) as mock_review:
        result = run_review_only("/repo", "task", session_root=str(tmp_path), emit_events=False)

    assert result.passed is True
    assert mock_review.call_args.kwargs["emit_events"] is False


def test_run_review_only_persists_decision_journal(tmp_path):
    from dgov.persistence import read_decision_journal

    session_root = str(tmp_path)

    with patch(
        "dgov.inspection.review_worker_pane",
        return_value=ReviewInfo(slug="task", verdict="safe", commit_count=2),
    ):
        result = run_review_only("/repo", "task", session_root=session_root)

    journal = read_decision_journal(session_root)
    assert result.passed is True
    assert len(journal) == 1
    assert journal[0]["kind"] == "review_output"
    assert journal[0]["provider_id"] in ("inspection-review", "cascade")
    assert journal[0]["result"]["decision"]["verdict"] == "safe"


def test_run_review_only_fails_on_stale_files(tmp_path):

    session_root = str(tmp_path)
    pane_data = {
        "base_sha": "abc123",
        "file_claims": ["src/foo.py"],
        "worktree_path": "",
    }
    with (
        patch(
            "dgov.inspection.review_worker_pane",
            return_value=ReviewInfo(slug="task", verdict="safe", commit_count=2),
        ),
        patch("dgov.persistence.get_pane", return_value=pane_data),
        patch(
            "dgov.gitops.build_manifest_on_completion",
            return_value=MagicMock(
                base_sha="abc123",
                file_claims=("src/foo.py",),
                paths_written=("src/foo.py",),
            ),
        ),
        patch(
            "dgov.gitops.validate_manifest_freshness",
            return_value=(False, ["src/foo.py"]),
        ),
    ):
        result = run_review_only("/repo", "task", session_root=session_root)

    # Stale files are recorded in review as warning, but don't block merge
    assert result.review.stale_files == ["src/foo.py"]
    assert result.review.freshness == "warn"
    assert result.passed is True  # staleness is a warning, merge-tree decides


def test_run_review_only_passes_when_manifest_fresh(tmp_path):

    session_root = str(tmp_path)
    pane_data = {
        "base_sha": "abc123",
        "file_claims": ["src/foo.py"],
        "worktree_path": "",
    }
    with (
        patch(
            "dgov.inspection.review_worker_pane",
            return_value=ReviewInfo(slug="task", verdict="safe", commit_count=2),
        ),
        patch("dgov.persistence.get_pane", return_value=pane_data),
        patch(
            "dgov.gitops.build_manifest_on_completion",
            return_value=MagicMock(
                base_sha="abc123",
                file_claims=("src/foo.py",),
                paths_written=("src/foo.py",),
            ),
        ),
        patch(
            "dgov.gitops.validate_manifest_freshness",
            return_value=(True, []),
        ),
    ):
        result = run_review_only("/repo", "task", session_root=session_root)

    # No stale files in review when fresh
    assert result.review.stale_files == []
    assert result.passed is True
    assert result.error is None


def test_run_wait_only_returns_worker_failed_state(tmp_path):
    with (
        patch(
            "dgov.waiter.wait_worker_pane",
            return_value={"done": "task", "method": "signal"},
        ),
        patch("dgov.persistence.get_pane", return_value={"state": "failed"}),
    ):
        result = run_wait_only("/repo", "task", session_root=str(tmp_path), max_retries=0)

    assert result.state == "failed"
    assert result.slug == "task"
    assert result.failure_stage == "worker_failed"


def test_run_wait_only_returns_timeout_when_retries_exhausted(tmp_path):
    from dgov.waiter import PaneTimeoutError

    with patch(
        "dgov.waiter.wait_worker_pane",
        side_effect=PaneTimeoutError("task", 30, "claude"),
    ):
        result = run_wait_only(
            "/repo",
            "task",
            session_root=str(tmp_path),
            timeout=30,
            max_retries=0,
        )

    assert result.state == "failed"
    assert result.failure_stage == "timeout"
    assert result.error == "Worker timed out after 30s (retries exhausted)"


def test_run_cleanup_only_preserves_inspectable_outcomes(tmp_path):
    with patch("dgov.persistence.mark_preserved_artifacts") as mock_mark:
        result = run_cleanup_only(
            "/repo",
            "task",
            session_root=str(tmp_path),
            state="review_pending",
        )

    assert result == CleanupOnlyResult(
        slug="task",
        action="preserve",
        reason="review_pending",
    )
    mock_mark.assert_called_once_with(
        str(tmp_path),
        "task",
        reason="review_pending",
        recoverable=False,
        state="review_pending",
        failure_stage=None,
    )


def test_run_cleanup_only_force_closes_worker_failed(tmp_path):
    with patch(
        "dgov.lifecycle.close_worker_pane",
        return_value=True,
    ) as mock_close:
        result = run_cleanup_only(
            "/repo",
            "task",
            session_root=str(tmp_path),
            state="failed",
            failure_stage="worker_failed",
        )

    assert result.action == "close"
    assert result.closed is True
    assert result.force is True
    mock_close.assert_called_once_with(
        "/repo",
        "task",
        session_root=str(tmp_path),
        force=True,
    )


class TestPostDispatchLifecycle:
    def test_completed_lifecycle_merges_after_safe_review(self, tmp_path):
        phases: list[tuple[str, str]] = []

        with (
            patch(
                "dgov.waiter.wait_worker_pane", return_value={"done": "task", "method": "signal"}
            ),
            patch("dgov.persistence.get_pane", return_value={"slug": "task", "state": "done"}),
            patch(
                "dgov.inspection.review_worker_pane",
                return_value=ReviewInfo(slug="task", verdict="safe", commit_count=1),
            ),
            patch(
                "dgov.merger.merge_worker_pane",
                return_value=MergeSuccess(merged="task", branch="task"),
            ),
        ):
            result = run_post_dispatch_lifecycle(
                "/repo",
                "task",
                session_root=str(tmp_path),
                phase_callback=lambda phase, slug: phases.append((phase, slug)),
            )

        assert result.state == "completed"
        assert result.slug == "task"
        assert result.merge_result.merged == "task"
        assert result.cleanup == CleanupOnlyResult(
            slug="task",
            action="preserve",
            reason="completed",
        )
        assert phases == [
            ("waiting", "task"),
            ("reviewing", "task"),
            ("merging", "task"),
            ("completed", "task"),
        ]

    def test_reviewed_pass_preserves_artifacts_when_auto_merge_disabled(self, tmp_path):
        phases: list[tuple[str, str]] = []

        with (
            patch(
                "dgov.waiter.wait_worker_pane", return_value={"done": "task", "method": "signal"}
            ),
            patch("dgov.persistence.get_pane", return_value={"slug": "task", "state": "done"}),
            patch(
                "dgov.inspection.review_worker_pane",
                return_value=ReviewInfo(slug="task", verdict="safe", commit_count=1),
            ),
            patch("dgov.merger.merge_worker_pane") as mock_merge,
        ):
            result = run_post_dispatch_lifecycle(
                "/repo",
                "task",
                session_root=str(tmp_path),
                auto_merge=False,
                phase_callback=lambda phase, slug: phases.append((phase, slug)),
            )

        assert result.state == "reviewed_pass"
        assert result.slug == "task"
        assert result.merge_result is None
        assert result.cleanup == CleanupOnlyResult(
            slug="task",
            action="preserve",
            reason="review_pending",
        )
        mock_merge.assert_not_called()
        assert phases == [
            ("waiting", "task"),
            ("reviewing", "task"),
        ]

    def test_timeout_retry_restarts_wait_on_new_slug(self, tmp_path):
        phases: list[tuple[str, str]] = []
        wait_calls: list[str] = []

        def _wait_side_effect(project_root, slug, **kwargs):
            from dgov.waiter import PaneTimeoutError

            wait_calls.append(slug)
            if slug == "task":
                raise PaneTimeoutError("task", 30, "claude")
            return {"done": slug, "method": "signal"}

        with (
            patch("dgov.waiter.wait_worker_pane", side_effect=_wait_side_effect),
            patch(
                "dgov.recovery.retry_worker_pane",
                return_value={"retried": True, "new_slug": "task-2"},
            ) as mock_retry,
            patch("dgov.persistence.get_pane", return_value={"slug": "task-2", "state": "done"}),
            patch(
                "dgov.inspection.review_worker_pane",
                return_value=ReviewInfo(slug="task-2", verdict="safe", commit_count=1),
            ),
            patch(
                "dgov.merger.merge_worker_pane",
                return_value=MergeSuccess(merged="task-2", branch="task-2"),
            ),
        ):
            result = run_post_dispatch_lifecycle(
                "/repo",
                "task",
                session_root=str(tmp_path),
                max_retries=1,
                retry_agent="claude",
                phase_callback=lambda phase, slug: phases.append((phase, slug)),
            )

        assert result.state == "completed"
        assert result.slug == "task-2"
        assert wait_calls == ["task", "task-2"]
        mock_retry.assert_called_once_with(
            "/repo",
            "task",
            session_root=str(tmp_path),
            agent="claude",
        )
        assert phases == [
            ("waiting", "task"),
            ("waiting", "task-2"),
            ("reviewing", "task-2"),
            ("merging", "task-2"),
            ("completed", "task-2"),
        ]

    def test_review_pending_returns_without_merge(self, tmp_path):
        phases: list[tuple[str, str]] = []

        with (
            patch(
                "dgov.waiter.wait_worker_pane", return_value={"done": "task", "method": "signal"}
            ),
            patch("dgov.persistence.get_pane", return_value={"slug": "task", "state": "done"}),
            patch(
                "dgov.inspection.review_worker_pane",
                return_value=ReviewInfo(slug="task", verdict="review", commit_count=1),
            ),
            patch("dgov.merger.merge_worker_pane") as mock_merge,
        ):
            result = run_post_dispatch_lifecycle(
                "/repo",
                "task",
                session_root=str(tmp_path),
                phase_callback=lambda phase, slug: phases.append((phase, slug)),
            )

        assert result.state == "review_pending"
        assert result.slug == "task"
        assert result.cleanup == CleanupOnlyResult(
            slug="task",
            action="preserve",
            reason="review_pending",
        )
        mock_merge.assert_not_called()
        assert phases == [
            ("waiting", "task"),
            ("reviewing", "task"),
        ]

    def test_worker_failed_lifecycle_closes_forcefully(self, tmp_path):
        with (
            patch(
                "dgov.waiter.wait_worker_pane",
                return_value={"done": "task", "method": "signal"},
            ),
            patch("dgov.persistence.get_pane", return_value={"slug": "task", "state": "failed"}),
            patch("dgov.recovery.maybe_auto_retry", return_value=None),
            patch(
                "dgov.lifecycle.close_worker_pane",
                return_value=True,
            ) as mock_close,
        ):
            result = run_post_dispatch_lifecycle(
                "/repo",
                "task",
                session_root=str(tmp_path),
            )

        assert result.state == "failed"
        assert result.failure_stage == "worker_failed"
        assert result.cleanup == CleanupOnlyResult(
            slug="task",
            action="close",
            reason="worker_failed",
            closed=True,
            force=True,
        )
        mock_close.assert_called_once_with(
            "/repo",
            "task",
            session_root=str(tmp_path),
            force=True,
        )


class TestReviewMerge:
    def test_dag_reactor_executes_merge_task(self, tmp_path):
        from types import SimpleNamespace

        from dgov.executor import DagReactor
        from dgov.kernel import MergeTask, TaskMergeDone

        dag = SimpleNamespace(
            tasks={"task": SimpleNamespace(commit_message="merge task")},
            merge_resolve="manual",
            merge_squash=False,
        )

        with (
            patch("dgov.persistence.get_pane", return_value=None),
            patch(
                "dgov.executor.run_merge_only",
                return_value=SimpleNamespace(error=None),
            ) as mock_merge,
        ):
            result = DagReactor("/repo", str(tmp_path), 1, dag).execute(
                MergeTask("task", "pane-1")
            )

        assert isinstance(result, TaskMergeDone)
        assert result.task_slug == "task"
        assert result.error is None
        mock_merge.assert_called_once_with(
            "/repo",
            "pane-1",
            session_root=str(tmp_path),
            resolve="manual",
            squash=False,
            message="merge task",
        )

    def test_dag_retry_resets_attempt_on_escalation(self, tmp_path):
        """_dag_retry returns attempt=0 when retry_or_escalate escalates (ledger #71)."""
        from dgov.executor import _dag_retry
        from dgov.kernel import TaskRetryStarted

        with (
            patch(
                "dgov.recovery.retry_or_escalate",
                return_value={
                    "action": "escalate",
                    "agent": "qwen-35b",
                    "new_slug": "pane-2",
                    "from_agent": "qwen-9b",
                },
            ),
            patch("dgov.persistence.upsert_dag_task"),
        ):
            event = _dag_retry("/repo", str(tmp_path), 1, "task-a", "pane-1", 2, 2, lambda _: None)

        assert isinstance(event, TaskRetryStarted)
        assert event.task_slug == "task-a"
        assert event.new_pane_slug == "pane-2"
        assert event.attempt == 0  # reset for new tier

    def test_dag_retry_preserves_attempt_on_same_tier(self, tmp_path):
        """_dag_retry keeps attempt unchanged on same-tier retry."""
        from dgov.executor import _dag_retry
        from dgov.kernel import TaskRetryStarted

        with (
            patch(
                "dgov.recovery.retry_or_escalate",
                return_value={
                    "action": "retry",
                    "agent": "qwen-9b",
                    "new_slug": "pane-2",
                },
            ),
            patch("dgov.persistence.upsert_dag_task"),
        ):
            event = _dag_retry("/repo", str(tmp_path), 1, "task-a", "pane-1", 2, 2, lambda _: None)

        assert isinstance(event, TaskRetryStarted)
        assert event.attempt == 2  # unchanged

    def test_run_review_merge_blocks_non_safe_review(self, tmp_path):
        with patch(
            "dgov.inspection.review_worker_pane",
            return_value=ReviewInfo(slug="task", verdict="review", commit_count=1),
        ):
            result = run_review_merge("/repo", "task", session_root=str(tmp_path))

        assert result.slug == "task"
        assert result.error == "Review verdict is review; refusing to merge"
        assert result.merge_result is None

    def test_run_review_merge_returns_merge_result(self, tmp_path):
        with (
            patch(
                "dgov.inspection.review_worker_pane",
                return_value=ReviewInfo(slug="task", verdict="safe", commit_count=2),
            ),
            patch(
                "dgov.executor.run_merge_only",
                return_value=MagicMock(
                    error=None,
                    merge_result=MergeSuccess(merged="task", branch="task"),
                ),
            ) as mock_merge,
        ):
            result = run_review_merge(
                "/repo",
                "task",
                session_root=str(tmp_path),
                resolve="agent",
                squash=False,
                rebase=False,
            )

        assert result.error is None
        assert result.merge_result.merged == "task"
        mock_merge.assert_called_once_with(
            "/repo",
            "task",
            session_root=str(tmp_path),
            resolve="agent",
            squash=False,
            rebase=False,
        )

    def test_run_review_merge_blocks_zero_commits(self, tmp_path):
        """run_review_merge should fail-fast on 0 commits (ledger #73)."""
        with patch(
            "dgov.inspection.review_worker_pane",
            return_value=ReviewInfo(slug="task", verdict="safe", commit_count=0),
        ):
            result = run_review_merge("/repo", "task", session_root=str(tmp_path))

        assert result.error == "No commits to merge"
        assert result.merge_result is None

    def test_run_land_only_closes_after_successful_merge(self, tmp_path):
        with (
            patch("dgov.persistence.get_pane", return_value={"slug": "task", "state": "done"}),
            patch(
                "dgov.executor.run_review_merge",
                return_value=MagicMock(
                    slug="task",
                    review={"slug": "task", "verdict": "safe", "commit_count": 2},
                    review_record=None,
                    merge_result=MergeSuccess(merged="task", branch="task"),
                    failure_stage=None,
                    error=None,
                ),
            ),
            patch("dgov.lifecycle.close_worker_pane", return_value=True) as mock_close,
        ):
            result = run_land_only("/repo", "task", session_root=str(tmp_path))

        assert result.error is None
        assert result.merge_result.merged == "task"
        assert result.cleanup == CleanupOnlyResult(
            slug="task",
            action="close",
            reason="landed",
            closed=True,
            force=False,
        )
        mock_close.assert_called_once_with("/repo", "task", session_root=str(tmp_path))


# =============================================================================
# Retry and Escalate Tests
# =============================================================================


def test_run_retry_only_success(monkeypatch, tmp_path):
    """Test run_retry_only with successful retry."""
    monkeypatch.setattr(
        "dgov.recovery.retry_worker_pane",
        lambda *args, **kwargs: {"new_slug": "s2"},
    )
    result = run_retry_only("/repo", "task1", session_root=str(tmp_path))
    assert isinstance(result, RetryResult)
    assert result.slug == "task1"
    assert result.new_slug == "s2"
    assert result.error is None


def test_run_retry_only_error(monkeypatch, tmp_path):
    """Test run_retry_only with error response."""
    monkeypatch.setattr(
        "dgov.recovery.retry_worker_pane",
        lambda *args, **kwargs: {"error": "retry failed"},
    )
    result = run_retry_only("/repo", "task1", session_root=str(tmp_path))
    assert isinstance(result, RetryResult)
    assert result.slug == "task1"
    assert result.new_slug is None
    assert result.error == "retry failed"


def test_run_escalate_only_success(monkeypatch, tmp_path):
    """Test run_escalate_only with successful escalation."""
    monkeypatch.setattr(
        "dgov.recovery.escalate_worker_pane",
        lambda *args, **kwargs: {"new_slug": "s2"},
    )
    result = run_escalate_only(
        "/repo",
        "task1",
        session_root=str(tmp_path),
        target_agent="qwen-35b",
    )
    assert isinstance(result, EscalateResult)
    assert result.slug == "task1"
    assert result.new_slug == "s2"
    assert result.target_agent == "qwen-35b"
    assert result.error is None


def test_run_escalate_only_error(monkeypatch, tmp_path):
    """Test run_escalate_only with error response."""
    monkeypatch.setattr(
        "dgov.recovery.escalate_worker_pane",
        lambda *args, **kwargs: {"error": "escalation failed"},
    )
    result = run_escalate_only(
        "/repo",
        "task1",
        session_root=str(tmp_path),
        target_agent="qwen-35b",
    )
    assert isinstance(result, EscalateResult)
    assert result.slug == "task1"
    assert result.new_slug is None
    assert result.target_agent == "qwen-35b"
    assert result.error == "escalation failed"


class TestResolveTouches:
    def test_explicit(self, tmp_path):
        result = resolve_touches(touches=["a.py", "b.py"])
        assert result == ["a.py", "b.py"]

    def test_dedupes(self, tmp_path):
        result = resolve_touches(touches=["a.py", "a.py"])
        assert result == ["a.py"]

    def test_from_prompt(self, monkeypatch):
        monkeypatch.setattr(
            "dgov.strategy.extract_task_context",
            lambda prompt: {
                "primary_files": ["src/merger.py", "src/driver.py"],
                "also_check": [],
                "tests": [],
                "hints": [],
            },
        )
        result = resolve_touches(prompt="fix merger")
        assert result == ["src/merger.py", "src/driver.py"]

    def test_empty(self, tmp_path):
        result = resolve_touches()
        assert result == []


# =============================================================================
# New Executor Syscalls Tests
# =============================================================================


@pytest.mark.unit
class TestNewSyscalls:
    """Tests for new executor syscalls that replace direct persistence calls."""

    def test_run_enqueue_merge(self, tmp_path, monkeypatch):
        """Test run_enqueue_merge creates ticket and emits event."""
        enqueued = []
        events = []

        def _enqueue(_sr, slug, req):
            enqueued.append((slug, req))
            return 42

        monkeypatch.setattr("dgov.persistence.enqueue_merge", _enqueue)
        monkeypatch.setattr("dgov.persistence.emit_event", lambda *a, **kw: events.append((a, kw)))
        from dgov.executor import run_enqueue_merge

        result = run_enqueue_merge(str(tmp_path), "test-slug", "governor")

        assert result["ticket"] == 42
        assert result["slug"] == "test-slug"
        assert result["requester"] == "governor"
        assert enqueued == [("test-slug", "governor")]
        assert len(events) == 1

    def test_run_process_merge_empty(self, tmp_path):
        """Test run_process_merge returns empty when no merges pending."""
        from dgov.executor import run_process_merge

        with patch("dgov.persistence.claim_next_merge", return_value=None):
            result = run_process_merge("/repo", str(tmp_path))

        assert result == {"status": "empty"}

    def test_run_process_merge_success(self, tmp_path, monkeypatch):
        """Test run_process_merge handles successful merge."""
        claimed = {"branch": "test-branch", "ticket": "TKT-123"}

        def _claim(_sr):
            return claimed

        def _land(*_args, **_kwargs):
            from dgov.merger import MergeSuccess

            return MagicMock(
                merge_result=MergeSuccess(merged="test-branch", branch="test-branch"),
                error=None,
            )

        monkeypatch.setattr("dgov.persistence.claim_next_merge", _claim)
        monkeypatch.setattr("dgov.executor.run_land_only", _land)

        complete_called = []
        emit_called = []

        def _complete(_sr, t, s, r):
            complete_called.append((t, s))

        monkeypatch.setattr("dgov.persistence.complete_merge", _complete)

        def _emit(*a, **kw):
            emit_called.append((a, kw))

        monkeypatch.setattr("dgov.persistence.emit_event", _emit)

        from dgov.executor import run_process_merge

        result = run_process_merge("/repo", str(tmp_path), resolve="skip", squash=True)

        assert result["ticket"] == "TKT-123"
        assert result["slug"] == "test-branch"
        assert result["success"] is True
        assert result["result"]["merged"] == "test-branch"
        assert complete_called[0] == ("TKT-123", True)
        assert len(emit_called) == 1

    def test_run_process_merge_error(self, tmp_path, monkeypatch):
        """Test run_process_merge handles merge failure."""
        claimed = {"branch": "test-branch", "ticket": "TKT-456"}

        def _claim(_sr):
            return claimed

        def _land(*_args, **_kwargs):

            return MagicMock(
                merge_result=MergeError(error="review failed", slug="test"),
                error="review failed",
            )

        monkeypatch.setattr("dgov.persistence.claim_next_merge", _claim)
        monkeypatch.setattr("dgov.executor.run_land_only", _land)

        complete_called = []

        def _complete(_sr, t, s, r):
            complete_called.append((t, s))

        monkeypatch.setattr("dgov.persistence.complete_merge", _complete)

        from dgov.executor import run_process_merge

        result = run_process_merge("/repo", str(tmp_path))

        assert result["ticket"] == "TKT-456"
        assert result["slug"] == "test-branch"
        assert result["success"] is False
        assert complete_called[0] == ("TKT-456", False)

    def test_run_resume_dag(self, tmp_path, monkeypatch):
        """Test run_resume_dag updates DAG run status."""
        updated = []
        events = []
        monkeypatch.setattr(
            "dgov.persistence.update_dag_run", lambda sr, rid, **kw: updated.append((rid, kw))
        )
        monkeypatch.setattr("dgov.persistence.emit_event", lambda *a, **kw: events.append((a, kw)))
        from dgov.executor import run_resume_dag

        run_resume_dag(str(tmp_path), 5)

        assert updated == [(5, {"status": "resumed"})]
        assert len(events) == 1

    def test_dag_interrupt_marks_run_blocked(self, tmp_path, monkeypatch):
        from dgov.executor import _dag_interrupt

        updated = []
        upserts = []
        events = []

        monkeypatch.setattr(
            "dgov.persistence.get_pane",
            lambda *args, **kwargs: {"role": "worker", "agent": "qwen-35b"},
        )
        monkeypatch.setattr("dgov.status.tail_worker_log", lambda *args, **kwargs: "tail")
        monkeypatch.setattr(
            "dgov.inspection.diff_worker_pane",
            lambda *args, **kwargs: {"diff": "diff-text"},
        )
        monkeypatch.setattr(
            "dgov.persistence.upsert_dag_task",
            lambda *args, **kwargs: upserts.append((args, kwargs)),
        )
        monkeypatch.setattr(
            "dgov.persistence.update_dag_run",
            lambda session_root, run_id, **kwargs: updated.append((run_id, kwargs)),
        )
        monkeypatch.setattr(
            "dgov.persistence.emit_event",
            lambda *args, **kwargs: events.append((args, kwargs)),
        )

        _dag_interrupt(
            "/repo",
            str(tmp_path),
            7,
            "task-1",
            "pane-1",
            "review_failed",
            lambda _message: None,
        )

        assert updated == [(7, {"status": "blocked"})]
        assert upserts[0][0][2:] == ("task-1", "blocked_on_governor", "qwen-35b")
        assert events[0][0][1] == "dag_blocked"

    def test_run_worker_checkpoint(self, tmp_path, monkeypatch):
        """Test run_worker_checkpoint sets metadata and emits event."""
        meta_calls = []
        events = []

        def _set_meta(_sr, slug, **kw):
            meta_calls.append((slug, kw))

        monkeypatch.setattr("dgov.persistence.set_pane_metadata", _set_meta)
        monkeypatch.setattr("dgov.persistence.emit_event", lambda *a, **kw: events.append((a, kw)))
        from dgov.executor import run_worker_checkpoint

        run_worker_checkpoint(str(tmp_path), "test-slug", "halfway done")

        assert meta_calls == [("test-slug", {"last_checkpoint": "halfway done"})]
        assert len(events) == 1


# =============================================================================
# Bug #185: Readonly phase timeout tests
# =============================================================================


class TestDagWaitAnyReadonlyTimeout:
    """Tests for Bug #185: Workers stuck in non-terminal phases should timeout."""

    @pytest.fixture(autouse=True)
    def _mock_wait_notify(self, monkeypatch):
        """Mock _wait_for_notify to prevent blocking in tests."""
        monkeypatch.setattr("dgov.persistence._wait_for_notify", lambda *a, **kw: None)

    def test_stuck_phase_triggers_timeout_after_readonly_threshold(self, tmp_path, monkeypatch):
        """Worker in STUCK phase for >readonly_timeout should return timed_out."""
        from dgov.executor import _dag_wait_any
        from dgov.kernel import TaskWaitDone, WorkerObservation, WorkerPhase

        call_count = [0]
        start_time = time.monotonic()

        def _observe_stuck(project_root, session_root, pane_slug):
            call_count[0] += 1
            # Always return STUCK to trigger readonly timeout
            return WorkerObservation(slug=pane_slug, phase=WorkerPhase.STUCK, alive=True)

        monkeypatch.setattr("dgov.monitor.observe_worker", _observe_stuck)

        result = _dag_wait_any(
            project_root="/repo",
            session_root=str(tmp_path),
            task_slugs=("task-1",),
            pane_map={"task-1": "pane-1"},
            stable_states={},
            task_timeouts={"task-1": 600},
            poll_interval=0.001,
            readonly_timeout=0.01,  # Short timeout for test
        )

        elapsed = time.monotonic() - start_time
        assert isinstance(result, TaskWaitDone)
        assert result.pane_state == "timed_out"
        assert result.task_slug == "task-1"
        assert result.pane_slug == "pane-1"
        assert elapsed < 0.5  # Should timeout quickly, not wait 600s

    def test_idle_phase_triggers_timeout_after_readonly_threshold(self, tmp_path, monkeypatch):
        """Worker in IDLE phase for >readonly_timeout should return timed_out."""
        from dgov.executor import _dag_wait_any
        from dgov.kernel import TaskWaitDone, WorkerObservation, WorkerPhase

        def _observe_idle(project_root, session_root, pane_slug):
            return WorkerObservation(slug=pane_slug, phase=WorkerPhase.IDLE, alive=True)

        monkeypatch.setattr("dgov.monitor.observe_worker", _observe_idle)

        result = _dag_wait_any(
            project_root="/repo",
            session_root=str(tmp_path),
            task_slugs=("task-1",),
            pane_map={"task-1": "pane-1"},
            stable_states={},
            task_timeouts={"task-1": 600},
            poll_interval=0.001,
            readonly_timeout=0.01,
        )

        assert isinstance(result, TaskWaitDone)
        assert result.pane_state == "timed_out"

    def test_waiting_input_phase_triggers_timeout(self, tmp_path, monkeypatch):
        """Worker in WAITING_INPUT phase for >readonly_timeout should return timed_out."""
        from dgov.executor import _dag_wait_any
        from dgov.kernel import TaskWaitDone, WorkerObservation, WorkerPhase

        def _observe_waiting_input(project_root, session_root, pane_slug):
            return WorkerObservation(slug=pane_slug, phase=WorkerPhase.WAITING_INPUT, alive=True)

        monkeypatch.setattr("dgov.monitor.observe_worker", _observe_waiting_input)

        result = _dag_wait_any(
            project_root="/repo",
            session_root=str(tmp_path),
            task_slugs=("task-1",),
            pane_map={"task-1": "pane-1"},
            stable_states={},
            task_timeouts={"task-1": 600},
            poll_interval=0.001,
            readonly_timeout=0.01,
        )

        assert isinstance(result, TaskWaitDone)
        assert result.pane_state == "timed_out"

    def test_done_phase_returns_immediately(self, tmp_path, monkeypatch):
        """Worker in DONE phase should return immediately with done state."""
        from dgov.executor import _dag_wait_any
        from dgov.kernel import TaskWaitDone, WorkerObservation, WorkerPhase

        def _observe_done(project_root, session_root, pane_slug):
            return WorkerObservation(slug=pane_slug, phase=WorkerPhase.DONE, alive=True)

        monkeypatch.setattr("dgov.monitor.observe_worker", _observe_done)

        result = _dag_wait_any(
            project_root="/repo",
            session_root=str(tmp_path),
            task_slugs=("task-1",),
            pane_map={"task-1": "pane-1"},
            stable_states={},
            task_timeouts={"task-1": 600},
            poll_interval=0.001,
            readonly_timeout=30.0,
        )

        assert isinstance(result, TaskWaitDone)
        assert result.pane_state == "done"

    def test_failed_phase_returns_immediately(self, tmp_path, monkeypatch):
        """Worker in FAILED phase should return immediately with failed state."""
        from dgov.executor import _dag_wait_any
        from dgov.kernel import TaskWaitDone, WorkerObservation, WorkerPhase

        def _observe_failed(project_root, session_root, pane_slug):
            return WorkerObservation(slug=pane_slug, phase=WorkerPhase.FAILED, alive=True)

        monkeypatch.setattr("dgov.monitor.observe_worker", _observe_failed)

        result = _dag_wait_any(
            project_root="/repo",
            session_root=str(tmp_path),
            task_slugs=("task-1",),
            pane_map={"task-1": "pane-1"},
            stable_states={},
            task_timeouts={"task-1": 600},
            poll_interval=0.001,
            readonly_timeout=30.0,
        )

        assert isinstance(result, TaskWaitDone)
        assert result.pane_state == "failed"

    def test_reset_readonly_timer_when_entering_working(self, tmp_path, monkeypatch):
        """Readonly timer should reset when worker transitions from STUCK to WORKING."""
        from dgov.executor import _dag_wait_any
        from dgov.kernel import TaskWaitDone, WorkerObservation, WorkerPhase

        phases = [WorkerPhase.STUCK, WorkerPhase.WORKING, WorkerPhase.DONE]
        call_count = [0]

        def _observe_transition(project_root, session_root, pane_slug):
            idx = min(call_count[0], len(phases) - 1)
            phase = phases[idx]
            call_count[0] += 1
            return WorkerObservation(slug=pane_slug, phase=phase, alive=True)

        monkeypatch.setattr("dgov.monitor.observe_worker", _observe_transition)

        result = _dag_wait_any(
            project_root="/repo",
            session_root=str(tmp_path),
            task_slugs=("task-1",),
            pane_map={"task-1": "pane-1"},
            stable_states={},
            task_timeouts={"task-1": 600},
            poll_interval=0.001,
            readonly_timeout=0.01,
        )

        # Should complete via DONE, not timeout
        assert isinstance(result, TaskWaitDone)
        assert result.pane_state == "done"

    def test_multiple_tasks_tracks_separate_readonly_timers(self, tmp_path, monkeypatch):
        """Each task should track its own readonly timer independently."""
        from dgov.executor import _dag_wait_any
        from dgov.kernel import TaskWaitDone, WorkerObservation, WorkerPhase

        call_count = [0]

        def _observe_mixed(project_root, session_root, pane_slug):
            call_count[0] += 1
            # task-1 is WORKING, task-2 is STUCK
            if pane_slug == "pane-1":
                return WorkerObservation(slug=pane_slug, phase=WorkerPhase.WORKING, alive=True)
            return WorkerObservation(slug=pane_slug, phase=WorkerPhase.STUCK, alive=True)

        monkeypatch.setattr("dgov.monitor.observe_worker", _observe_mixed)

        result = _dag_wait_any(
            project_root="/repo",
            session_root=str(tmp_path),
            task_slugs=("task-1", "task-2"),
            pane_map={"task-1": "pane-1", "task-2": "pane-2"},
            stable_states={},
            task_timeouts={"task-1": 600, "task-2": 600},
            poll_interval=0.001,
            readonly_timeout=0.01,
        )

        # task-2 should timeout first (STUCK), not task-1 (WORKING)
        assert isinstance(result, TaskWaitDone)
        assert result.pane_state == "timed_out"
        assert result.task_slug == "task-2"
        assert result.pane_slug == "pane-2"
