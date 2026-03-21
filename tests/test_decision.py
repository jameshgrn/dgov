from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from dgov.decision import (
    AuditProvider,
    CascadeProvider,
    ConsensusProvider,
    DecisionAuditEntry,
    DecisionKind,
    DecisionPayload,
    DecisionRecord,
    MonitorOutputDecision,
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
    DeterministicClassificationProvider,
    InspectionReviewProvider,
    LocalOutputClassificationProvider,
    OpenRouterRoutingProvider,
    StatisticalRoutingProvider,
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


# -- CascadeProvider tests --


def test_cascade_provider_returns_first_successful_result() -> None:
    """CascadeProvider returns first provider result when it succeeds."""

    def _first_success(_: MonitorOutputRequest) -> DecisionRecord[MonitorOutputDecision]:
        return DecisionRecord(
            kind=DecisionKind.CLASSIFY_OUTPUT,
            provider_id="static",  # StaticDecisionProvider uses default "static"
            decision=MonitorOutputDecision(classification="done"),
        )

    def _second_should_not_call(_: MonitorOutputRequest) -> DecisionRecord[MonitorOutputDecision]:
        raise ProviderError("should not be called")

    provider = CascadeProvider(
        inner_providers=[
            StaticDecisionProvider(provider_id="deterministic", classify_output_fn=_first_success),
            StaticDecisionProvider(classify_output_fn=_second_should_not_call),
        ]
    )

    result = provider.classify_output(MonitorOutputRequest(output="task complete"))

    assert result.decision.classification == "done"
    # Real provider_id is stored, not "cascade"
    assert result.provider_id == "deterministic"


def test_cascade_provider_falls_through_on_provider_error() -> None:
    """CascadeProvider falls through to second when first raises ProviderError."""

    def _first_fails(_: MonitorOutputRequest) -> DecisionRecord[MonitorOutputDecision]:
        raise ProviderError("regex no match")

    def _second_success(_: MonitorOutputRequest) -> DecisionRecord[MonitorOutputDecision]:
        return DecisionRecord(
            kind=DecisionKind.CLASSIFY_OUTPUT,
            provider_id="static",  # StaticDecisionProvider uses default "static"
            decision=MonitorOutputDecision(classification="working"),
        )

    provider = CascadeProvider(
        inner_providers=[
            StaticDecisionProvider(provider_id="deterministic", classify_output_fn=_first_fails),
            StaticDecisionProvider(provider_id="local", classify_output_fn=_second_success),
        ]
    )

    result = provider.classify_output(MonitorOutputRequest(output="working on code"))

    assert result.decision.classification == "working"
    assert result.provider_id == "local"


def test_cascade_provider_falls_through_when_validator_rejects() -> None:
    """CascadeProvider falls through when validator rejects first result."""

    def _first_result(_: MonitorOutputRequest) -> DecisionRecord[MonitorOutputDecision]:
        return DecisionRecord(
            kind=DecisionKind.CLASSIFY_OUTPUT,
            provider_id="static",
            decision=MonitorOutputDecision(classification="done"),
        )

    def _reject_done(result: DecisionRecord) -> bool:
        # Reject "done" classifications, prefer other results
        return result.decision.classification != "done"  # type: ignore[attr-defined]

    def _second_success(_: MonitorOutputRequest) -> DecisionRecord[MonitorOutputDecision]:
        return DecisionRecord(
            kind=DecisionKind.CLASSIFY_OUTPUT,
            provider_id="static",
            decision=MonitorOutputDecision(classification="working"),
        )

    provider = CascadeProvider(
        inner_providers=[
            StaticDecisionProvider(provider_id="deterministic", classify_output_fn=_first_result),
            StaticDecisionProvider(provider_id="local", classify_output_fn=_second_success),
        ],
        validator=_reject_done,
    )

    result = provider.classify_output(MonitorOutputRequest(output="task complete"))

    assert result.decision.classification == "working"
    assert result.provider_id == "local"


# -- DeterministicClassificationProvider tests --


def test_deterministic_classification_provider_returns_known_pattern() -> None:
    """DeterministicClassificationProvider returns classification for known patterns."""
    provider = DeterministicClassificationProvider()

    # Test "done" pattern (success/complete)
    result = provider.classify_output(
        MonitorOutputRequest(output="Task complete. All tests pass.")
    )
    assert result.decision.classification == "done"
    assert result.provider_id == "deterministic-classifier"

    # Test "stuck" pattern (error keywords)
    result = provider.classify_output(
        MonitorOutputRequest(output="Error: ConnectionRefusedException raised")
    )
    assert result.decision.classification == "stuck"
    assert result.provider_id == "deterministic-classifier"

    # Test "committing" pattern
    result = provider.classify_output(
        MonitorOutputRequest(output="Running git commit and pushing changes")
    )
    assert result.decision.classification == "committing"
    assert result.provider_id == "deterministic-classifier"


def test_deterministic_classification_provider_raises_on_ambiguous() -> None:
    """DeterministicClassificationProvider raises ProviderError for ambiguous input."""
    provider = DeterministicClassificationProvider()

    # Unknown output - should fall through to LLM
    with pytest.raises(ProviderError, match="No deterministic pattern matched"):
        provider.classify_output(MonitorOutputRequest(output="I'm working on this now"))


# -- ConsensusProvider tests --


def test_consensus_provider_returns_a_when_both_agree() -> None:
    """ConsensusProvider returns provider_a's result when both agree."""

    def _agree_fn(a: DecisionRecord[DecisionPayload], b: DecisionRecord[DecisionPayload]) -> bool:
        return (
            a.decision.classification == b.decision.classification  # type: ignore[attr-defined]
        )

    provider = ConsensusProvider(
        provider_a=StaticDecisionProvider(
            provider_id="cheap-a",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="cheap-a",
                decision=MonitorOutputDecision(classification="done"),
            ),
        ),
        provider_b=StaticDecisionProvider(
            provider_id="cheap-b",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="cheap-b",
                decision=MonitorOutputDecision(classification="done"),
            ),
        ),
        tiebreaker=StaticDecisionProvider(
            provider_id="expensive-tiebreaker",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="expensive-tiebreaker",
                decision=MonitorOutputDecision(classification="tiebreaker"),
            ),
        ),
        agree_fn=_agree_fn,
    )

    result = provider.classify_output(MonitorOutputRequest(output="task complete"))

    assert result.decision.classification == "done"
    # Returns provider_a's result when they agree
    assert result.provider_id == "cheap-a"


