"""Tests for circuit-breaker detection of stuck workers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dgov.backend import set_backend
from dgov.done import _circuit_breaker_fingerprint, _is_done
from dgov.persistence import (
    CIRCUIT_BREAKER_THRESHOLD,
    STATE_DIR,
    WorkerPane,
    add_pane,
    get_pane,
    record_failure,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def mock_backend():
    import dgov.backend as _be

    prev = _be._backend
    mock = MagicMock()
    mock.create_pane.return_value = "%1"
    mock.is_alive.return_value = True
    mock.bulk_info.return_value = {}
    set_backend(mock)
    yield mock
    _be._backend = prev


def _setup_pane(tmp_path: Path, slug: str = "test-slug") -> None:
    add_pane(
        str(tmp_path),
        WorkerPane(
            slug=slug,
            prompt="test",
            pane_id="%1",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / slug),
            branch_name=slug,
        ),
    )


class TestRecordFailure:
    def test_first_failure_returns_one(self, tmp_path: Path) -> None:
        _setup_pane(tmp_path)
        count = record_failure(str(tmp_path), "test-slug", "abc123")
        assert count == 1

    def test_same_hash_increments(self, tmp_path: Path) -> None:
        _setup_pane(tmp_path)
        sr = str(tmp_path)
        record_failure(sr, "test-slug", "abc123")
        record_failure(sr, "test-slug", "abc123")
        count = record_failure(sr, "test-slug", "abc123")
        assert count == 3

    def test_different_hashes_independent(self, tmp_path: Path) -> None:
        _setup_pane(tmp_path)
        sr = str(tmp_path)
        record_failure(sr, "test-slug", "hash_a")
        record_failure(sr, "test-slug", "hash_b")
        count_a = record_failure(sr, "test-slug", "hash_a")
        count_b = record_failure(sr, "test-slug", "hash_b")
        assert count_a == 2
        assert count_b == 2

    def test_missing_slug_returns_zero(self, tmp_path: Path) -> None:
        _setup_pane(tmp_path, slug="other")
        count = record_failure(str(tmp_path), "nonexistent", "abc123")
        assert count == 0

    def test_metadata_survives_in_pane(self, tmp_path: Path) -> None:
        _setup_pane(tmp_path)
        sr = str(tmp_path)
        record_failure(sr, "test-slug", "h1")
        record_failure(sr, "test-slug", "h1")
        pane = get_pane(sr, "test-slug")
        assert pane is not None
        assert pane["failure_hashes"] == {"h1": 2}

    def test_threshold_constant(self) -> None:
        assert CIRCUIT_BREAKER_THRESHOLD == 3


class TestCircuitBreakerInIsDone:
    """Test that _is_done's Signal 5 triggers circuit breaker on repeated output."""

    def test_triggers_after_threshold(self, tmp_path: Path) -> None:
        """Simulate output cycling: error → work → error → work → error (3x)."""
        _setup_pane(tmp_path)
        sr = str(tmp_path)
        (tmp_path / STATE_DIR / "done").mkdir(parents=True, exist_ok=True)

        pane_record = get_pane(sr, "test-slug")
        stable_state: dict = {}

        error_output = "FAIL: test_foo.py\nAssertionError: 1 != 2"
        working_output = "Running fix attempt..."

        # Tick 1: error — first observation, count=1
        stable_state["last_output"] = error_output
        assert (
            _is_done(sr, "test-slug", pane_record=pane_record, _stable_state=stable_state) is False
        )

        # Tick 2: working — different hash, count=1
        stable_state["last_output"] = working_output
        assert (
            _is_done(sr, "test-slug", pane_record=pane_record, _stable_state=stable_state) is False
        )

        # Tick 3: error again — count=2
        stable_state["last_output"] = error_output
        assert (
            _is_done(sr, "test-slug", pane_record=pane_record, _stable_state=stable_state) is False
        )

        # Tick 4: working
        stable_state["last_output"] = working_output
        assert (
            _is_done(sr, "test-slug", pane_record=pane_record, _stable_state=stable_state) is False
        )

        # Tick 5: error — count=3, triggers circuit breaker
        stable_state["last_output"] = error_output
        assert (
            _is_done(sr, "test-slug", pane_record=pane_record, _stable_state=stable_state) is True
        )

        pane = get_pane(sr, "test-slug")
        assert pane is not None
        assert pane["state"] == "failed"
        assert pane.get("circuit_breaker")

    def test_no_trigger_below_threshold(self, tmp_path: Path) -> None:
        """Two repeated errors shouldn't trigger the circuit breaker."""
        _setup_pane(tmp_path)
        sr = str(tmp_path)
        (tmp_path / STATE_DIR / "done").mkdir(parents=True, exist_ok=True)

        pane_record = get_pane(sr, "test-slug")
        stable_state: dict = {}

        error_output = "ERROR: something broke"
        other_output = "trying again..."

        for output in [error_output, other_output, error_output]:
            stable_state["last_output"] = output
            result = _is_done(sr, "test-slug", pane_record=pane_record, _stable_state=stable_state)
            assert result is False

        pane = get_pane(sr, "test-slug")
        assert pane is not None
        assert pane["state"] == "active"

    def test_same_output_consecutive_ticks_not_counted(self, tmp_path: Path) -> None:
        """Same output on consecutive ticks (agent idle) should not increment."""
        _setup_pane(tmp_path)
        sr = str(tmp_path)
        (tmp_path / STATE_DIR / "done").mkdir(parents=True, exist_ok=True)

        pane_record = get_pane(sr, "test-slug")
        stable_state: dict = {}

        error_output = "ERROR: stuck"

        # Same output 10 times — hash never changes between ticks, only recorded once
        for _ in range(10):
            stable_state["last_output"] = error_output
            result = _is_done(sr, "test-slug", pane_record=pane_record, _stable_state=stable_state)
        assert result is False

        pane = get_pane(sr, "test-slug")
        assert pane is not None
        assert pane["state"] == "active"

    def test_hash_uses_more_than_last_five_lines(self, tmp_path: Path) -> None:
        """Alternating states with the same last 5 lines should still be distinguished."""
        _setup_pane(tmp_path)
        sr = str(tmp_path)
        pane_record = get_pane(sr, "test-slug")
        stable_state: dict = {}

        shared_tail = "\n".join(f"shared line {idx}" for idx in range(1, 6))
        output_a = "context A line 1\ncontext A line 2\n" + shared_tail
        output_b = "context B line 1\ncontext B line 2\n" + shared_tail

        assert _circuit_breaker_fingerprint(output_a) != _circuit_breaker_fingerprint(output_b)

        for output in [output_a, output_b, output_a, output_b]:
            stable_state["current_output"] = output
            assert (
                _is_done(
                    sr,
                    "test-slug",
                    pane_record=pane_record,
                    _stable_state=stable_state,
                )
                is False
            )

        stable_state["current_output"] = output_a
        assert (
            _is_done(
                sr,
                "test-slug",
                pane_record=pane_record,
                _stable_state=stable_state,
            )
            is True
        )

        pane = get_pane(sr, "test-slug")
        assert pane is not None
        assert pane["state"] == "failed"
