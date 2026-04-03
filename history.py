"""Report history tracking for comparing lint runs over time."""

from __future__ import annotations

import json
import time
from pathlib import Path

from scilint.cache import CACHE_ROOT

_HISTORY_DIR_NAME = "report_history"
_HISTORY_FILE = "runs.jsonl"


def _history_dir() -> Path:
    return CACHE_ROOT / _HISTORY_DIR_NAME


def _history_path() -> Path:
    return _history_dir() / _HISTORY_FILE


def _finding_key(file_path: str, rule_id: str, source_line: int, match: str) -> str:
    """Create a stable key for a finding to enable comparison across runs."""
    return f"{file_path}|{rule_id}|{source_line}|{match}"


def save_run(
    findings: list[tuple[Path, list[dict]]],
    *,
    run_id: str | None = None,
) -> dict:
    """Save a lint run to history.

    Args:
        findings: List of (file_path, list_of_finding_dicts) tuples.
        run_id: Optional custom run ID. If None, generates one from timestamp.

    Returns:
        The saved run record.
    """
    now = time.time()
    run_id = run_id or f"run_{int(now)}"

    # Aggregate counts
    total = 0
    by_severity: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_file: dict[str, dict] = {}

    all_finding_keys: list[str] = []

    for file_path, file_findings in findings:
        file_str = str(file_path)
        file_stats: dict = {
            "total": 0,
            "by_severity": {},
            "by_rule": {},
            "findings": [],
        }

        for f in file_findings:
            total += 1
            file_stats["total"] += 1

            sev = f["severity"]
            by_severity[sev] = by_severity.get(sev, 0) + 1
            file_stats["by_severity"][sev] = file_stats["by_severity"].get(sev, 0) + 1

            rule = f["rule_id"]
            by_rule[rule] = by_rule.get(rule, 0) + 1
            file_stats["by_rule"][rule] = file_stats["by_rule"].get(rule, 0) + 1

            key = _finding_key(file_str, rule, f["source_line"], f["match"])
            all_finding_keys.append(key)
            file_stats["findings"].append({
                "rule_id": rule,
                "severity": sev,
                "source_line": f["source_line"],
                "match": f["match"],
                "title": f["title"],
                "key": key,
            })

        if file_stats["total"] > 0:
            by_file[file_str] = file_stats

    run_record = {
        "run_id": run_id,
        "timestamp": now,
        "total": total,
        "by_severity": by_severity,
        "by_rule": by_rule,
        "by_file": by_file,
        "finding_keys": all_finding_keys,
    }

    # Append to JSONL file
    hpath = _history_path()
    hpath.parent.mkdir(parents=True, exist_ok=True)
    with hpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps(run_record) + "\n")

    return run_record


def load_history() -> list[dict]:
    """Load all run records from history."""
    hpath = _history_path()
    if not hpath.exists():
        return []

    runs = []
    for line in hpath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return runs


def get_run(run_id: str) -> dict | None:
    """Get a specific run by ID."""
    for run in load_history():
        if run["run_id"] == run_id:
            return run
    return None


def get_history_for_file(file_path: str) -> list[dict]:
    """Get all runs that include findings for a specific file."""
    return [r for r in load_history() if file_path in r.get("by_file", {})]


def compare_runs(run1_id: str, run2_id: str) -> dict:
    """Compare two runs and return differences.

    Returns:
        Dict with 'new', 'resolved', 'persistent' finding keys and summary stats.
    """
    run1 = get_run(run1_id)
    run2 = get_run(run2_id)

    if run1 is None:
        raise ValueError(f"Run not found: {run1_id}")
    if run2 is None:
        raise ValueError(f"Run not found: {run2_id}")

    keys1 = set(run1.get("finding_keys", []))
    keys2 = set(run2.get("finding_keys", []))

    new_keys = keys2 - keys1
    resolved_keys = keys1 - keys2
    persistent_keys = keys1 & keys2

    # Build finding lookup for new/resolved
    def _find_finding(run: dict, key: str) -> dict | None:
        for file_path, file_data in run.get("by_file", {}).items():
            for finding in file_data.get("findings", []):
                if finding["key"] == key:
                    return {**finding, "file": file_path}
        return None

    new_findings = []
    for key in sorted(new_keys):
        f = _find_finding(run2, key)
        if f:
            new_findings.append(f)

    resolved_findings = []
    for key in sorted(resolved_keys):
        f = _find_finding(run1, key)
        if f:
            resolved_findings.append(f)

    return {
        "run1": run1_id,
        "run2": run2_id,
        "run1_timestamp": run1["timestamp"],
        "run2_timestamp": run2["timestamp"],
        "run1_total": run1["total"],
        "run2_total": run2["total"],
        "delta": run2["total"] - run1["total"],
        "new": new_findings,
        "resolved": resolved_findings,
        "persistent_count": len(persistent_keys),
        "new_count": len(new_keys),
        "resolved_count": len(resolved_keys),
    }


def get_previous_run(current_run_id: str) -> dict | None:
    """Get the run immediately before the current one."""
    runs = load_history()
    for i, run in enumerate(runs):
        if run["run_id"] == current_run_id and i > 0:
            return runs[i - 1]
    return None


def get_comparison_summary_text(comparison: dict) -> str:
    """Generate a text summary of the comparison."""
    parts = []
    new_count = comparison["new_count"]
    resolved_count = comparison["resolved_count"]
    delta = comparison["delta"]

    if new_count == 0 and resolved_count == 0:
        return "No changes since last run."

    if new_count > 0:
        parts.append(f"{new_count} new issue{'s' if new_count != 1 else ''}")
    if resolved_count > 0:
        parts.append(f"{resolved_count} resolved")
    if delta != 0:
        direction = "increase" if delta > 0 else "decrease"
        parts.append(f"net {direction} of {abs(delta)}")

    return ", ".join(parts) + " since last run."


def get_comparison_summary_html(comparison: dict) -> str:
    """Generate an HTML summary of the comparison."""
    new_count = comparison["new_count"]
    resolved_count = comparison["resolved_count"]
    delta = comparison["delta"]

    if new_count == 0 and resolved_count == 0:
        return '<div class="trend-summary trend-unchanged">✓ No changes since last run.</div>'

    parts = ['<div class="trend-summary">']
    if new_count > 0:
        parts.append(
            f'<span class="trend-new">⬆ {new_count} new issue{"s" if new_count != 1 else ""}</span>'
        )
    if resolved_count > 0:
        parts.append(
            f'<span class="trend-resolved">⬇ {resolved_count} resolved</span>'
        )
    if delta != 0:
        direction = "increase" if delta > 0 else "decrease"
        css_class = "trend-worse" if delta > 0 else "trend-better"
        parts.append(
            f'<span class="{css_class}">Net {direction} of {abs(delta)}</span>'
        )
    parts.append("</div>")

    return " ".join(parts)
