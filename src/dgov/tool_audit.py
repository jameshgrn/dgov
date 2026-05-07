"""Tool-call audit summaries derived from worker_log events."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _ToolAccumulator:
    tool: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    policy_failures: int = 0
    clipped_results: int = 0
    total_result_chars: int = 0
    total_raw_result_chars: int = 0
    total_duration_ms: float = 0.0
    duration_count: int = 0
    roles: set[str] = field(default_factory=set)
    error_kinds: Counter[str] = field(default_factory=Counter)

    def add_call(self, content: dict[str, Any]) -> None:
        self.calls += 1
        role = content.get("role")
        if isinstance(role, str) and role:
            self.roles.add(role)

    def add_result(self, content: dict[str, Any]) -> None:
        role = content.get("role")
        if isinstance(role, str) and role:
            self.roles.add(role)

        status = content.get("status")
        if status == "failed":
            self.failures += 1
            error_kind = content.get("error_kind")
            if isinstance(error_kind, str) and error_kind:
                self.error_kinds[error_kind] += 1
                if error_kind == "policy_blocked":
                    self.policy_failures += 1
        elif status == "success":
            self.successes += 1

        if content.get("result_clipped") is True:
            self.clipped_results += 1
        self.total_result_chars += _int_value(content.get("result_chars"))
        self.total_raw_result_chars += _int_value(content.get("raw_result_chars"))
        duration_ms = _float_value(content.get("duration_ms"))
        if duration_ms is not None:
            self.total_duration_ms += duration_ms
            self.duration_count += 1

    def freeze(self) -> ToolAuditRow:
        return ToolAuditRow(
            tool=self.tool,
            calls=self.calls,
            successes=self.successes,
            failures=self.failures,
            policy_failures=self.policy_failures,
            clipped_results=self.clipped_results,
            total_result_chars=self.total_result_chars,
            total_raw_result_chars=self.total_raw_result_chars,
            total_duration_ms=self.total_duration_ms,
            duration_count=self.duration_count,
            roles=tuple(sorted(self.roles)),
            error_kinds=tuple(
                sorted(self.error_kinds.items(), key=lambda item: (-item[1], item[0]))
            ),
        )


@dataclass(frozen=True)
class ToolAuditRow:
    tool: str
    calls: int
    successes: int = 0
    failures: int = 0
    policy_failures: int = 0
    clipped_results: int = 0
    total_result_chars: int = 0
    total_raw_result_chars: int = 0
    total_duration_ms: float = 0.0
    duration_count: int = 0
    roles: tuple[str, ...] = ()
    error_kinds: tuple[tuple[str, int], ...] = ()

    @property
    def result_count(self) -> int:
        return self.successes + self.failures

    @property
    def failure_rate(self) -> float:
        denominator = max(self.calls, self.result_count)
        return self.failures / denominator if denominator else 0.0

    @property
    def average_result_chars(self) -> float:
        return self.total_result_chars / self.result_count if self.result_count else 0.0

    @property
    def average_raw_result_chars(self) -> float:
        return self.total_raw_result_chars / self.result_count if self.result_count else 0.0

    @property
    def average_duration_ms(self) -> float:
        return self.total_duration_ms / self.duration_count if self.duration_count else 0.0

    @property
    def top_error_kind(self) -> str | None:
        return self.error_kinds[0][0] if self.error_kinds else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "calls": self.calls,
            "successes": self.successes,
            "failures": self.failures,
            "failure_rate": self.failure_rate,
            "policy_failures": self.policy_failures,
            "clipped_results": self.clipped_results,
            "average_result_chars": self.average_result_chars,
            "average_raw_result_chars": self.average_raw_result_chars,
            "average_duration_ms": self.average_duration_ms,
            "roles": list(self.roles),
            "error_kinds": dict(self.error_kinds),
            "top_error_kind": self.top_error_kind,
        }


@dataclass(frozen=True)
class ToolAuditSummary:
    rows: tuple[ToolAuditRow, ...]
    plan_name: str | None = None
    role: str | None = None

    @property
    def total_calls(self) -> int:
        return sum(row.calls for row in self.rows)

    @property
    def total_successes(self) -> int:
        return sum(row.successes for row in self.rows)

    @property
    def total_failures(self) -> int:
        return sum(row.failures for row in self.rows)

    @property
    def total_clipped_results(self) -> int:
        return sum(row.clipped_results for row in self.rows)

    def as_dict(self, limit: int = 0) -> dict[str, Any]:
        rows = self.rows[:limit] if limit > 0 else self.rows
        return {
            "plan_name": self.plan_name,
            "role": self.role,
            "total_calls": self.total_calls,
            "total_successes": self.total_successes,
            "total_failures": self.total_failures,
            "total_clipped_results": self.total_clipped_results,
            "tools": [row.as_dict() for row in rows],
        }


def summarize_tool_events(
    events: list[dict[str, Any]],
    *,
    plan_name: str | None = None,
    role: str | None = None,
) -> ToolAuditSummary:
    accumulators: dict[str, _ToolAccumulator] = {}
    for event in events:
        if event.get("event") != "worker_log":
            continue
        if plan_name is not None and event.get("plan_name") != plan_name:
            continue
        log_type = event.get("log_type")
        if log_type not in ("call", "result"):
            continue
        content = event.get("content")
        if not isinstance(content, dict):
            continue
        if role is not None and content.get("role") != role:
            continue
        tool = content.get("tool")
        if not isinstance(tool, str) or not tool:
            continue

        accumulator = accumulators.setdefault(tool, _ToolAccumulator(tool=tool))
        if log_type == "call":
            accumulator.add_call(content)
        else:
            accumulator.add_result(content)

    rows = tuple(
        sorted(
            (accumulator.freeze() for accumulator in accumulators.values()),
            key=lambda row: (-row.calls, row.tool),
        )
    )
    return ToolAuditSummary(rows=rows, plan_name=plan_name, role=role)


def _int_value(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0


def _float_value(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None