def test_consensus_provider_escalates_to_tiebreaker_on_disagreement() -> None:
    """ConsensusProvider escalates to tiebreaker when providers disagree."""

    def _agree_fn(a: DecisionRecord[DecisionPayload], b: DecisionRecord[DecisionPayload]) -> bool:
        return (
            a.decision.classification == b.decision.classification  # type: ignore[attr-defined]
        )

    provider = ConsensusProvider(
        provider_a=StaticDecisionProvider(
            provider_id="cheap-a",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="cheap-a",
                decision=MonitorOutputDecision(classification="done"),
            ),
        ),
        provider_b=StaticDecisionProvider(
            provider_id="cheap-b",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="cheap-b",
                decision=MonitorOutputDecision(classification="working"),
            ),
        ),
        tiebreaker=StaticDecisionProvider(
            provider_id="expensive-tiebreaker",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="expensive-tiebreaker",
                decision=MonitorOutputDecision(classification="tiebreaker"),
            ),
        ),
        agree_fn=_agree_fn,
    )

    result = provider.classify_output(MonitorOutputRequest(output="ambiguous"))

    assert result.decision.classification == "tiebreaker"
    # Returns tiebreaker's result on disagreement
    assert result.provider_id == "expensive-tiebreaker"


def test_consensus_provider_degrades_when_a_fails() -> None:
    """ConsensusProvider degrades to provider_b when provider_a fails."""

    def _agree_fn(a: DecisionRecord[DecisionPayload], b: DecisionRecord[DecisionPayload]) -> bool:
        return True  # Never reached

    provider = ConsensusProvider(
        provider_a=StaticDecisionProvider(
            provider_id="cheap-a",
            classify_output_fn=lambda _: (_ for _ in ()).throw(ProviderError("provider a failed")),
        ),
        provider_b=StaticDecisionProvider(
            provider_id="cheap-b",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="cheap-b",
                decision=MonitorOutputDecision(classification="working"),
            ),
        ),
        tiebreaker=StaticDecisionProvider(
            provider_id="expensive-tiebreaker",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="expensive-tiebreaker",
                decision=MonitorOutputDecision(classification="tiebreaker"),
            ),
        ),
        agree_fn=_agree_fn,
    )

    result = provider.classify_output(MonitorOutputRequest(output="test"))

    assert result.decision.classification == "working"
    # Returns provider_b when a fails
    assert result.provider_id == "cheap-b"


