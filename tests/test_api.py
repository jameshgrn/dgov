"""Unit tests for the public dgov API."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dgov.api import Orchestrator
from dgov.inspection import ReviewInfo

pytestmark = pytest.mark.unit


def test_orchestrator_review_stays_read_only(tmp_path) -> None:
    orchestrator = Orchestrator("/repo", session_root=str(tmp_path))

    with patch(
        "dgov.executor.run_review_only",
        return_value=MagicMock(
            slug="task",
            verdict="safe",
            commit_count=2,
            error=None,
            review=ReviewInfo(slug="task", verdict="safe", commit_count=2, files_changed=1),
        ),
    ) as mock_review:
        result = orchestrator.review("task")

    assert result.slug == "task"
    assert result.verdict == "safe"
    assert result.commit_count == 2
    assert result.files_changed == 1
    assert mock_review.call_args.kwargs["emit_events"] is False
