"""Run completion event recording helpers."""

from __future__ import annotations

import json
from datetime import timedelta

from dgov.event_types import RunCompleted
from dgov.persistence.events import emit_event


def emit_run_completed(
    project_root: str,
    plan_name: str,
    run_status: str,
    duration: timedelta,
    gate_result: dict[str, object],
    run_source: str,
) -> None:
    """Emit run_completed event with final status and Sentrux gate result."""
    emit_event(
        project_root,
        RunCompleted(
            pane=plan_name,
            plan_name=plan_name,
            run_status=run_status,
            duration_s=round(duration.total_seconds(), 2),
            sentrux=json.dumps(gate_result, default=str),
            run_source=run_source,
        ),
    )
