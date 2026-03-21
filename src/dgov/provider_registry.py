"""Central provider selection and optional decision journaling."""

from __future__ import annotations

from dgov.decision import (
    AuditProvider,
    CascadeProvider,
    DecisionKind,
    DecisionProvider,
    UnsupportedDecisionError,
)
from dgov.decision_providers import (
    DeterministicClassificationProvider,
    InspectionReviewProvider,
    LocalOutputClassificationProvider,
    OpenRouterRoutingProvider,
)


def _base_provider(kind: DecisionKind) -> DecisionProvider:
    match kind:
        case DecisionKind.ROUTE_TASK:
            return OpenRouterRoutingProvider()
        case DecisionKind.CLASSIFY_OUTPUT:
            # Deterministic-first cascade: free regex → cheap LLM fallback
            return CascadeProvider(
                inner_providers=[
                    DeterministicClassificationProvider(),
                    LocalOutputClassificationProvider(),
                ]
            )
        case DecisionKind.REVIEW_OUTPUT:
            return InspectionReviewProvider()
        case _:
            raise UnsupportedDecisionError(f"No provider registered for {kind}")


def get_provider(kind: DecisionKind, *, session_root: str | None = None) -> DecisionProvider:
    """Return the provider for a decision kind, wrapped with journaling when requested."""
    provider = _base_provider(kind)
    if not session_root:
        return provider

    from dgov.persistence import record_decision_audit

    return AuditProvider(
        inner=provider,
        sink=lambda entry: record_decision_audit(session_root, entry),
    )
