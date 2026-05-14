"""CLI surface for `dgov diagnose` — report known failure shapes."""

from __future__ import annotations

import json
from pathlib import Path

import click

from dgov.cli import cli, want_json
from dgov.diagnose import (
    DiagnosisFinding,
    check_archive_policy_drift,
    check_plan_claims_violation,
)
from dgov.persistence.events import read_events
from dgov.project_root import resolve_project_root


@cli.command(name="diagnose")
@click.option("--root", "-r", default=".", help="Project root")
def diagnose_cmd(root: str) -> None:
    """Report known governor failure shapes from the Failure-to-Task catalog.

    Reads current repo state (gitignore rules, recent settlement events)
    and prints typed next-action cards for any matched failure shape.
    See `.dgov/governor.md` for the catalog.
    """
    project_root = resolve_project_root(Path(root))
    session_root = project_root / ".dgov"
    findings: list[DiagnosisFinding] = []
    findings.extend(_safe(check_archive_policy_drift, project_root=project_root))
    events = _load_events(session_root)
    findings.extend(_safe(check_plan_claims_violation, events=events))
    _emit(findings)


def _load_events(session_root: Path) -> list[dict]:
    try:
        return list(read_events(str(session_root), limit=200))
    except Exception as exc:
        click.echo(f"warning: could not load events: {exc}", err=True)
        return []


def _safe(check, **kwargs):
    try:
        return check(**kwargs)
    except Exception as exc:
        click.echo(f"warning: check {check.__name__} failed: {exc}", err=True)
        return []


def _emit(findings: list[DiagnosisFinding]) -> None:
    if want_json():
        payload = {
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
        click.echo(json.dumps(payload, indent=2))
        return
    if not findings:
        click.echo("No failure shapes matched current repo state.")
        return
    for f in findings:
        click.echo(f"\n{f.name}  [{f.intent_class}]")
        click.echo(f"  evidence: {f.evidence}")
        click.echo(f"  next:     {f.next_action}")
        click.echo(f"  do not:   {f.do_not}")
