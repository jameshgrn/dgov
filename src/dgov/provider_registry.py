"""Central provider selection and optional decision journaling."""

from __future__ import annotations

from dgov.decision import AuditProvider, DecisionKind, DecisionProvider, UnsupportedDecisionError
from dgov.decision_providers import (
    InspectionReviewProvider,
    LocalOutputClassificationProvider,
    OpenRouterRoutingProvider,
)


def _base_provider(kind: DecisionKind) -> DecisionProvider:
    match kind:
        case DecisionKind.ROUTE_TASK:
            return OpenRouterRoutingProvider()
        case DecisionKind.CLASSIFY_OUTPUT:
            return LocalOutputClassificationProvider()
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


def get_route_task_provider(*, session_root: str | None = None) -> DecisionProvider:
    return get_provider(DecisionKind.ROUTE_TASK, session_root=session_root)


def get_output_classification_provider(*, session_root: str | None = None) -> DecisionProvider:
    return get_provider(DecisionKind.CLASSIFY_OUTPUT, session_root=session_root)


def get_review_provider(*, session_root: str | None = None) -> DecisionProvider:
    return get_provider(DecisionKind.REVIEW_OUTPUT, session_root=session_root)
