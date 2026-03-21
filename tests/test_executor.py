from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import dgov.lifecycle as _lifecycle  # noqa: F401 - bind real persistence symbols before patches
from dgov.executor import (
    CleanupOnlyResult,
    PostDispatchActionExecutor,
    derive_prompt_touches,
    review_merge_gate,
    run_cleanup_only,
    run_dispatch_preflight,
    run_land_only,
    run_post_dispatch_lifecycle,
    run_review_merge,
    run_review_only,
    run_wait_only,
)

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

    assert derive_prompt_touches("fix it") == ["src/a.py", "src/b.py", "tests/test_a.py"]


def test_run_dispatch_preflight_prefers_explicit_touches():
    fake_report = MagicMock()

    with patch("dgov.preflight.run_preflight", return_value=fake_report) as mock_preflight:
        result = run_dispatch_preflight(
            "/repo",
            "claude",
            prompt="fix src/a.py",
            touches=["src/exact.py", "tests/test_exact.py"],
            session_root="/session",
        )

    assert result is fake_report
    mock_preflight.assert_called_once_with(
        project_root="/repo",
        agent="claude",
        touches=["src/exact.py", "tests/test_exact.py"],
        expected_branch=None,
        session_root="/session",
        skip_deps=True,
    )


def test_review_merge_gate_blocks_zero_commit():
    with patch(
        "dgov.inspection.review_worker_pane",
        return_value={"slug": "task", "verdict": "safe", "commit_count": 0},
    ):
        gate = review_merge_gate("/repo", "task", session_root="/session")

    assert gate.passed is False
    assert gate.error == "No commits to merge"


def test_review_merge_gate_blocks_non_safe_verdict():
    with patch(
        "dgov.inspection.review_worker_pane",
        return_value={"slug": "task", "verdict": "review", "commit_count": 2},
    ):
        gate = review_merge_gate("/repo", "task", session_root="/session")

    assert gate.passed is False
    assert gate.error == "Review verdict is review; refusing to merge"


def test_run_review_only_returns_typed_review_result():
    with patch(
        "dgov.inspection.review_worker_pane",
        return_value={"slug": "task", "verdict": "safe", "commit_count": 2},
    ):
        result = run_review_only("/repo", "task", session_root="/session")

    assert result.slug == "task"
    assert result.passed is True
    assert result.verdict == "safe"
    assert result.commit_count == 2
    assert result.error is None
    assert result.review_record is not None
    assert result.review_record.provider_id == "inspection-review"
    assert result.review_record.decision.commit_count == 2


def test_run_review_only_persists_decision_journal(tmp_path):
    from dgov.persistence import read_decision_journal

    session_root = str(tmp_path)

    with patch(
        "dgov.inspection.review_worker_pane",
        return_value={"slug": "task", "verdict": "safe", "commit_count": 2},
    ):
        result = run_review_only("/repo", "task", session_root=session_root)

    journal = read_decision_journal(session_root)
    assert result.passed is True
    assert len(journal) == 1
    assert journal[0]["kind"] == "review_output"
    assert journal[0]["provider_id"] == "inspection-review"
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
            return_value={"slug": "task", "verdict": "safe", "commit_count": 2},
        ),
        patch("dgov.persistence.get_pane", return_value=pane_data),
        patch(
            "dgov.kernel.build_manifest_on_completion",
            return_value=MagicMock(
                base_sha="abc123",
                file_claims=("src/foo.py",),
                paths_written=("src/foo.py",),
                claim_violations=(),
            ),
        ),
        patch(
            "dgov.kernel.validate_manifest_freshness",
            return_value=(False, ["src/foo.py"]),
        ),
    ):
        result = run_review_only("/repo", "task", session_root=session_root)

    # Stale files are recorded in review dict and block merge
    assert "stale_files" in result.review
    assert result.review["stale_files"] == ["src/foo.py"]
    assert result.passed is False
    assert result.error is not None
    assert "stale" in result.error.lower()


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
            return_value={"slug": "task", "verdict": "safe", "commit_count": 2},
        ),
        patch("dgov.persistence.get_pane", return_value=pane_data),
        patch(
            "dgov.kernel.build_manifest_on_completion",
            return_value=MagicMock(
                base_sha="abc123",
                file_claims=("src/foo.py",),
                paths_written=("src/foo.py",),
                claim_violations=(),
            ),
        ),
        patch(
            "dgov.kernel.validate_manifest_freshness",
            return_value=(True, []),
        ),
    ):
        result = run_review_only("/repo", "task", session_root=session_root)

    # No stale files in review dict when fresh
    assert "stale_files" not in result.review
    assert result.passed is True
    assert result.error is None


