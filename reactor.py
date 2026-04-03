"""Reactor: Stateless reflex arc. Maps failure to repair prompt. <150 lines."""

from __future__ import annotations
import re
from dataclasses import dataclass

REPAIR_TEMPLATES = {
    "SENTRUX_RULE": """Role: Systems Engineer. Task: Fix structural violation.
Constraint: {constraint}. Violation: {details}.
Objective: Modify code to pass sentrux gate. Do not add features.""",
    "TEST_FAIL": """Role: Test Engineer. Task: Fix failing test.
Test: {test_name}. Error: {error}.
Objective: Make test pass with minimal change.""",
    "TIMEOUT": """Role: Performance Engineer. Task: Reduce latency.
Current: {latency_ms}ms. Threshold: {threshold_ms}ms.
Objective: Optimize hot path. No architectural changes.""",
    "UNKNOWN": """Role: Systems Engineer. Task: Investigate failure.
Failure: {error}. Context: {context}.
Objective: Produce minimal fix.""",
}

VIOLATION_PATTERNS = [
    (r"cyclomatic.*>(\d+)", "SENTRUX_RULE", "complexity"),
    (r"lines.*>(\d+)", "SENTRUX_RULE", "bloat"),
    (r"test.*fail", "TEST_FAIL", "test"),
    (r"timeout", "TIMEOUT", "performance"),
]


@dataclass(frozen=True)
class Failure:
    error: str
    context: str
    attempt_id: str


@dataclass(frozen=True)
class RepairPrompt:
    template_type: str
    prompt: str
    target_constraint: str | None


def triage(failure: Failure) -> RepairPrompt:
    """Map failure to repair template. Pure function."""
    err = failure.error.lower()
    for pattern, template_type, constraint in VIOLATION_PATTERNS:
        if re.search(pattern, err):
            tmpl = REPAIR_TEMPLATES[template_type]
            prompt = tmpl.format(
                constraint=constraint,
                details=failure.error,
                error=failure.error,
                context=failure.context,
            )
            return RepairPrompt(template_type, prompt, constraint)
    # Fallback
    prompt = REPAIR_TEMPLATES["UNKNOWN"].format(
        error=failure.error, context=failure.context
    )
    return RepairPrompt("UNKNOWN", prompt, None)


def formulate_repair(failure: Failure, retry_count: int = 0) -> dict:
    """Generate repair task spec."""
    rp = triage(failure)
    return {
        "task_id": f"repair-{failure.attempt_id}-{retry_count}",
        "parent_attempt": failure.attempt_id,
        "retry_count": retry_count,
        "prompt": rp.prompt,
        "constraint": rp.target_constraint,
        "template_type": rp.template_type,
    }


def should_retry(failure: Failure, history: list[dict], max_retries: int = 2) -> bool:
    """Fail-closed: only retry if we haven't hit limit AND failure is deterministic."""
    if len(history) >= max_retries:
        return False
    # Don't retry transient/unknown failures deterministically
    if "network" in failure.error.lower() or "timeout" in failure.error.lower():
        return False
    return True


def main() -> int:
    """CLI: stdin failure -> stdout repair prompt."""
    import sys
    import json

    data = sys.stdin.read()
    if not data:
        return 0
    fail = json.loads(data)
    failure = Failure(
        fail.get("error", ""),
        fail.get("context", ""),
        fail.get("attempt_id", "unknown"),
    )
    repair = formulate_repair(failure, fail.get("retry_count", 0))
    print(json.dumps(repair))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