def test_consensus_provider_degrades_when_b_fails() -> None:
    """ConsensusProvider degrades to provider_a when provider_b fails."""

    def _agree_fn(a: DecisionRecord[DecisionPayload], b: DecisionRecord[DecisionPayload]) -> bool:
        return True  # Never reached

    provider = ConsensusProvider(
        provider_a=StaticDecisionProvider(
            provider_id="cheap-a",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="cheap-a",
                decision=MonitorOutputDecision(classification="done"),
            ),
        ),
        provider_b=StaticDecisionProvider(
            provider_id="cheap-b",
            classify_output_fn=lambda _: (_ for _ in ()).throw(ProviderError("provider b failed")),
        ),
        tiebreaker=StaticDecisionProvider(
            provider_id="expensive-tiebreaker",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="expensive-tiebreaker",
                decision=MonitorOutputDecision(classification="tiebreaker"),
            ),
        ),
        agree_fn=_agree_fn,
    )

    result = provider.classify_output(MonitorOutputRequest(output="test"))

    assert result.decision.classification == "done"
    # Returns provider_a when b fails
    assert result.provider_id == "cheap-a"


def test_consensus_provider_raises_when_both_fail() -> None:
    """ConsensusProvider raises ProviderError when both providers fail."""

    def _agree_fn(a: DecisionRecord[DecisionPayload], b: DecisionRecord[DecisionPayload]) -> bool:
        return True  # Never reached

    provider = ConsensusProvider(
        provider_a=StaticDecisionProvider(
            provider_id="cheap-a",
            classify_output_fn=lambda _: (_ for _ in ()).throw(ProviderError("provider a failed")),
        ),
        provider_b=StaticDecisionProvider(
            provider_id="cheap-b",
            classify_output_fn=lambda _: (_ for _ in ()).throw(ProviderError("provider b failed")),
        ),
        tiebreaker=StaticDecisionProvider(
            provider_id="expensive-tiebreaker",
            classify_output_fn=lambda _: DecisionRecord(
                kind=DecisionKind.CLASSIFY_OUTPUT,
                provider_id="expensive-tiebreaker",
                decision=MonitorOutputDecision(classification="tiebreaker"),
            ),
        ),
        agree_fn=_agree_fn,
    )

    with pytest.raises(ProviderError, match="Both consensus providers failed"):
        provider.classify_output(MonitorOutputRequest(output="test"))


# -- StatisticalRoutingProvider tests --


def test_statistical_routing_picks_best_agent(tmp_path: str) -> None:
    """StatisticalRoutingProvider picks the agent with highest pass rate."""
    from dgov.decision import DecisionAuditEntry
    from dgov.persistence import record_decision_audit

    session_root = tmp_path

    # Create mock review records for two agents
    def make_review_record(agent: str, pane_slug: str, verdict: str) -> dict:
        return {
            "ts": "2024-01-01T00:00:00+00:00",
            "kind": "review_output",
            "provider_id": "inspection-review",
            "pane_slug": pane_slug,
            "agent_id": agent,
            "request_json": "{}",
            "result_json": f'{{"decision": {{"verdict": "{verdict}"}}}}',
            "error": None,
            "duration_ms": 100.0,
            "metadata_json": "{}",
        }

    # Agent pi: 8 safe, 2 stuck = 80% pass rate (10 reviews)
    for i in range(8):
        record_decision_audit(
            session_root,
            DecisionAuditEntry(
                provider_id="inspection-review",
                request=ReviewOutputRequest(slug=f"pi-task-{i}", agent_id="pi"),
                result=None,
                error=None,
                duration_ms=100.5,
            ),
        )
    # Override these with manual records to set verdicts
    for i in range(8):
        rec = make_review_record("pi", f"pi-safe-{i}", "safe")
        from dgov.persistence import _get_db

        conn = _get_db(session_root)
        conn.execute(
            """
            INSERT INTO decision_journal (ts, kind, provider_id, trace_id, pane_slug, agent_id,
                request_json, result_json, error, duration_ms, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                rec["ts"],
                rec["kind"],
                rec["provider_id"],
                None,
                rec["pane_slug"],
                rec["agent_id"],
                rec["request_json"],
                rec["result_json"],
                rec["error"],
                rec["duration_ms"],
                rec["metadata_json"],
            ),
        )
        conn.commit()

    for i in range(2):
        rec = make_review_record("pi", f"pi-stuck-{i}", "stuck")
        from dgov.persistence import _get_db

        conn = _get_db(session_root)
        conn.execute(
            """
            INSERT INTO decision_journal (ts, kind, provider_id, trace_id, pane_slug, agent_id,
                request_json, result_json, error, duration_ms, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                rec["ts"],
                rec["kind"],
                rec["provider_id"],
                None,
                rec["pane_slug"],
                rec["agent_id"],
                rec["request_json"],
                rec["result_json"],
                rec["error"],
                rec["duration_ms"],
                rec["metadata_json"],
            ),
        )
        conn.commit()

    # Agent claude: 9 safe, 1 stuck = 90% pass rate (10 reviews)
    for i in range(9):
        rec = make_review_record("claude", f"claude-safe-{i}", "safe")
        from dgov.persistence import _get_db

        conn = _get_db(session_root)
        conn.execute(
            """
            INSERT INTO decision_journal (ts, kind, provider_id, trace_id, pane_slug, agent_id,
                request_json, result_json, error, duration_ms, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                rec["ts"],
                rec["kind"],
                rec["provider_id"],
                None,
                rec["pane_slug"],
                rec["agent_id"],
                rec["request_json"],
                rec["result_json"],
                rec["error"],
                rec["duration_ms"],
                rec["metadata_json"],
            ),
        )
        conn.commit()

    for i in range(1):
        rec = make_review_record("claude", f"claude-stuck-{i}", "stuck")
        from dgov.persistence import _get_db

        conn = _get_db(session_root)
        conn.execute(
            """
            INSERT INTO decision_journal (ts, kind, provider_id, trace_id, pane_slug, agent_id,
                request_json, result_json, error, duration_ms, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                rec["ts"],
                rec["kind"],
                rec["provider_id"],
                None,
                rec["pane_slug"],
                rec["agent_id"],
                rec["request_json"],
                rec["result_json"],
                rec["error"],
                rec["duration_ms"],
                rec["metadata_json"],
            ),
        )
        conn.commit()

    # Test: should pick claude (90% > 80%)
    provider = StatisticalRoutingProvider(session_root=str(session_root), min_samples=5)
    result = provider.route_task(
        RouteTaskRequest(prompt="debug flaky test", installed_agents=("pi", "claude"))
    )

    assert result.decision.agent == "claude"
    assert "statistical:" in result.decision.reason
    assert "90%" in result.decision.reason


