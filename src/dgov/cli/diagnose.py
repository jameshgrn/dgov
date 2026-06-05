"""CLI surface for `dgov diagnose` — report known failure shapes."""

from __future__ import annotations

import json
from pathlib import Path

import click

from dgov.cli import cli, want_json
from dgov.diagnose import (
    DiagnosisFinding,
    check_plan_claims_violation,
    check_stale_review_attention,
    stale_review_attention_tasks,
)
from dgov.live_state import tasks_from_events
from dgov.persistence.events import emit_event, read_events
from dgov.plan_sources import active_plan_source_names
from dgov.project_root import resolve_project_root


@cli.command(name="diagnose")
@click.option("--root", "-r", default=".", help="Project root")
@click.option(
    "--repair-stale-review-attention",
    is_flag=True,
    help="Append task_closed events for reviewed tasks whose plan sources are inactive.",
)
def diagnose_cmd(root: str, repair_stale_review_attention: bool) -> None:
    """Report known governor failure shapes from the Failure-to-Task catalog.

    Reads current repo state (gitignore rules, recent settlement events)
    and prints typed next-action cards for any matched failure shape.
    See `.dgov/governor.md` for the catalog.
    """
    project_root = resolve_project_root(Path(root))
    session_root = project_root / ".dgov"
    findings: list[DiagnosisFinding] = []
    events = _load_events(session_root)
    findings.extend(_safe(check_plan_claims_violation, events=events))
    active_plan_names = active_plan_source_names(project_root)
    live_tasks = _load_live_tasks(project_root)
    repairs: list[dict] | None = None
    if repair_stale_review_attention:
        repairs = _repair_stale_review_attention(
            project_root,
            live_tasks,
            active_plan_names,
        )
        if repairs:
            live_tasks = _load_live_tasks(project_root)
    findings.extend(
        _safe(
            check_stale_review_attention,
            live_tasks=live_tasks,
            active_plan_names=active_plan_names,
        )
    )
    _emit(findings, repairs=repairs)


def _load_events(session_root: Path) -> list[dict]:
    try:
        return list(read_events(str(session_root), limit=200))
    except Exception as exc:
        click.echo(f"warning: could not load events: {exc}", err=True)
        return []


def _load_live_tasks(project_root: Path) -> list[dict]:
    try:
        return list(tasks_from_events(str(project_root), latest_run_only=True))
    except Exception as exc:
        click.echo(f"warning: could not load live tasks: {exc}", err=True)
        return []


def _safe(check, **kwargs):
    try:
        return check(**kwargs)
    except Exception as exc:
        click.echo(f"warning: check {check.__name__} failed: {exc}", err=True)
        return []


def _repair_stale_review_attention(
    project_root: Path,
    live_tasks: list[dict],
    active_plan_names: frozenset[str],
) -> list[dict]:
    stale_tasks = stale_review_attention_tasks(live_tasks, active_plan_names)
    for task in stale_tasks:
        emit_event(
            str(project_root),
            "task_closed",
            "diagnose",
            plan_name=task.get("plan_name"),
            task_slug=task.get("slug"),
            reason="stale_review_attention",
        )
    return stale_tasks


def _emit(findings: list[DiagnosisFinding], repairs: list[dict] | None = None) -> None:
    if want_json():
        payload: dict[str, object] = {
            "findings": [
                {
                    "name": f.name,
                    "intent_class": f.intent_class,
                    "evidence": f.evidence,
                    "next_action": f.next_action,
                    "do_not": f.do_not,
                }
                for f in findings
            ]
        }
        if repairs is not None:
            payload["repairs"] = [
                {
                    "plan_name": str(task.get("plan_name") or ""),
                    "slug": str(task.get("slug") or ""),
                    "state": "closed",
                    "reason": "stale_review_attention",
                }
                for task in repairs
            ]
        click.echo(json.dumps(payload, indent=2))
        return
    if repairs is not None:
        if repairs:
            click.echo(f"Closed {len(repairs)} stale reviewed task(s).")
        else:
            click.echo("No stale reviewed attention tasks to close.")
    if not findings:
        click.echo("No failure shapes matched current repo state.")
        return
    for f in findings:
        click.echo(f"\n{f.name}  [{f.intent_class}]")
        click.echo(f"  evidence: {f.evidence}")
        click.echo(f"  next:     {f.next_action}")
        click.echo(f"  do not:   {f.do_not}")
