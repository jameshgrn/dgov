"""Review-then-fix pipeline: dispatch review workers, parse findings, dispatch fix workers."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from dgov.persistence import emit_event
from dgov.waiter import wait_for_slugs

logger = logging.getLogger(__name__)

# -- Severity ordering --

_SEVERITY_LEVELS = {"critical": 0, "medium": 1, "low": 2}

REVIEW_PROMPT_TEMPLATE = """\
Review the following file(s) for bugs, logic errors, security issues, and code quality problems.

Targets: {targets}

Output your findings as a JSON array. Each finding must have these fields:
- "file": string (file path relative to repo root)
- "line": integer (line number, 0 if unknown)
- "severity": "critical" | "medium" | "low"
- "category": string (e.g. "bug", "security", "logic", "style", "performance")
- "description": string (what the problem is)
- "suggested_fix": string (how to fix it, or "" if unclear)

Output ONLY the JSON array, no markdown fences, no commentary.
Example:
[{{"file": "src/foo.py", "line": 42, "severity": "medium",
  "category": "bug", "description": "Off-by-one in loop",
  "suggested_fix": "Change < to <="}}]

If no issues found, output: []
"""

FIX_PROMPT_TEMPLATE = """\
Fix the following issues in {file_path}:

{findings_text}

For each finding:
1. Read the file
2. Apply the fix at the specified line
3. Run `uv run ruff check {file_path}` and `uv run ruff format {file_path}`
4. Commit your changes with a descriptive message

