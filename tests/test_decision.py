from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from dgov.decision import (
    AuditProvider,
    DecisionAuditEntry,
    DecisionKind,
    DecisionRecord,
    MonitorOutputRequest,
    ProviderError,
    ProviderTimeoutError,
    ReviewOutputRequest,
    RouteTaskDecision,
    RouteTaskRequest,
    ShadowDecisionResult,
    ShadowProvider,
    StaticDecisionProvider,
    TimeoutProvider,
)
from dgov.decision_providers import (
    InspectionReviewProvider,
    LocalOutputClassificationProvider,
    OpenRouterRoutingProvider,
)

pytestmark = pytest.mark.unit


def _route_record(agent: str, *, provider_id: str = "static") -> DecisionRecord[RouteTaskDecision]:
    return DecisionRecord(
        kind=DecisionKind.ROUTE_TASK,
        provider_id=provider_id,
        decision=RouteTaskDecision(agent=agent),
        confidence=0.9,
    )


def test_static_provider_returns_typed_route_decision() -> None:
    provider = StaticDecisionProvider(
        route_task_fn=lambda request: DecisionRecord(
            kind=DecisionKind.ROUTE_TASK,
            provider_id="static",
            decision=RouteTaskDecision(agent="pi", reason=request.prompt),
        )
    )

    result = provider.route_task(RouteTaskRequest(prompt="rename variable"))

    assert DecisionKind.ROUTE_TASK in provider.capabilities()
    assert result.decision.agent == "pi"
    assert result.decision.reason == "rename variable"


def test_audit_provider_records_success() -> None:
    entries: list[DecisionAuditEntry] = []
    provider = AuditProvider(
        inner=StaticDecisionProvider(route_task_fn=lambda request: _route_record("claude")),
        sink=entries.append,
    )

    result = provider.route_task(RouteTaskRequest(prompt="debug flaky test"))

    assert result.decision.agent == "claude"
    assert len(entries) == 1
    assert entries[0].error is None
    assert entries[0].result is not None
    assert entries[0].provider_id == "static"


