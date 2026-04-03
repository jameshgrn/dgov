"""DAG file parser and execution engine — minimal governor loop version."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from dgov.dag_graph import compute_tiers, topological_order, validate_dag
from dgov.dag_parser import DagDefinition, DagRunSummary, parse_dag_file
from dgov.kernel import DagKernel, DispatchTask, TaskDispatched, TaskWaitDone

logger = logging.getLogger(__name__)


def _with_overridden_roots(
    dag: DagDefinition,
    project_root: str,
    session_root: str | None = None,
) -> DagDefinition:
    from dataclasses import replace

    effective_session = session_root if session_root is not None else project_root
    return replace(
        dag,
        project_root=project_root,
        session_root=effective_session,
    )


def _dag_file_hash(path: str) -> str:
    """SHA-256 of the raw DAG file bytes (before parsing)."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_dag(
    dag_file: str,
    *,
    dry_run: bool = False,
    skip: set[str] | None = None,
    project_root: str | None = None,
    session_root: str | None = None,
) -> DagRunSummary:
    """Execute a DAG file through the DagKernel state machine."""
    dag = parse_dag_file(dag_file)
    if project_root is not None:
        dag = _with_overridden_roots(dag, project_root, session_root)
    if dry_run:
        tiers = compute_tiers(dag.tasks)
        _render_dry_run(tiers)
        return DagRunSummary(run_id=0, dag_file=dag_file, status="dry_run")

    return run_dag_via_kernel(
        dag,
        dag_key=str(Path(dag_file).resolve()),
        definition_hash=_dag_file_hash(dag_file),
        skip=skip or None,
    )


def _render_dry_run(tiers: list[list[str]]) -> None:
    """Print dry-run plan showing execution tiers."""
    print("= Dry-run execution plan =")
    for i, tier in enumerate(tiers):
        print(f"Tier {i}: {', '.join(tier)}")


def run_dag_via_kernel(
    dag: DagDefinition,
    *,
    dag_key: str,
    definition_hash: str,
    skip: set[str] | None = None,
) -> DagRunSummary:
    """Execute DAG through the kernel state machine."""
    deps = {slug: tuple(t.depends_on) for slug, t in dag.tasks.items()}

    kernel = DagKernel(
        deps=deps,
        skip=frozenset(skip or ()),
    )

    kernel.start()

    summary = DagRunSummary(run_id=1, dag_file=dag_key, status="running")

    while True:
        # Get next actions from kernel
        # For now, execute dispatches immediately (single-threaded)
        # TODO: parallel dispatch when max_concurrent > 1
        next_action = None
        for action in kernel.next_actions():
            if isinstance(action, DispatchTask):
                task = dag.tasks[action.task_slug]
                # Execute the dispatch
                from dgov.pane import create_pane
                pane = create_pane(
                    task_slug=action.task_slug,
                    prompt=task.prompt,
                    agent=task.agent,
                    cwd=dag.session_root,
                )
                kernel.handle(TaskDispatched(action.task_slug, pane.slug))
            else:
                next_action = action

        if next_action is None:
            break

    # Collect results
    for slug, state in kernel.task_states.items():
        if state.value == "succeeded":
            summary.succeeded.append(slug)
        elif state.value == "failed":
            summary.failed.append(slug)
        elif state.value == "skipped":
            summary.skipped.append(slug)

    summary.status = "complete"
    return summary
