from __future__ import annotations

from dataclasses import dataclass

_ATTENTION_STATES = frozenset({"reviewed_pass", "reviewed_fail"})


@dataclass(frozen=True)
class DiagnosisFinding:
    name: str  # catalog entry name, e.g. "plan_claims_violation"
    intent_class: str  # "Project policy" or "Governance repair"
    evidence: str  # one-line description of what was observed
    next_action: str  # one-line typed next task
    do_not: str  # one-line warning


def check_plan_claims_violation(events: list[dict]) -> list[DiagnosisFinding]:
    """Return findings for recent settlement scope violations.

    Surfaces `review_fail` events whose `verdict` is `scope_violation` or
    `read_scope_violation`. One finding per failing task.
    """
    seen: set[tuple[str, str]] = set()
    findings: list[DiagnosisFinding] = []
    for ev in events:
        if ev.get("event") != "review_fail":
            continue
        verdict = ev.get("verdict", "")
        if verdict not in {"scope_violation", "read_scope_violation"}:
            continue
        plan_name = ev.get("plan_name", "")
        task_slug = ev.get("task_slug", "")
        key = (plan_name, task_slug)
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            DiagnosisFinding(
                name="plan_claims_violation",
                intent_class="Governance repair",
                evidence=f"`{plan_name}/{task_slug}` rejected with verdict `{verdict}`",
                next_action="Fix the plan's file claims or decompose the task; re-run.",
                do_not="Brute-force retry the same plan. Scope violations are terminal.",
            )
        )
    return findings


def check_stale_review_attention(
    live_tasks: list[dict],
    active_plan_names: frozenset[str],
) -> list[DiagnosisFinding]:
    """Return a finding when reviewed tasks are live only because history leaked."""
    stale_attention = [
        task
        for task in live_tasks
        if task.get("state") in _ATTENTION_STATES
        and task.get("plan_name")
        and task.get("plan_name") not in active_plan_names
    ]
    if not stale_attention:
        return []

    examples = ", ".join(
        f"{task.get('plan_name')}/{task.get('slug')} ({task.get('state')})"
        for task in stale_attention[:3]
    )
    suffix = "" if len(stale_attention) <= 3 else f", +{len(stale_attention) - 3} more"
    return [
        DiagnosisFinding(
            name="stale_review_attention",
            intent_class="Governance repair",
            evidence=(
                f"{len(stale_attention)} reviewed task(s) lack an active plan source: "
                f"{examples}{suffix}"
            ),
            next_action=(
                "Treat as lifecycle hygiene: restore/rerun the plan source or append a "
                "terminal lifecycle event through a repair path."
            ),
            do_not="Edit state.db or task rows by hand; event history is the source of truth.",
        )
    ]


CHECKS = (check_plan_claims_violation,)
"""Ordered registry for raw-event checks. Each check returns
`list[DiagnosisFinding]`. Keep names in sync with the Failure-to-task catalog
in `.dgov/governor.md`."""
