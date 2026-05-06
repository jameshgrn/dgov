"""Tests for tool-call audit aggregation."""

from __future__ import annotations

import pytest

from dgov.tool_audit import summarize_tool_events

pytestmark = pytest.mark.unit


def _worker_log(
    event_id: int,
    log_type: str,
    content: object,
    *,
    plan_name: str = "plan-a",
) -> dict[str, object]:
    return {
        "id": event_id,
        "event": "worker_log",
        "pane": "pane-a",
        "task_slug": "task-a",
        "plan_name": plan_name,
        "log_type": log_type,
        "content": content,
    }


def test_summarize_tool_events_handles_historical_call_result_shape() -> None:
    summary = summarize_tool_events([
        _worker_log(1, "call", {"tool": "read_file", "args": {}}),
        _worker_log(2, "result", {"tool": "read_file", "status": "success"}),
        _worker_log(3, "call", {"tool": "edit_file", "args": {}}),
        _worker_log(4, "result", {"tool": "edit_file", "status": "failed"}),
    ])

    assert summary.total_calls == 2
    assert summary.total_successes == 1
    assert summary.total_failures == 1
    assert [row.tool for row in summary.rows] == ["edit_file", "read_file"]
    edit_row = summary.rows[0]
    assert edit_row.calls == 1
    assert edit_row.failures == 1
    assert edit_row.failure_rate == 1.0


def test_summarize_tool_events_aggregates_enriched_result_fields() -> None:
    summary = summarize_tool_events([
        _worker_log(1, "call", {"tool": "read_file", "role": "worker"}),
        _worker_log(
            2,
            "result",
            {
                "tool": "read_file",
                "role": "worker",
                "status": "success",
                "result_chars": 100,
                "raw_result_chars": 100,
                "duration_ms": 12.5,
            },
        ),
        _worker_log(3, "call", {"tool": "read_file", "role": "worker"}),
        _worker_log(
            4,
            "result",
            {
                "tool": "read_file",
                "role": "worker",
                "status": "failed",
                "error_kind": "not_found",
                "result_chars": 50,
                "raw_result_chars": 500,
                "result_clipped": True,
                "duration_ms": 7.5,
            },
        ),
    ])

    row = summary.rows[0]
    assert row.tool == "read_file"
    assert row.calls == 2
    assert row.successes == 1
    assert row.failures == 1
    assert row.clipped_results == 1
    assert row.average_result_chars == 75
    assert row.average_raw_result_chars == 300
    assert row.average_duration_ms == 10
    assert row.roles == ("worker",)
    assert row.top_error_kind == "not_found"


def test_summarize_tool_events_filters_by_plan_and_role() -> None:
    summary = summarize_tool_events(
        [
            _worker_log(1, "call", {"tool": "read_file", "role": "worker"}, plan_name="plan-a"),
            _worker_log(2, "call", {"tool": "grep", "role": "researcher"}, plan_name="plan-a"),
            _worker_log(3, "call", {"tool": "edit_file", "role": "worker"}, plan_name="plan-b"),
        ],
        plan_name="plan-a",
        role="worker",
    )

    assert summary.total_calls == 1
    assert summary.plan_name == "plan-a"
    assert summary.role == "worker"
    assert summary.rows[0].tool == "read_file"


def test_summarize_tool_events_orders_by_call_count_then_tool_name() -> None:
    summary = summarize_tool_events([
        _worker_log(1, "call", {"tool": "z_tool"}),
        _worker_log(2, "call", {"tool": "a_tool"}),
        _worker_log(3, "call", {"tool": "a_tool"}),
        _worker_log(4, "call", {"tool": "b_tool"}),
        _worker_log(5, "call", {"tool": "b_tool"}),
    ])

    assert [row.tool for row in summary.rows] == ["a_tool", "b_tool", "z_tool"]