def test_run_wait_only_returns_worker_failed_state():
    with (
        patch(
            "dgov.waiter.wait_worker_pane",
            return_value={"done": "task", "method": "signal"},
        ),
        patch("dgov.persistence.get_pane", return_value={"state": "failed"}),
    ):
        result = run_wait_only("/repo", "task", session_root="/session", max_retries=0)

    assert result.state == "failed"
    assert result.slug == "task"
    assert result.failure_stage == "worker_failed"


def test_run_wait_only_returns_timeout_when_retries_exhausted():
    from dgov.waiter import PaneTimeoutError

    with patch(
        "dgov.waiter.wait_worker_pane",
        side_effect=PaneTimeoutError("task", 30, "claude"),
    ):
        result = run_wait_only("/repo", "task", session_root="/session", timeout=30, max_retries=0)

    assert result.state == "failed"
    assert result.failure_stage == "timeout"
    assert result.error == "Worker timed out after 30s (retries exhausted)"


def test_run_cleanup_only_preserves_inspectable_outcomes():
    with patch("dgov.persistence.mark_preserved_artifacts") as mock_mark:
        result = run_cleanup_only(
            "/repo",
            "task",
            session_root="/session",
            state="review_pending",
        )

    assert result == CleanupOnlyResult(
        slug="task",
        action="preserve",
        reason="review_pending",
    )
    mock_mark.assert_called_once_with(
        "/session",
        "task",
        reason="review_pending",
        recoverable=False,
        state="review_pending",
        failure_stage=None,
    )


def test_run_cleanup_only_force_closes_worker_failed():
    with patch(
        "dgov.lifecycle.close_worker_pane",
        return_value=True,
    ) as mock_close:
        result = run_cleanup_only(
            "/repo",
            "task",
            session_root="/session",
            state="failed",
            failure_stage="worker_failed",
        )

    assert result.action == "close"
    assert result.closed is True
    assert result.force is True
    mock_close.assert_called_once_with(
        "/repo",
        "task",
        session_root="/session",
        force=True,
    )


def test_post_dispatch_action_executor_executes_review_action(tmp_path):
    phases: list[tuple[str, str]] = []

    with patch(
        "dgov.inspection.review_worker_pane",
        return_value={"slug": "task", "verdict": "safe", "commit_count": 2},
    ):
        runtime = PostDispatchActionExecutor(
            project_root="/repo",
            session_root=str(tmp_path),
            phase_callback=lambda phase, slug: phases.append((phase, slug)),
        )
        from dgov.kernel import ReviewPane

        event = runtime.execute(ReviewPane("task"))

    assert runtime.review is not None
    assert runtime.review.slug == "task"
    assert phases == [("reviewing", "task")]
    assert event.result.slug == "task"


