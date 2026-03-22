"""Unit tests for decision providers in src/dgov/decision_providers.py.

Tests providers NOT covered in test_decision.py:
- DeterministicClassificationProvider (done/stuck pattern matching, ambiguous error)
- StatisticalRoutingProvider (highest pass rate selection, insufficient data error, pane fallback)
- InspectionReviewProvider (verdict handling, missing project_root/slug error)
"""

from __future__ import annotations

import pytest

from dgov.decision import (
    MonitorOutputRequest,
    ProviderError,
    ReviewOutputDecision,
    ReviewOutputRequest,
    RouteTaskRequest,
)
from dgov.decision_providers import (
    DeterministicClassificationProvider,
    InspectionReviewProvider,
    StatisticalRoutingProvider,
)

pytestmark = pytest.mark.unit


# -- DeterministicClassificationProvider tests --


def test_deterministic_classification_provider_handles_done_patterns():
    """DeterministicClassificationProvider returns 'done' for completion patterns."""
    provider = DeterministicClassificationProvider()

    # "task complete" pattern match
    result = provider.classify_output(MonitorOutputRequest(output="Task complete"))
    assert result.decision.classification == "done"
    assert result.provider_id == "deterministic-classifier"

    # Alternative "done" pattern
    result = provider.classify_output(MonitorOutputRequest(output="All done here"))
    assert result.decision.classification == "done"

    # "finished" pattern match
    result = provider.classify_output(MonitorOutputRequest(output="I've finished implementing"))
    assert result.decision.classification == "done"


def test_deterministic_classification_provider_handles_stuck_patterns():
    """DeterministicClassificationProvider returns 'stuck' for error patterns."""
    provider = DeterministicClassificationProvider()

    # Generic error pattern
    result = provider.classify_output(MonitorOutputRequest(output="Error: ConnectionRefusedError"))
    assert result.decision.classification == "stuck"

    # Exception/traceback pattern
    output = "Traceback (most recent call last):\n  ..."
    result = provider.classify_output(MonitorOutputRequest(output=output))
    assert result.decision.classification == "stuck"

    # Crash/panic/fatal keywords
    result = provider.classify_output(MonitorOutputRequest(output="Fatal error occurred"))
    assert result.decision.classification == "stuck"


def test_deterministic_classification_provider_raises_on_ambiguous():
    """DeterministicClassificationProvider raises ProviderError for ambiguous output."""
    provider = DeterministicClassificationProvider()

    # Output matching no deterministic pattern should raise ProviderError
    with pytest.raises(ProviderError, match="No deterministic pattern matched"):
        provider.classify_output(MonitorOutputRequest(output="Working on this now"))


def test_deterministic_classification_provider_handles_committing_pattern():
    """DeterministicClassificationProvider returns 'committing' for git patterns."""
    provider = DeterministicClassificationProvider()

    result = provider.classify_output(MonitorOutputRequest(output="Running git add and commit"))
    assert result.decision.classification == "committing"


def test_deterministic_classification_provider_handles_waiting_input_pattern():
    """DeterministicClassificationProvider returns 'waiting_input' for input-waiting patterns."""
    provider = DeterministicClassificationProvider()

    result = provider.classify_output(MonitorOutputRequest(output="Waiting for user confirmation"))
    assert result.decision.classification == "waiting_input"


def test_deterministic_classification_provider_handles_idle_pattern():
    """DeterministicClassificationProvider returns 'idle' for idle patterns."""
    provider = DeterministicClassificationProvider()

    result = provider.classify_output(MonitorOutputRequest(output="No work detected, pausing"))
    assert result.decision.classification == "idle"


# -- StatisticalRoutingProvider tests --


def _insert_span(conn, trace_id, kind, agent="", outcome="success", verdict="", **kw):
    """Helper to insert a span row for testing."""
    conn.execute(
        "INSERT INTO spans (trace_id, span_kind, started_at, ended_at, "
        "duration_ms, outcome, agent, verdict) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trace_id,
            kind,
            "2024-01-01T00:00:00+00:00",
            "2024-01-01T00:01:00+00:00",
            60000,
            outcome,
            agent,
            verdict,
        ),
    )


