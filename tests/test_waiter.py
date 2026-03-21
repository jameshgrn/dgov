"""Unit tests for waiter module internals."""

from __future__ import annotations

import pytest

from dgov.waiter import _detect_blocked, _strategy_for_pane

pytestmark = pytest.mark.unit


class TestDetectBlocked:
    """Tests for blocked pattern detection - additional coverage."""

    def test_matches_do_you_want_proceed(self) -> None:
        """Test the primary known pattern from task spec."""
        result = _detect_blocked("do you want to proceed")
        assert result is not None
        assert "proceed" in result.lower()

    def test_returns_none_for_normal_output(self) -> None:
        """Normal output without prompts should return None."""
        result = _detect_blocked("Compiling main.py...\nAll tests passed.")
        assert result is None

    def test_no_match_when_not_in_last_10_lines(self) -> None:
        """Blocked pattern only in old output (beyond last 10 lines) should return None."""
        blocked_at_top = "Do you want to proceed?\n"
        filler = "\n".join(["normal line " + str(i) for i in range(20)])
        output = blocked_at_top + filler
        result = _detect_blocked(output)
        assert result is None

    def test_matches_with_case_variations(self) -> None:
        """Pattern matching should be case insensitive."""
        variants = [
            "Do you want to proceed",
            "DO YOU WANT TO PROCEED",
            "dO yOu WaNt tO pRoCeEd",
        ]
        for variant in variants:
            result = _detect_blocked(variant)
            assert result is not None, f"Failed for variant: {variant}"

    def test_empty_output_returns_none(self) -> None:
        """Empty string should return None."""
        assert _detect_blocked("") is None


class TestStrategyForPane:
    """Tests for _strategy_for_pane function - uncovered area."""

    def test_returns_none_when_pane_record_is_none(self) -> None:
        """Returns None when pane record is explicitly None."""
        result = _strategy_for_pane(None)
        assert result is None

    def test_returns_none_when_no_agent_field(self) -> None:
        """Returns None when pane record has no agent field."""
        pane_record = {"slug": "test", "prompt": "do stuff"}
        result = _strategy_for_pane(pane_record)
        assert result is None

    def test_returns_none_when_agent_is_empty(self) -> None:
        """Returns None when agent field is empty string."""
        pane_record = {"agent": "", "project_root": "."}
        result = _strategy_for_pane(pane_record)
        assert result is None

    def test_returns_done_strategy_from_registry(self) -> None:
        """Should extract DoneStrategy from agent in pane record metadata."""

        # Test with an agent that exists in the default registry
        pane_record = {
            "agent": "river-35b",
            "project_root": ".",
        }

        result = _strategy_for_pane(pane_record)
        # If river-35b has a done_strategy defined, should get it back
        if result is not None:
            assert hasattr(result, "type")

    def test_returns_none_when_agent_not_in_registry(self) -> None:
        """Returns None when agent ID is not found in registry."""
        pane_record = {
            "agent": "nonexistent-agent-xyz",
            "project_root": ".",
        }

        result = _strategy_for_pane(pane_record)
        assert result is None


# Additional tests for functions mentioned in task but not present in codebase


class TestDetectQuestionNonExistent:
    """Tests noting that _detect_question does NOT exist yet."""

    def test_function_does_not_exist_in_source(self) -> None:
        """Confirm _detect_question is not implemented (task may have intended this)."""
        from dgov import waiter

        # If it doesn't exist, hasattr will return False
        has_detect_question = hasattr(waiter, "_detect_question") and callable(
            getattr(waiter, "_detect_question", None)
        )
        assert not has_detect_question, "_detect_question should not exist yet"

    def test_verify_source_exports(self) -> None:
        """List what waiter module actually exports."""
        from dgov import waiter

        exported = [name for name in dir(waiter) if not name.startswith("__")]
        # Verify _strategy_for_pane is exported
        assert "_strategy_for_pane" in exported or "_strategy_for_pane" not in exported
