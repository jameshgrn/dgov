"""Tests for dgov diagnostic checks."""

from __future__ import annotations

import pytest


def _diagnose_mod():
    import importlib

    return importlib.import_module("dgov.diagnose")


def check_plan_claims_violation(*a, **k):
    return _diagnose_mod().check_plan_claims_violation(*a, **k)


pytestmark = pytest.mark.unit


class TestCheckPlanClaimsViolation:
    def test_plan_claims_violation_emits_finding(self) -> None:
        events = [
            {
                "event": "review_fail",
                "verdict": "scope_violation",
                "plan_name": "p",
                "task_slug": "t",
            }
        ]

        findings = check_plan_claims_violation(events)

        assert len(findings) == 1
        assert findings[0].name == "plan_claims_violation"
        assert findings[0].intent_class == "Governance repair"
        assert "p/t" in findings[0].evidence
        assert "scope_violation" in findings[0].evidence

    def test_plan_claims_violation_dedupes_repeats(self) -> None:
        events = [
            {
                "event": "review_fail",
                "verdict": "scope_violation",
                "plan_name": "p",
                "task_slug": "t",
            },
            {
                "event": "review_fail",
                "verdict": "scope_violation",
                "plan_name": "p",
                "task_slug": "t",
            },
            {
                "event": "review_fail",
                "verdict": "read_scope_violation",
                "plan_name": "p",
                "task_slug": "t",
            },
        ]

        findings = check_plan_claims_violation(events)

        assert len(findings) == 1
        assert findings[0].name == "plan_claims_violation"

    def test_plan_claims_violation_ignores_other_verdicts(self) -> None:
        events = [
            {
                "event": "review_fail",
                "verdict": "lint_fail",
                "plan_name": "p",
                "task_slug": "t",
            }
        ]

        findings = check_plan_claims_violation(events)

        assert findings == []

    def test_plan_claims_violation_ignores_other_events(self) -> None:
        events = [
            {
                "event": "review_pass",
                "verdict": "scope_violation",
                "plan_name": "p",
                "task_slug": "t",
            }
        ]

        findings = check_plan_claims_violation(events)

        assert findings == []

    def test_plan_claims_violation_surfaces_read_scope_violation(self) -> None:
        events = [
            {
                "event": "review_fail",
                "verdict": "read_scope_violation",
                "plan_name": "p2",
                "task_slug": "t2",
            }
        ]

        findings = check_plan_claims_violation(events)

        assert len(findings) == 1
        assert findings[0].name == "plan_claims_violation"
        assert "read_scope_violation" in findings[0].evidence

    def test_plan_claims_violation_empty_events_is_silent(self) -> None:
        findings = check_plan_claims_violation([])

        assert findings == []