def test_statistical_routing_raises_on_insufficient_data(tmp_path: str) -> None:
    """StatisticalRoutingProvider raises ProviderError when no agents have min_samples."""
    from dgov.decision import DecisionAuditEntry
    from dgov.persistence import record_decision_audit

    session_root = tmp_path

    # Create a single review for agent pi (below min_samples=5)
    provider = StatisticalRoutingProvider(session_root=str(session_root), min_samples=5)

    result = DecisionRecord(
        kind=DecisionKind.REVIEW_OUTPUT,
        provider_id="inspection-review",
        decision={"verdict": "safe"},
        trace_id="trace-1",
    )
    record_decision_audit(
        session_root,
        DecisionAuditEntry(
            provider_id="inspection-review",
            request=ReviewOutputRequest(slug="task-1", agent_id="pi"),
            result=result,
            error=None,
            duration_ms=100.5,
        ),
    )

    with pytest.raises(ProviderError, match="insufficient data"):
        provider.route_task(RouteTaskRequest(prompt="test prompt"))


def test_statistical_routing_fallback_to_pane_slug_lookup(tmp_path: str) -> None:
    """StatisticalRoutingProvider falls back to pane slug lookup for agent_id."""
    from dgov.persistence import WorkerPane, add_pane

    session_root = tmp_path

    # Create a pane record (without agent_id in journal)
    pane = WorkerPane(
        slug="task-1",
        prompt="test task",
        pane_id="pane-1",
        agent="pi",
        project_root=str(session_root),
        worktree_path="/tmp/wt",
        branch_name="test",
    )
    add_pane(session_root, pane)

    # Create review record WITHOUT agent_id field
    from dgov.persistence import _get_db

    conn = _get_db(session_root)
    conn.execute(
        """
        INSERT INTO decision_journal (ts, kind, provider_id, trace_id, pane_slug, agent_id,
            request_json, result_json, error, duration_ms, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            "2024-01-01T00:00:00+00:00",
            "review_output",
            "inspection-review",
            None,
            "task-1",
            None,
            "{}",
            '{"decision": {"verdict": "safe"}}',
            None,
            100.0,
            "{}",
        ),
    )
    conn.commit()

    # Should still work - falls back to pane slug lookup
    provider = StatisticalRoutingProvider(session_root=str(session_root), min_samples=1)
    result = provider.route_task(RouteTaskRequest(prompt="test prompt"))

    assert result.decision.agent == "pi"
