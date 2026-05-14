from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiagnosisFinding:
    name: str  # catalog entry name, e.g. "archive_policy_drift"
    intent_class: str  # "Project policy" or "Governance repair"
    evidence: str  # one-line description of what was observed
    next_action: str  # one-line typed next task
    do_not: str  # one-line warning


def check_archive_policy_drift(project_root: Path) -> list[DiagnosisFinding]:
    """Return a finding if `.dgov/plans/archive/` is git-ignored.

    Probe path does not need to exist. `git check-ignore -v` exits 0 when
    the path is ignored and prints the matching rule on stdout.
    """
    probe = project_root / ".dgov" / "plans" / "archive" / "_probe" / "_root.toml"
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-v", str(probe)],
            cwd=project_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    rule = result.stdout.strip().splitlines()[0] if result.stdout.strip() else "(no rule output)"
    return [
        DiagnosisFinding(
            name="archive_policy_drift",
            intent_class="Project policy",
            evidence=f"`.dgov/plans/archive/` is git-ignored — {rule}",
            next_action=(
                "Edit the repo's `.dgov/.gitignore` so durable plan archives"
                " are trackable; retry the finalization path."
            ),
            do_not=(
                "Rerun the landed worker task to recover bookkeeping."
                " Worker-task completion and governor finalization are separate states."
            ),
        )
    ]


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


CHECKS = (check_archive_policy_drift, check_plan_claims_violation)
"""Ordered registry. Each check returns `list[DiagnosisFinding]`. Keep names
in sync with the Failure-to-task catalog in `.dgov/governor.md`."""