def test_statistical_routing_picks_agent_with_highest_pass_rate(tmp_path):
    """StatisticalRoutingProvider picks agent with highest pass rate from spans."""
    from dgov.persistence import _get_db

    session_root = str(tmp_path)
    conn = _get_db(session_root)

    # Agent "pi": 5 dispatches, 4 safe reviews out of 5 (80%)
    for i in range(5):
        _insert_span(conn, f"pi-task-{i}", "dispatch", agent="pi")
    for i in range(4):
        _insert_span(conn, f"pi-task-{i}", "review", agent="pi", verdict="safe")
    _insert_span(conn, "pi-task-4", "review", agent="pi", verdict="stuck")

    # Agent "claude": 5 dispatches, 5 safe reviews (100%)
    for i in range(5):
        _insert_span(conn, f"claude-task-{i}", "dispatch", agent="claude")
        _insert_span(conn, f"claude-task-{i}", "review", agent="claude", verdict="safe")

    conn.commit()

    provider = StatisticalRoutingProvider(session_root=session_root, min_samples=5)
    result = provider.route_task(
        RouteTaskRequest(prompt="debug issue", installed_agents=("pi", "claude"))
    )

    assert result.decision.agent == "claude"
    assert "statistical:" in result.decision.reason


def test_statistical_routing_raises_on_insufficient_samples(tmp_path):
    """StatisticalRoutingProvider raises ProviderError when < min_samples dispatches."""
    from dgov.persistence import _get_db

    session_root = str(tmp_path)
    conn = _get_db(session_root)

    # Only 3 dispatches (below min_samples=5)
    for i in range(3):
        _insert_span(conn, f"task-{i}", "dispatch", agent="pi")
        _insert_span(conn, f"task-{i}", "review", agent="pi", verdict="safe")
    conn.commit()

    provider = StatisticalRoutingProvider(session_root=session_root, min_samples=5)
    with pytest.raises(ProviderError, match="insufficient span data"):
        provider.route_task(RouteTaskRequest(prompt="test", installed_agents=("pi",)))


def test_statistical_routing_multiple_agents_different_rates(tmp_path):
    """StatisticalRoutingProvider correctly ranks agents with different pass rates."""
    from dgov.persistence import _get_db

    session_root = str(tmp_path)
    conn = _get_db(session_root)

    # Agent "fast": 5 dispatches, 3 safe (60%)
    for i in range(5):
        _insert_span(conn, f"fast-{i}", "dispatch", agent="fast")
    for i in range(3):
        _insert_span(conn, f"fast-{i}", "review", agent="fast", verdict="safe")
    for i in range(3, 5):
        _insert_span(conn, f"fast-{i}", "review", agent="fast", verdict="reject")

    # Agent "slow": 5 dispatches, 5 safe (100%)
    for i in range(5):
        _insert_span(conn, f"slow-{i}", "dispatch", agent="slow")
        _insert_span(conn, f"slow-{i}", "review", agent="slow", verdict="safe")
    conn.commit()

    provider = StatisticalRoutingProvider(session_root=session_root, min_samples=5)
    result = provider.route_task(
        RouteTaskRequest(prompt="test", installed_agents=("fast", "slow"))
    )
    assert result.decision.agent == "slow"


def test_statistical_routing_handles_empty_spans(tmp_path):
    """StatisticalRoutingProvider raises when no spans exist."""
    from dgov.persistence import _get_db

    _get_db(str(tmp_path))  # ensure schema exists

    provider = StatisticalRoutingProvider(session_root=str(tmp_path), min_samples=1)
    with pytest.raises(ProviderError, match="insufficient span data"):
        provider.route_task(RouteTaskRequest(prompt="test", installed_agents=("pi",)))


def test_statistical_routing_ignores_non_dispatch_for_counting(tmp_path):
    """StatisticalRoutingProvider counts dispatches, not other span kinds."""
    from dgov.persistence import _get_db

    session_root = str(tmp_path)
    conn = _get_db(session_root)

    # Only review spans (no dispatches) — should not qualify
    for i in range(5):
        _insert_span(conn, f"task-{i}", "review", agent="pi", verdict="safe")
    conn.commit()

    provider = StatisticalRoutingProvider(session_root=session_root, min_samples=1)
    with pytest.raises(ProviderError, match="insufficient span data"):
        provider.route_task(RouteTaskRequest(prompt="test", installed_agents=("pi",)))


