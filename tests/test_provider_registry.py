"""Tests for dgov.provider_registry module."""

import pytest

from dgov.decision import (
    AuditProvider,
    CascadeProvider,
    DecisionKind,
    UnsupportedDecisionError,
)
from dgov.decision_providers import InspectionReviewProvider
from dgov.provider_registry import get_provider


class TestGetProvider:
    """Test cases for get_provider function."""

    @pytest.mark.unit
    def test_get_provider_returns_cascade_for_classify_output(self):
        """get_provider returns CascadeProvider for CLASSIFY_OUTPUT kind."""
        provider = get_provider(DecisionKind.CLASSIFY_OUTPUT)
        assert isinstance(provider, CascadeProvider)

    @pytest.mark.unit
    def test_get_provider_returns_cascade_for_route_task(self):
        """get_provider returns CascadeProvider for ROUTE_TASK kind."""
        provider = get_provider(DecisionKind.ROUTE_TASK)
        assert isinstance(provider, CascadeProvider)

    @pytest.mark.unit
    def test_get_provider_returns_inspection_review_for_review_output(self):
        """get_provider returns InspectionReviewProvider for REVIEW_OUTPUT kind."""
        provider = get_provider(DecisionKind.REVIEW_OUTPUT)
        assert isinstance(provider, InspectionReviewProvider)

    @pytest.mark.unit
    def test_get_provider_wraps_with_audit_when_session_root_provided(self):
        """get_provider wraps base provider with AuditProvider when session_root provided."""
        temp_dir = "/tmp/test_session_root"
        provider = get_provider(DecisionKind.CLASSIFY_OUTPUT, session_root=temp_dir)
        assert isinstance(provider, AuditProvider)

    @pytest.mark.unit
    def test_get_provider_raises_unsupported_decision_error_for_unknown_kind(self):
        """get_provider raises UnsupportedDecisionError for unknown decision kinds."""
        # PARSE_COMPLETION and DISAMBIGUATE are not implemented in _base_provider
        for kind in (DecisionKind.PARSE_COMPLETION, DecisionKind.DISAMBIGUATE):
            with pytest.raises(UnsupportedDecisionError) as exc_info:
                get_provider(kind)
            assert f"No provider registered for {kind}" in str(exc_info.value)