class TestPostDispatchLifecycle:
    def test_completed_lifecycle_merges_after_safe_review(self):
        phases: list[tuple[str, str]] = []

        with (
            patch(
                "dgov.waiter.wait_worker_pane", return_value={"done": "task", "method": "signal"}
            ),
            patch("dgov.persistence.get_pane", return_value={"slug": "task", "state": "done"}),
            patch(
                "dgov.inspection.review_worker_pane",
                return_value={"slug": "task", "verdict": "safe", "commit_count": 1},
            ),
            patch(
                "dgov.merger.merge_worker_pane",
                return_value={"merged": "task", "branch": "task"},
            ),
        ):
            result = run_post_dispatch_lifecycle(
                "/repo",
                "task",
                session_root="/session",
                phase_callback=lambda phase, slug: phases.append((phase, slug)),
            )

        assert result.state == "completed"
        assert result.slug == "task"
        assert result.merge_result == {"merged": "task", "branch": "task"}
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

    def test_reviewed_pass_preserves_artifacts_when_auto_merge_disabled(self):
        phases: list[tuple[str, str]] = []

        with (
            patch(
                "dgov.waiter.wait_worker_pane", return_value={"done": "task", "method": "signal"}
            ),
            patch("dgov.persistence.get_pane", return_value={"slug": "task", "state": "done"}),
            patch(
                "dgov.inspection.review_worker_pane",
                return_value={"slug": "task", "verdict": "safe", "commit_count": 1},
            ),
            patch("dgov.merger.merge_worker_pane") as mock_merge,
        ):
            result = run_post_dispatch_lifecycle(
                "/repo",
                "task",
                session_root="/session",
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

    def test_timeout_retry_restarts_wait_on_new_slug(self):
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
                return_value={"slug": "task-2", "verdict": "safe", "commit_count": 1},
            ),
            patch(
                "dgov.merger.merge_worker_pane",
                return_value={"merged": "task-2", "branch": "task-2"},
            ),
        ):
            result = run_post_dispatch_lifecycle(
                "/repo",
                "task",
                session_root="/session",
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
            session_root="/session",
            agent="claude",
        )
        assert phases == [
            ("waiting", "task"),
            ("waiting", "task-2"),
            ("reviewing", "task-2"),
            ("merging", "task-2"),
            ("completed", "task-2"),
        ]

    def test_review_pending_returns_without_merge(self):
        phases: list[tuple[str, str]] = []

        with (
            patch(
                "dgov.waiter.wait_worker_pane", return_value={"done": "task", "method": "signal"}
            ),
            patch("dgov.persistence.get_pane", return_value={"slug": "task", "state": "done"}),
            patch(
                "dgov.inspection.review_worker_pane",
                return_value={"slug": "task", "verdict": "review", "commit_count": 1},
            ),
            patch("dgov.merger.merge_worker_pane") as mock_merge,
        ):
            result = run_post_dispatch_lifecycle(
                "/repo",
                "task",
                session_root="/session",
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

    def test_worker_failed_lifecycle_closes_forcefully(self):
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
                session_root="/session",
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
            session_root="/session",
            force=True,
        )


class TestReviewMerge:
    def test_run_review_merge_blocks_non_safe_review(self):
        with patch(
            "dgov.inspection.review_worker_pane",
            return_value={"slug": "task", "verdict": "review", "commit_count": 1},
        ):
            result = run_review_merge("/repo", "task", session_root="/session")

        assert result.slug == "task"
        assert result.error == "Review verdict is review; refusing to merge"
        assert result.merge_result is None

    def test_run_review_merge_returns_merge_result(self):
        with (
            patch(
                "dgov.inspection.review_worker_pane",
                return_value={"slug": "task", "verdict": "safe", "commit_count": 2},
            ),
            patch(
                "dgov.executor.run_merge_only",
                return_value=MagicMock(
                    error=None,
                    merge_result={"merged": "task", "branch": "task"},
                ),
            ) as mock_merge,
        ):
            result = run_review_merge(
                "/repo",
                "task",
                session_root="/session",
                resolve="agent",
                squash=False,
                rebase=False,
            )

        assert result.error is None
        assert result.merge_result == {"merged": "task", "branch": "task"}
        mock_merge.assert_called_once_with(
            "/repo",
            "task",
            session_root="/session",
            resolve="agent",
            squash=False,
            rebase=False,
        )

    def test_run_land_only_closes_after_successful_merge(self):
        with (
            patch(
                "dgov.executor.run_review_merge",
                return_value=MagicMock(
                    slug="task",
                    review={"slug": "task", "verdict": "safe", "commit_count": 2},
                    review_record=None,
                    merge_result={"merged": "task", "branch": "task"},
                    failure_stage=None,
                    error=None,
                ),
            ),
            patch("dgov.lifecycle.close_worker_pane", return_value=True) as mock_close,
        ):
            result = run_land_only("/repo", "task", session_root="/session")

        assert result.error is None
        assert result.merge_result == {"merged": "task", "branch": "task"}
        assert result.cleanup == CleanupOnlyResult(
            slug="task",
            action="close",
            reason="landed",
            closed=True,
            force=False,
        )
        mock_close.assert_called_once_with("/repo", "task", session_root="/session")