def test_statistical_routing_handles_no_agent_spans(tmp_path):
    """StatisticalRoutingProvider skips spans with empty agent field."""
    from dgov.persistence import _get_db

    session_root = str(tmp_path)
    conn = _get_db(session_root)

    # Dispatch spans with empty agent
    for i in range(5):
        _insert_span(conn, f"task-{i}", "dispatch", agent="")
    conn.commit()

    provider = StatisticalRoutingProvider(session_root=session_root, min_samples=1)
    with pytest.raises(ProviderError, match="insufficient span data"):
        provider.route_task(RouteTaskRequest(prompt="test", installed_agents=("pi",)))


# -- InspectionReviewProvider tests --


def test_inspection_review_provider_returns_correct_verdict(tmp_path):
    """InspectionReviewProvider returns typed ReviewOutputDecision with correct verdict."""

    provider = InspectionReviewProvider()

    # Mock the review_worker_pane call with a "safe" verdict
    mock_review = {
        "slug": "task-1",
        "verdict": "safe",
        "commit_count": 3,
        "issues": [],
        "error": None,
    }

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr("dgov.inspection.review_worker_pane", lambda *args, **kwargs: mock_review)

        result = provider.review_output(
            ReviewOutputRequest(
                project_root=str(tmp_path),
                slug="task-1",
            )
        )

    assert result.provider_id == "inspection-review"
    assert isinstance(result.decision, ReviewOutputDecision)
    assert result.decision.verdict == "safe"
    assert result.decision.commit_count == 3
    assert result.decision.issues == ()
    assert result.decision.reason is None
    assert result.artifact == mock_review


def test_inspection_review_provider_handles_unsafe_verdict():
    """InspectionReviewProvider correctly handles non-safe verdicts."""
    from unittest.mock import patch

    provider = InspectionReviewProvider()

    # Mock with "fail" verdict and issues
    mock_review = {
        "slug": "task-2",
        "verdict": "fail",
        "commit_count": 0,
        "issues": ["protected files touched", "test failures"],
        "error": "tests failed",
    }

    with patch("dgov.inspection.review_worker_pane", return_value=mock_review):
        result = provider.review_output(
            ReviewOutputRequest(
                project_root="/tmp/test",
                slug="task-2",
            )
        )

    assert result.decision.verdict == "fail"
    assert result.decision.commit_count == 0
    assert result.decision.issues == ("protected files touched", "test failures")
    assert result.decision.reason == "tests failed"


def test_inspection_review_provider_requires_project_root(tmp_path):
    """InspectionReviewProvider raises ProviderError when project_root is missing."""
    provider = InspectionReviewProvider()

    with pytest.raises(ProviderError, match="project_root and slug"):
        provider.review_output(
            ReviewOutputRequest(
                project_root=None,  # type: ignore[arg-type]
                slug="task-1",
            )
        )


def test_inspection_review_provider_requires_slug(tmp_path):
    """InspectionReviewProvider raises ProviderError when slug is missing."""
    provider = InspectionReviewProvider()

    with pytest.raises(ProviderError, match="project_root and slug"):
        provider.review_output(
            ReviewOutputRequest(
                project_root=str(tmp_path),
                slug=None,  # type: ignore[arg-type]
            )
        )


def test_inspection_review_provider_handles_unknown_verdict():
    """InspectionReviewProvider handles missing or unknown verdict gracefully."""
    from unittest.mock import patch

    provider = InspectionReviewProvider()

    # Mock with missing verdict
    mock_review = {
        "slug": "task-3",
        "verdict": None,  # type: ignore[dict-item]
        "commit_count": None,  # type: ignore[dict-item]
        "issues": None,  # type: ignore[dict-item]
    }

    with patch("dgov.inspection.review_worker_pane", return_value=mock_review):
        result = provider.review_output(
            ReviewOutputRequest(
                project_root="/tmp/test",
                slug="task-3",
            )
        )

    # None gets converted to string "None" by str() call in provider
    assert result.decision.verdict == "None"
    assert result.decision.commit_count == 0


def test_inspection_review_provider_handles_empty_issues_list():
    """InspectionReviewProvider handles empty issues list correctly."""
    from unittest.mock import patch

    provider = InspectionReviewProvider()

    mock_review = {
        "slug": "task-4",
        "verdict": "safe",
        "commit_count": 1,
        "issues": [],
        "error": None,
    }

    with patch("dgov.inspection.review_worker_pane", return_value=mock_review):
        result = provider.review_output(
            ReviewOutputRequest(
                project_root="/tmp/test",
                slug="task-4",
            )
        )

    assert result.decision.issues == ()