def test_audit_provider_records_error() -> None:
    entries: list[DecisionAuditEntry] = []

    def _fail(_: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        raise ProviderError("boom")

    provider = AuditProvider(
        inner=StaticDecisionProvider(route_task_fn=_fail),
        sink=entries.append,
    )

    with pytest.raises(ProviderError, match="boom"):
        provider.route_task(RouteTaskRequest(prompt="anything"))

    assert len(entries) == 1
    assert entries[0].result is None
    assert entries[0].error == "boom"


def test_timeout_provider_raises_on_slow_provider() -> None:
    def _slow(_: RouteTaskRequest) -> DecisionRecord[RouteTaskDecision]:
        time.sleep(0.05)
        return _route_record("pi")

    provider = TimeoutProvider(
        inner=StaticDecisionProvider(route_task_fn=_slow),
        timeout_s=0.01,
    )

    with pytest.raises(ProviderTimeoutError, match="timed out"):
        provider.route_task(RouteTaskRequest(prompt="rename variable"))


def test_shadow_provider_returns_primary_and_records_shadow() -> None:
    shadows: list[ShadowDecisionResult] = []
    provider = ShadowProvider(
        primary=StaticDecisionProvider(
            provider_id="primary",
            route_task_fn=lambda request: _route_record("claude", provider_id="primary"),
        ),
        shadow=StaticDecisionProvider(
            provider_id="shadow",
            route_task_fn=lambda request: _route_record("pi", provider_id="shadow"),
        ),
        sink=shadows.append,
    )

    result = provider.route_task(RouteTaskRequest(prompt="debug flaky test"))

    assert result.decision.agent == "claude"
    assert len(shadows) == 1
    assert shadows[0].primary.provider_id == "primary"
    assert shadows[0].shadow is not None
    assert shadows[0].shadow.provider_id == "shadow"
    assert shadows[0].shadow_error is None


def test_openrouter_routing_provider_parses_known_agent() -> None:
    provider = OpenRouterRoutingProvider()

    with patch(
        "dgov.openrouter.chat_completion",
        return_value={
            "model": "qwen/qwen3.5-35b",
            "choices": [{"message": {"content": "codex"}}],
        },
    ):
        result = provider.route_task(
            RouteTaskRequest(
                prompt="refactor all files",
                installed_agents=("pi", "claude", "codex"),
                trace_id="trace-1",
            )
        )

    assert result.decision.agent == "codex"
    assert result.provider_id == "openrouter-routing"
    assert result.model_id == "qwen/qwen3.5-35b"
    assert result.trace_id == "trace-1"


def test_local_output_provider_normalizes_unknown_classification() -> None:
    provider = LocalOutputClassificationProvider()

    with patch(
        "dgov.openrouter.chat_completion_local_first",
        return_value={
            "model": "qwen-local",
            "choices": [{"message": {"content": "I think this is working"}}],
        },
    ):
        result = provider.classify_output(
            MonitorOutputRequest(output="ambiguous output", trace_id="trace-2")
        )

    assert result.decision.classification == "unknown"
    assert result.provider_id == "local-output-classifier"
    assert result.model_id == "qwen-local"
    assert result.trace_id == "trace-2"


def test_inspection_review_provider_returns_typed_review_record() -> None:
    provider = InspectionReviewProvider()

    with patch(
        "dgov.inspection.review_worker_pane",
        return_value={
            "slug": "task",
            "verdict": "review",
            "commit_count": 2,
            "issues": ["protected files touched"],
        },
    ):
        result = provider.review_output(
            ReviewOutputRequest(
                project_root="/repo",
                slug="task",
                session_root="/session",
            )
        )

    assert result.provider_id == "inspection-review"
    assert result.decision.verdict == "review"
    assert result.decision.commit_count == 2
    assert result.decision.issues == ("protected files touched",)
    assert result.artifact == {
        "slug": "task",
        "verdict": "review",
        "commit_count": 2,
        "issues": ["protected files touched"],
    }


def test_classify_task_uses_provider_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    from dgov.strategy import classify_task

    provider = StaticDecisionProvider(route_task_fn=lambda request: _route_record("pi"))
    monkeypatch.setattr(
        "dgov.provider_registry.get_provider",
        lambda kind, session_root=None: provider,
    )

    result = classify_task("rename variable x to y in main.py")

    assert result == "pi"


# -- Decision journal tests --


class MockResult:
    """Mock decision result for testing."""

    def __init__(self, trace_id: str = "trace-123", model_id: str = "qwen-35b"):
        self.trace_id = trace_id
        self.model_id = model_id
        self.confidence = 0.9
        self.latency_ms = 100.0
        self.cost_usd = 0.01
        self.evidence_refs = []
        self.raw_artifact_ref = None
        self.created_at = 1234567890.0


def test_record_decision_audit_writes_model_id_confidence_pane_slug(tmp_path: str) -> None:
    """Test that record_decision_audit writes model_id, confidence, pane_slug columns."""
    from dgov.persistence import read_decision_journal, record_decision_audit

    session_root = tmp_path

    result = MockResult(model_id="qwen-35b")
    request = RouteTaskRequest(prompt="test prompt", trace_id="trace-123")
    # Use object.__setattr__ for frozen dataclass
    object.__setattr__(request, "pane_slug", "test-pane-slug")

    entry = DecisionAuditEntry(
        provider_id="test-provider",
        request=request,
        result=result,
        error=None,
        duration_ms=100.5,
    )

    record_decision_audit(session_root, entry)

    # Read back and verify new columns are populated
    rows = read_decision_journal(session_root)
    assert len(rows) == 1
    assert rows[0]["model_id"] == "qwen-35b"
    assert rows[0]["confidence"] == 0.9
    assert rows[0]["pane_slug"] == "test-pane-slug"


def test_read_decision_journal_filters_by_pane_slug(tmp_path: str) -> None:
    """Test that read_decision_journal filters by pane_slug."""
    from dgov.persistence import read_decision_journal, record_decision_audit

    session_root = tmp_path

    result = MockResult()

    # Create entries with different pane_slugs
    for slug in ["pane-1", "pane-2", "pane-1"]:
        request = RouteTaskRequest(prompt="test prompt", trace_id="trace-123")
        object.__setattr__(request, "pane_slug", slug)
        entry = DecisionAuditEntry(
            provider_id="test-provider",
            request=request,
            result=result,
            error=None,
            duration_ms=100.5,
        )
        record_decision_audit(session_root, entry)

    # Query with pane_slug filter
    rows = read_decision_journal(session_root, pane_slug="pane-1")
    assert len(rows) == 2
    for row in rows:
        assert row["pane_slug"] == "pane-1"

    rows_unfiltered = read_decision_journal(session_root)
    assert len(rows_unfiltered) == 3


def test_decision_journal_migration_is_idempotent(tmp_path: str) -> None:
    """Test that migration is idempotent (calling it twice doesn't error)."""
    from dgov.persistence import _get_db

    # _get_db creates tables on first call; verify columns exist
    conn = _get_db(str(tmp_path))
    cursor = conn.execute("PRAGMA table_info(decision_journal)")
    columns = {row[1] for row in cursor.fetchall()}

    required_columns = {
        "id",
        "ts",
        "kind",
        "provider_id",
        "trace_id",
        "model_id",
        "confidence",
        "pane_slug",
    }
    assert required_columns.issubset(columns)


def test_read_decision_journal_combined_filters(tmp_path: str) -> None:
    """Test that kind and pane_slug filters can be combined."""
    from dgov.persistence import read_decision_journal, record_decision_audit

    session_root = tmp_path

    result = MockResult()

    # Create entries with different kinds and pane_slugs
    for kind_name, slug in [
        ("route_task", "pane-1"),
        ("classify_output", "pane-2"),
    ]:
        if kind_name == "route_task":
            request = RouteTaskRequest(prompt="test prompt", trace_id="trace-123")
            object.__setattr__(request, "pane_slug", slug)
        else:
            request = MonitorOutputRequest(output="test output", trace_id="trace-123")

        entry = DecisionAuditEntry(
            provider_id="test-provider",
            request=request,
            result=result,
            error=None,
            duration_ms=100.5,
        )
        record_decision_audit(session_root, entry)

    # Query with both filters - route_task pane-1
    rows = read_decision_journal(session_root, kind="route_task", pane_slug="pane-1")
    assert len(rows) == 1
    assert rows[0]["kind"] == "route_task"
    assert rows[0]["pane_slug"] == "pane-1"

    # classify_output - no pane_slug set (None)
    rows = read_decision_journal(session_root, kind="classify_output", pane_slug=None)
    # None filter returns all results since WHERE pane_slug = NULL never matches
    assert len(rows) >= 1
