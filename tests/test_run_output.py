"""Unit tests for run_output helpers — status classification and warning emission."""

from __future__ import annotations

from datetime import timedelta

import pytest

from dgov.cli.run_output import (
    apply_self_review_degraded,
    derive_run_status,
    emit_self_review_degraded_warning,
    run_status_and_summary,
)

pytestmark = pytest.mark.unit

_EMPTY_GATE: dict[str, object] = {}
_EMPTY_BRANCH: dict[str, object] = {}
_ZERO_DUR = timedelta(seconds=5)


class TestApplySelfReviewDegraded:
    def test_complete_plus_degraded_returns_degraded(self):
        assert apply_self_review_degraded("complete", True) == "degraded"

    def test_complete_without_degraded_unchanged(self):
        assert apply_self_review_degraded("complete", False) == "complete"

    def test_failed_plus_degraded_stays_failed(self):
        assert apply_self_review_degraded("failed", True) == "failed"

    def test_partial_plus_degraded_stays_partial(self):
        assert apply_self_review_degraded("partial", True) == "partial"

    def test_degraded_plus_degraded_stays_degraded(self):
        assert apply_self_review_degraded("degraded", True) == "degraded"


class TestDeriveRunStatus:
    def test_clean_run_returns_complete(self):
        assert (
            derive_run_status(failed=[], abandoned=[], succeeded=["a"], sentrux_failed=False)
            == "complete"
        )

    def test_sentrux_alone_returns_degraded(self):
        assert (
            derive_run_status(failed=[], abandoned=[], succeeded=["a"], sentrux_failed=True)
            == "degraded"
        )

    def test_failed_with_succeeded_returns_partial(self):
        assert (
            derive_run_status(failed=["b"], abandoned=[], succeeded=["a"], sentrux_failed=False)
            == "partial"
        )

    def test_all_failed_returns_failed(self):
        assert (
            derive_run_status(failed=["a"], abandoned=[], succeeded=[], sentrux_failed=False)
            == "failed"
        )


class TestRunStatusAndSummary:
    def test_backwards_compatible_call(self):
        results = {"a": "merged"}
        status, *_ = run_status_and_summary(results, {}, _EMPTY_GATE, _EMPTY_BRANCH, _ZERO_DUR)
        assert status == "complete"

    def test_failure_in_results_returns_partial(self):
        results = {"a": "merged", "b": "failed"}
        status, failed, _abandoned, _skipped, succeeded, _ = run_status_and_summary(
            results, {}, _EMPTY_GATE, _EMPTY_BRANCH, _ZERO_DUR
        )
        assert status == "partial"
        assert "b" in failed
        assert "a" in succeeded


class TestEmitSelfReviewDegradedWarning:
    def test_emits_degraded_warning_to_stderr(self, capsys):
        emit_self_review_degraded_warning()
        captured = capsys.readouterr()
        assert "self-review" in captured.err
        assert "degraded" in captured.err