def test_inspection_review_provider_passes_extra_kwargs_to_review(tmp_path):
    """InspectionReviewProvider passes session_root and full kwargs to review_worker_pane."""
    from unittest.mock import patch

    provider = InspectionReviewProvider()

    mock_review = {"slug": "task-5", "verdict": "safe"}

    with patch("dgov.inspection.review_worker_pane", return_value=mock_review) as mock_func:
        provider.review_output(
            ReviewOutputRequest(
                project_root=str(tmp_path),
                slug="task-5",
                session_root="/session",
                full=True,
            )
        )

    # Verify review_worker_pane was called with correct kwargs
    mock_func.assert_called_once_with(
        str(tmp_path),
        "task-5",
        session_root="/session",
        full=True,
    )


# -- ModelReviewProvider tests --


class TestModelReviewProvider:
    """Tests for ModelReviewProvider and helpers."""

    def test_parse_review_response_approved(self):
        """Parse an approved response."""
        from dgov.decision_providers import _parse_review_response

        content = "VERDICT: approved\nSUMMARY: Looks good\nISSUES: none"
        verdict, issues, summary = _parse_review_response(content)
        assert verdict == "safe"
        assert issues == ()
        assert summary == "Looks good"

    def test_parse_review_response_concerns(self):
        """Parse a response with concerns."""
        from dgov.decision_providers import _parse_review_response

        content = "VERDICT: concerns\nSUMMARY: Missing edge case\nISSUES: No null check on line 42"
        verdict, issues, summary = _parse_review_response(content)
        assert verdict == "concerns"
        assert len(issues) == 1
        assert "null check" in issues[0]
        assert summary == "Missing edge case"

    def test_parse_review_response_changes_requested(self):
        """Parse a changes_requested verdict."""
        from dgov.decision_providers import _parse_review_response

        content = "VERDICT: changes_requested\nSUMMARY: Needs rework\nISSUES: Bad design"
        verdict, issues, summary = _parse_review_response(content)
        assert verdict == "concerns"  # "change" triggers concerns

    def test_parse_review_response_multiple_issues(self):
        """Parse response with multiple issues."""
        from dgov.decision_providers import _parse_review_response

        content = (
            "VERDICT: concerns\n"
            "SUMMARY: Several problems\n"
            "ISSUES: Missing error handling\n"
            "Race condition in cleanup\n"
            "Unbounded loop on line 99"
        )
        verdict, issues, summary = _parse_review_response(content)
        assert verdict == "concerns"
        assert len(issues) == 3

    def test_parse_review_response_empty(self):
        """Parse empty response defaults to approved."""
        from dgov.decision_providers import _parse_review_response

        verdict, issues, summary = _parse_review_response("")
        assert verdict == "approved"
        assert issues == ()

    def test_resolve_review_model_known(self):
        """Known logical names resolve to OpenRouter model IDs."""
        from dgov.decision_providers import _resolve_review_model

        assert _resolve_review_model("qwen-35b") == "qwen/qwen3.5-35b"
        assert _resolve_review_model("qwen-122b") == "qwen/qwen3.5-122b"
        assert _resolve_review_model("qwen-9b") == "qwen/qwen3.5-9b"

    def test_resolve_review_model_unknown_passthrough(self):
        """Unknown names pass through as-is (might be a direct model ID)."""
        from dgov.decision_providers import _resolve_review_model

        assert _resolve_review_model("custom/model-v1") == "custom/model-v1"

    def test_model_review_requires_review_agent(self):
        """ModelReviewProvider raises ProviderError without review_agent."""
        from dgov.decision import ProviderError, ReviewOutputRequest
        from dgov.decision_providers import ModelReviewProvider

        provider = ModelReviewProvider()
        request = ReviewOutputRequest(
            project_root="/tmp",
            slug="test",
            review_agent="",
        )
        with pytest.raises(ProviderError, match="requires review_agent"):
            provider.review_output(request)

    def test_model_review_requires_diff(self):
        """ModelReviewProvider raises ProviderError without diff."""
        from dgov.decision import ProviderError, ReviewOutputRequest
        from dgov.decision_providers import ModelReviewProvider

        provider = ModelReviewProvider()
        request = ReviewOutputRequest(
            project_root="",
            slug="",
            review_agent="qwen-35b",
            diff="",
        )
        with pytest.raises(ProviderError, match="No diff available"):
            provider.review_output(request)
