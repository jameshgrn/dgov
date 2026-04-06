"""Append-only deploy log — tracks which plan units have shipped.

Single JSONL file at `.dgov/plans/deployed.jsonl`, filtered by plan name.
Never mutate or delete entries. Malformed lines are skipped with a warning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("dgov.deploy_log")

_LOG_FILENAME = "deployed.jsonl"


@dataclass(frozen=True)
class DeployRecord:
    """One shipped unit."""

    plan: str
    unit: str
    sha: str
    ts: str


def _log_path(project_root: str) -> Path:
    return Path(project_root) / ".dgov" / "plans" / _LOG_FILENAME


def append(
    project_root: str,
    plan_name: str,
    unit_id: str,
    commit_sha: str,
    timestamp: str | None = None,
) -> None:
    """Append one deploy record. Creates parent dirs if needed."""
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = {"plan": plan_name, "unit": unit_id, "sha": commit_sha, "ts": ts}
    path = _log_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def read(project_root: str, plan_name: str) -> list[DeployRecord]:
    """Read all deploy records for a given plan. Skips malformed lines."""
    path = _log_path(project_root)
    if not path.exists():
        return []
    records: list[DeployRecord] = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed line %d in %s", lineno, path)
            continue
        if data.get("plan") != plan_name:
            continue
        records.append(
            DeployRecord(
                plan=data.get("plan", ""),
                unit=data.get("unit", ""),
                sha=data.get("sha", ""),
                ts=data.get("ts", ""),
            )
        )
    return records


def is_deployed(project_root: str, plan_name: str, unit_id: str) -> bool:
    """Check if a specific unit has been deployed."""
    return any(r.unit == unit_id for r in read(project_root, plan_name))
