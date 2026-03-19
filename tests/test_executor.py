from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.executor import (
    ExecutorLifecycle,
    derive_prompt_touches,
    review_merge_gate,
    run_dispatch_preflight,
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


# -- ExecutorLifecycle tests --


class TestExecutorLifecycle:
    """Unit tests for ExecutorLifecycle in dgov.executor.

    Covers:
    - Successful completion
    - Review pending on non-safe verdict
    - Failed pane handling
    - Retry on timeout preserving original agent
    - Escalation on timeout preserving caller permission mode
    """

    def test_executor_lifecycle_successful_completion(self, tmp_path):
        """Successful pane completion transitions to done state."""
        session_root = str(tmp_path / "session")
        (Path(session_root) / ".dgov").mkdir(parents=True)

        slug = "test-success"
        fake_pane_record = {
            "slug": slug,
            "state": "running",
            "agent": "qwen-9b",
            "pane_id": "fake-pane-id",
        }

        with patch("dgov.persistence.get_pane", return_value=fake_pane_record):
            with patch("dgov.persistence.update_pane_state") as mock_update:
                with patch("dgov.persistence.emit_event") as mock_emit:
                    lifecycle = ExecutorLifecycle(session_root)
                    result = lifecycle.handle_successful_completion(slug)

        assert result["slug"] == slug
        assert result["method"] == "signal_or_commit"
        assert result["error"] is None
        mock_update.assert_called_once_with(session_root, slug, "done")
        mock_emit.assert_called_once_with(session_root, "pane_done", slug)

    def test_executor_lifecycle_review_pending_non_safe_verdict(self, tmp_path):
        """Non-safe verdict transitions to review_pending state."""
        session_root = str(tmp_path / "session")
        (Path(session_root) / ".dgov").mkdir(parents=True)

        slug = "test-review"
        fake_pane_record = {
            "slug": slug,
            "state": "running",
            "agent": "qwen-9b",
            "pane_id": "fake-pane-id",
        }

        def mock_review(*args, **kwargs):
            return {
                "slug": slug,
                "verdict": "review",
                "commit_count": 2,
                "issues": ["protected file"],
            }

        with patch("dgov.persistence.get_pane", return_value=fake_pane_record):
            with patch(
                "dgov.inspection.review_worker_pane",
                side_effect=mock_review,
            ):
                with patch("dgov.persistence.update_pane_state") as mock_update:
                    with patch("dgov.persistence.emit_event") as mock_emit:
                        lifecycle = ExecutorLifecycle(session_root)
                        result = lifecycle.handle_review_pending(slug)

        assert result["slug"] == slug
        assert result["verdict"] == "review"
        assert result["issues"] == ["protected file"]
        mock_update.assert_called_once_with(session_root, slug, "review_pending")
        mock_emit.assert_called_once()

    def test_executor_lifecycle_failed_pane_handling(self, tmp_path):
        """Failed pane triggers auto-retry via maybe_auto_retry."""
        session_root = str(tmp_path / "session")
        project_root = str(tmp_path / "project")
        (Path(session_root) / ".dgov").mkdir(parents=True)

        slug = "test-failed"
        fake_pane_record = {
            "slug": slug,
            "state": "failed",
            "agent": "qwen-9b",
            "pane_id": "fake-pane-id",
            "prompt": "fix the parser",
        }

        mock_retry_result = {
            "retried": True,
            "original_slug": slug,
            "new_slug": "test-failed-2",
            "agent": "qwen-9b",
            "attempt": 2,
        }

        with patch("dgov.persistence.get_pane", return_value=fake_pane_record):
            with patch(
                "dgov.recovery.maybe_auto_retry",
                return_value=mock_retry_result,
            ):
                lifecycle = ExecutorLifecycle(session_root)
                result = lifecycle.handle_failed_pane(slug, project_root=project_root)

        assert result["slug"] == slug
        assert result["action"] == "retry"
        assert result["new_slug"] == "test-failed-2"
        assert result["attempt"] == 2
        assert result["from_agent"] == "qwen-9b"
        assert result["to_agent"] == "qwen-9b"

    def test_executor_lifecycle_timeout_retry_preserves_agent(self, tmp_path):
        """Timeout triggers retry with the same agent preserved."""
        session_root = str(tmp_path / "session")
        project_root = str(tmp_path / "project")
        (Path(session_root) / ".dgov").mkdir(parents=True)

        slug = "test-timeout"
        agent = "qwen-9b"
        fake_pane_record = {
            "slug": slug,
            "state": "failed",
            "agent": agent,
            "pane_id": "fake-pane-id",
            "prompt": "fix the parser",
        }

        mock_retry_result = {
            "retried": True,
            "original_slug": slug,
            "new_slug": f"{slug}-2",
            "agent": agent,
            "attempt": 2,
        }

        with patch("dgov.persistence.get_pane", return_value=fake_pane_record):
            with patch(
                "dgov.recovery.maybe_auto_retry",
                return_value=mock_retry_result,
            ):
                lifecycle = ExecutorLifecycle(session_root)
                result = lifecycle.handle_timeout(slug, project_root=project_root)

        # Should have retried with same agent
        assert result["action"] == "retry"
        assert result["from_agent"] == agent
        assert result["to_agent"] == agent
        assert result["new_slug"] == f"{slug}-2"
        assert result["attempt"] == 2

    def test_executor_lifecycle_timeout_escalation_preserves_permission_context(self, tmp_path):
        """Timeout after retries exhausted triggers escalation."""
        session_root = str(tmp_path / "session")
        project_root = str(tmp_path / "project")
        (Path(session_root) / ".dgov").mkdir(parents=True)

        slug = "test-escalate"
        agent = "qwen-4b"
        fake_pane_record = {
            "slug": slug,
            "state": "failed",
            "agent": agent,
            "pane_id": "fake-pane-id",
            "prompt": "fix the parser",
            "max_retries": 0,
        }

        mock_escalate_result = {
            "escalated": slug,
            "to": "qwen-9b",
            "new_slug": f"{slug}-esc-1",
        }

        with patch("dgov.persistence.get_pane", return_value=fake_pane_record):
            with patch(
                "dgov.recovery.maybe_auto_retry",
                return_value=mock_escalate_result,
            ):
                lifecycle = ExecutorLifecycle(session_root)
                result = lifecycle.handle_timeout(slug, project_root=project_root)

        # Should have escalated
        assert result["action"] == "escalate"
        assert result["from_agent"] == agent
        assert result["to_agent"] == "qwen-9b"
        assert result["new_slug"] == f"{slug}-esc-1"