Do NOT modify any other files. Do NOT create documentation files.
"""


@dataclass(frozen=True)
class ReviewFinding:
    file: str
    line: int
    severity: str
    category: str
    description: str
    suggested_fix: str

    def dedup_key(self) -> tuple[str, int, str]:
        return (self.file, self.line, self.category)


def parse_review_findings(output: str) -> list[ReviewFinding]:
    """Parse structured JSON findings from review agent output.

    Expects agent to output a JSON array of finding objects.
    Gracefully handles malformed output by returning an empty list.
    """
    if not output or not output.strip():
        return []

    text = output.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        lines = text.splitlines()
        # Remove first line (```json or ```) and last line (```)
        inner = []
        in_fence = False
        for line in lines:
            if line.strip().startswith("```") and not in_fence:
                in_fence = True
                continue
            if line.strip() == "```" and in_fence:
                break
            if in_fence:
                inner.append(line)
        text = "\n".join(inner) if inner else text

    # Try to find a JSON array in the output
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        logger.warning("No JSON array found in review output")
        return []

    json_text = text[start : end + 1]

    try:
        raw = json.loads(json_text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse JSON from review output")
        return []

    if not isinstance(raw, list):
        return []

    findings = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            findings.append(
                ReviewFinding(
                    file=str(item.get("file", "")),
                    line=int(item.get("line", 0)),
                    severity=str(item.get("severity", "low")),
                    category=str(item.get("category", "")),
                    description=str(item.get("description", "")),
                    suggested_fix=str(item.get("suggested_fix", "")),
                )
            )
        except (TypeError, ValueError):
            continue

    return findings


def _deduplicate(findings: list[ReviewFinding]) -> list[ReviewFinding]:
    """Remove duplicate findings (same file+line+category)."""
    seen: set[tuple[str, int, str]] = set()
    result = []
    for f in findings:
        key = f.dedup_key()
        if key not in seen:
            seen.add(key)
            result.append(f)
    return result


def _filter_by_severity(
    findings: list[ReviewFinding], threshold: str = "medium"
) -> list[ReviewFinding]:
    """Filter findings by severity threshold.

    'critical' = only critical
    'medium' = critical + medium
    'low' = all
    """
    cutoff = _SEVERITY_LEVELS.get(threshold, 1)
    return [f for f in findings if _SEVERITY_LEVELS.get(f.severity, 2) <= cutoff]


def _group_by_file(findings: list[ReviewFinding]) -> dict[str, list[ReviewFinding]]:
    """Group findings by file path."""
    groups: dict[str, list[ReviewFinding]] = {}
    for f in findings:
        groups.setdefault(f.file, []).append(f)
    return groups


def run_review_fix_pipeline(
    project_root: str,
    targets: list[str],
    review_agent: str = "claude",
    fix_agent: str = "claude",
    session_root: str | None = None,
    auto_approve: bool = False,
    severity_threshold: str = "medium",
    timeout: int = 600,
) -> dict:
    """Run the review-then-fix pipeline.

    Phase 1: Dispatch review workers, collect and parse findings.
    Phase 2 (if auto_approve): Dispatch fix workers per file.
    Phase 3 (if auto_approve): Merge fix branches sequentially.

    Returns summary dict with findings_count, fixed_count, etc.
    """
    from dgov.lifecycle import close_worker_pane, create_worker_pane
    from dgov.merger import merge_worker_pane
    from dgov.status import capture_worker_output

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)

    # Validate targets exist
    missing = [
        t for t in targets if not Path(t).exists() and not (Path(project_root) / t).exists()
    ]
    if missing:
        emit_event(
            session_root,
            "review_fix_started",
            "pipeline",
            targets=targets,
            error=f"Target(s) not found: {', '.join(missing)}",
        )
        return {
            "error": f"Target(s) not found: {', '.join(missing)}",
            "phase": "validation",
            "findings_count": 0,
        }

    emit_event(session_root, "review_fix_started", "pipeline", targets=targets)

    # -- PHASE 1: REVIEW --
    review_slugs: list[str] = []
    for i, target in enumerate(targets):
        slug = f"review-{i:03d}-{Path(target).stem}"[:50]
        prompt = REVIEW_PROMPT_TEMPLATE.format(targets=target)
        try:
            pane = create_worker_pane(
                project_root=project_root,
                prompt=prompt,
                agent=review_agent,
                permission_mode="bypassPermissions",
                slug=slug,
                session_root=session_root,
            )
            review_slugs.append(pane.slug)
        except Exception as e:
            logger.warning("Failed to create review worker for %s: %s", target, e)

    # Wait for all review workers
    wait_for_slugs(session_root, review_slugs, timeout=timeout)

    # Capture output and parse findings
    all_findings: list[ReviewFinding] = []
    for slug in review_slugs:
        output = capture_worker_output(project_root, slug, lines=200, session_root=session_root)
        findings = parse_review_findings(output or "")
        all_findings.extend(findings)

        # Emit per-finding events
        for f in findings:
            emit_event(
                session_root,
                "review_fix_finding",
                slug,
                file=f.file,
                line=f.line,
                severity=f.severity,
                category=f.category,
            )

    # Close review workers
    for slug in review_slugs:
        close_worker_pane(project_root, slug, session_root=session_root, force=True)

    # Deduplicate and filter
    all_findings = _deduplicate(all_findings)
    filtered = _filter_by_severity(all_findings, severity_threshold)

    if not auto_approve:
        emit_event(
            session_root,
            "review_fix_completed",
            "pipeline",
            phase="review_only",
            findings_count=len(filtered),
        )
        return {
            "phase": "review_only",
            "findings_count": len(filtered),
            "findings": [asdict(f) for f in filtered],
            "all_findings_count": len(all_findings),
            "filtered_out": len(all_findings) - len(filtered),
        }

    if not filtered:
        emit_event(
            session_root,
            "review_fix_completed",
            "pipeline",
            phase="complete",
            findings_count=0,
        )
        return {
            "phase": "complete",
            "findings_count": 0,
            "fixed_count": 0,
            "merged_count": 0,
            "failed_count": 0,
            "test_status": "skipped",
        }

    # -- PHASE 2: FIX --
    grouped = _group_by_file(filtered)
    fix_slugs: list[str] = []

    for file_path, file_findings in grouped.items():
        slug = f"fix-{Path(file_path).stem}"[:50]
        findings_text = "\n".join(
            f"- Line {f.line}: [{f.severity}] {f.category}: {f.description}"
            + (f"\n  Suggested fix: {f.suggested_fix}" if f.suggested_fix else "")
            for f in file_findings
        )
        prompt = FIX_PROMPT_TEMPLATE.format(file_path=file_path, findings_text=findings_text)
        try:
            pane = create_worker_pane(
                project_root=project_root,
                prompt=prompt,
                agent=fix_agent,
                permission_mode="acceptEdits",
                slug=slug,
                session_root=session_root,
            )
            fix_slugs.append(pane.slug)
        except Exception as e:
            logger.warning("Failed to create fix worker for %s: %s", file_path, e)

    # Wait for all fix workers
    wait_for_slugs(session_root, fix_slugs, timeout=timeout)

    # -- PHASE 3: VALIDATE (merge + test) --
    merged_count = 0
    failed_count = 0
    test_failures: list[str] = []

    for slug in fix_slugs:
        merge_result = merge_worker_pane(project_root, slug, session_root=session_root)
        if "merged" in merge_result:
            merged_count += 1
            # Run targeted tests after each merge (only on changed files)
            import subprocess

            # Get files changed in this merge
            changed_r = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
                cwd=project_root,
                capture_output=True,
                text=True,
            )
            changed_files = (
                [f for f in changed_r.stdout.strip().splitlines() if f]
                if changed_r.returncode == 0
                else []
            )

            # Map changed source files to their test counterparts
            test_files: list[str] = []
            for f in changed_files:
                p = Path(f)
                if not p.parts or p.parts[0] != "src":
                    continue
                # src/dgov/foo.py -> tests/test_foo.py, tests/test_dgov_foo.py
                stem = p.stem
                pkg = p.parts[1] if len(p.parts) > 2 else ""
                candidates = [
                    Path("tests") / f"test_{stem}.py",
                    Path("tests") / f"test_{pkg}_{stem}.py",
                ]
                for c in candidates:
                    if c.exists():
                        test_files.append(str(c))
                        break

            if test_files:
                test_result = subprocess.run(
                    ["uv", "run", "pytest", "-q", "--tb=short", "-x", *test_files],
                    cwd=project_root,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            else:
                test_result = None
            if test_result is not None and test_result.returncode != 0:
                test_failures.append(slug)
        else:
            failed_count += 1

        # Close fix worker
        close_worker_pane(project_root, slug, session_root=session_root, force=True)

    test_status = "pass" if not test_failures else f"failures:{','.join(test_failures)}"

    emit_event(
        session_root,
        "review_fix_completed",
        "pipeline",
        phase="complete",
        findings_count=len(filtered),
        merged_count=merged_count,
        failed_count=failed_count,
    )

    return {
        "phase": "complete",
        "findings_count": len(filtered),
        "fixed_count": merged_count,
        "merged_count": merged_count,
        "failed_count": failed_count,
        "test_status": test_status,
    }
