from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dgov.executor import derive_prompt_touches, review_merge_gate, run_dispatch_preflight

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
