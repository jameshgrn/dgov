"""Small support adapters for EventDagRunner dependencies."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dgov.actions import DagAction
from dgov.dag_parser import DagTaskSpec
from dgov.settlement_flow import IntegrationRiskRecord
from dgov.types import Worktree

logger = logging.getLogger(__name__)
_TEST_FAILURE_COMMAND_RE = re.compile(r"^Test failure from `(?P<command>[^`]+)`:", re.MULTILINE)
DispatchCoroutine = Coroutine[Any, Any, list[DagAction]]
DispatchJob = tuple[str, DispatchCoroutine]
KernelActionHandler = Callable[
    [Any, list[DispatchJob], list[DagAction]],
    Awaitable[bool | None],
]


def normalize_scope_path(path: str) -> str:
    return path.strip().lstrip("./").rstrip("/")


def verify_test_targets(task: DagTaskSpec, test_dir: str) -> tuple[str, ...]:
    test_root = normalize_scope_path(test_dir)
    if not test_root:
        return ()
    claimed = (
        *task.files.create,
        *task.files.edit,
        *task.files.touch,
        *task.files.read,
    )
    return tuple(
        dict.fromkeys(
            norm
            for path in claimed
            if (norm := normalize_scope_path(path))
            and (norm == test_root or norm.startswith(f"{test_root}/"))
        )
    )


def parse_test_failure_command(error: str) -> str | None:
    match = _TEST_FAILURE_COMMAND_RE.search(error)
    if match is None:
        return None
    return match.group("command").strip() or None


def summarize_risk_evidence(risk_record: IntegrationRiskRecord) -> str:
    return summarize_runner_evidence(risk_record.overlap_evidence)


@dataclass(frozen=True, slots=True)
class RetryProvenance:
    """Marks a TaskContext as awaiting dispatch as a retry of a prior run."""

    from_dispatch_run_id: str


@dataclass(frozen=True, slots=True)
class ForkProvenance:
    """Marks a TaskContext as awaiting fork dispatch after iteration exhaustion."""

    from_dispatch_run_id: str


DispatchProvenance = RetryProvenance | ForkProvenance | None


@dataclass
class TaskContext:
    """Per-task runtime state tracked by EventDagRunner."""

    pane_slug: str | None = None
    attempts: int = 0
    error: str | None = None
    start_time: float | None = None
    duration: float | None = None
    worktree: Worktree | None = None
    worker_task: Any = None
    rejected_worktree: Worktree | None = None
    call_count: int = 0
    fork_depth: int = 0
    review_file_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    current_dispatch_run_id: str | None = None
    provenance: DispatchProvenance = None


@dataclass(frozen=True)
class RunLoopStep:
    actions: list[DagAction]
    final: dict[str, str] | None = None
    stop: bool = False


def load_runner_project_config(session_root: str) -> Any:
    from dgov.config import load_project_config

    return load_project_config(session_root)


def reset_runner_plan_state(session_root: str, plan_name: str) -> None:
    from dgov.persistence import reset_plan_state

    reset_plan_state(session_root, plan_name)


def current_runner_source() -> str:
    from dgov.run_source import current_run_source

    return current_run_source()


def deployed_units(session_root: str, dag_name: str) -> tuple[str, ...]:
    from dgov import deploy_log

    return tuple(record.unit for record in deploy_log.read(session_root, dag_name))


def deploy_records_by_unit(session_root: str, dag_name: str) -> dict[str, Any]:
    from dgov import deploy_log

    return {record.unit: record for record in deploy_log.read(session_root, dag_name)}


def latest_deploy_record_for_units(
    session_root: str, dag_name: str, units: Iterable[str]
) -> Any | None:
    from dgov import deploy_log

    wanted = set(units)
    latest = None
    for record in deploy_log.read(session_root, dag_name):
        if record.unit in wanted:
            latest = record
    return latest


def latest_runner_run_start_ids(events: list[dict[str, object]]) -> dict[str, int]:
    from dgov.live_state import latest_run_start_ids

    return latest_run_start_ids(events)


def latest_dispatch_run_id(session_root: str, plan_id: str, unit_slug: str) -> str | None:
    from dgov.persistence.dispatch_runs import list_dispatch_runs

    rows = list_dispatch_runs(session_root, plan_id=plan_id, unit_slug=unit_slug)
    return rows[-1]["id"] if rows else None


def save_runner_dispatch_run(session_root: str, dispatch_run: Any) -> None:
    from dgov.persistence.dispatch_runs import save_dispatch_run

    save_dispatch_run(session_root, dispatch_run)


def load_runner_dispatch_run(session_root: str, dispatch_run_id: str) -> Any | None:
    from dgov.dispatch_run import _dispatch_run_from_row_dict
    from dgov.persistence.dispatch_runs import get_dispatch_run

    row = get_dispatch_run(session_root, dispatch_run_id)
    return _dispatch_run_from_row_dict(row) if row else None


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
