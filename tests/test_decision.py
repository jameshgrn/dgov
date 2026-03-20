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
    monkeypatch.setattr("dgov.strategy.get_task_routing_provider", lambda: provider)

    result = classify_task("rename variable x to y in main.py")

    assert result == "pi"
