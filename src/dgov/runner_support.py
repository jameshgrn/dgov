"""Small support adapters for EventDagRunner dependencies."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_runner_project_config(session_root: str) -> Any:
    from dgov.config import load_project_config

    return load_project_config(session_root)


def reset_runner_plan_state(session_root: str, plan_name: str) -> None:
    from dgov.persistence import reset_plan_state

    reset_plan_state(session_root, plan_name)


def current_runner_source() -> str:
    from dgov.run_source import current_run_source

    return current_run_source()


def latest_runner_run_start_ids(events: list[dict[str, object]]) -> dict[str, int]:
    from dgov.live_state import latest_run_start_ids

    return latest_run_start_ids(events)


def deployed_units(session_root: str, dag_name: str) -> tuple[str, ...]:
    from dgov import deploy_log

    return tuple(record.unit for record in deploy_log.read(session_root, dag_name))


def deploy_records_by_unit(session_root: str, dag_name: str) -> dict[str, Any]:
    from dgov import deploy_log

    return {record.unit: record for record in deploy_log.read(session_root, dag_name)}


def effective_sop_set_hash(session_root: str) -> str:
    from dgov.sop_bundler import compute_sop_set_hash, load_sops

    sops_dir = Path(session_root) / ".dgov" / "sops"
    try:
        effective_sops = load_sops(sops_dir)
        return compute_sop_set_hash(effective_sops) if effective_sops else ""
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("Effective SOP bundle could not be loaded at dispatch: %s", exc)
        return ""


def summarize_runner_evidence(overlap_evidence: Iterable[Any]) -> str:
    from dgov.semantic_settlement import summarize_evidence

    return summarize_evidence(overlap_evidence)
