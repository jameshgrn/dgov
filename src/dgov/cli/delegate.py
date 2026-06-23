"""CLI surface for `dgov delegate` — render a lieutenant delegation brief."""

from __future__ import annotations

import json
from pathlib import Path

import click

from dgov.cli import cli, load_project_config_or_exit, want_json
from dgov.project_root import resolve_project_root

_STOP_RULES = (
    "Stop if a plan task produces a scope violation — fix claims before retrying.",
    "Stop if settlement rejects three consecutive attempts on the same task.",
    "Stop if the vision requires capabilities outside the declared providers.",
    "Stop and log a 'decision' ledger entry before adding file claims beyond those compiled.",
)

_WORKFLOW_STEPS = (
    "dgov plan create <goal>  — or author TOML directly",
    "dgov compile <dir>       — validate claims and compile plan tree",
    "dgov run <dir>           — dispatch tasks to workers in isolated worktrees",
    "dgov plan review <dir>   — debrief after each run",
    "dgov ledger add ...      — record decisions, rules, and bugs after review",
)

_LEDGER_OBLIGATIONS = (
    "Record worker bugs:       dgov ledger add bug <description>",
    "Record new rules:         dgov ledger add rule <description>",
    "Record decisions:         dgov ledger add decision <description>",
    "Resolve closed entries:   dgov ledger resolve <id>",
)


@cli.command(name="delegate")
@click.argument("vision")
@click.option(
    "--lieutenant-provider",
    "lieutenant_provider",
    default=None,
    help="Provider name for the lieutenant governor. Defaults to project default.",
)
@click.option(
    "--worker-provider",
    "worker_provider",
    default=None,
    help="Provider name for workers. Defaults to project default.",
)
@click.option("--root", "-r", default=".", help="Project root")
def delegate_cmd(
    vision: str,
    lieutenant_provider: str | None,
    worker_provider: str | None,
    root: str,
) -> None:
    """Render a delegation brief for a lieutenant governor.

    VISION is a short statement of what the lieutenant should accomplish.
    The brief includes provider contracts, stop rules, required plan-mediated
    workflow, verification expectations, and ledger obligations.
    """
    project_root = resolve_project_root(Path(root))
    config = load_project_config_or_exit(project_root)

    known_providers = set(config.providers.keys())
    resolved_lieutenant = lieutenant_provider or config.llm_provider
    resolved_worker = worker_provider or config.llm_provider

    errors: list[str] = []
    if resolved_lieutenant not in known_providers:
        errors.append(
            f"lieutenant provider {resolved_lieutenant!r} is not defined in [providers.*]."
            f" Known: {sorted(known_providers)}"
        )
    if resolved_worker not in known_providers:
        errors.append(
            f"worker provider {resolved_worker!r} is not defined in [providers.*]."
            f" Known: {sorted(known_providers)}"
        )
    if errors:
        for err in errors:
            click.echo(f"Error: {err}", err=True)
        raise click.exceptions.Exit(code=1)

    lieutenant_cfg = config.providers[resolved_lieutenant]
    worker_cfg = config.providers[resolved_worker]

    lint_cmd = config.resolve_lint_cmd()
    format_check_cmd = config.resolve_format_check_cmd("<file>")
    test_cmd = config.resolve_test_cmd()

    if want_json():
        payload: dict[str, object] = {
            "vision": vision,
            "lieutenant": {
                "provider": resolved_lieutenant,
                "default_agent": lieutenant_cfg.default_agent or "",
                "base_url": lieutenant_cfg.base_url,
            },
            "worker": {
                "provider": resolved_worker,
                "default_agent": worker_cfg.default_agent or "",
                "base_url": worker_cfg.base_url,
            },
            "stop_rules": list(_STOP_RULES),
            "workflow": list(_WORKFLOW_STEPS),
            "verification": {
                "lint": lint_cmd,
                "format_check": format_check_cmd,
                "test": test_cmd,
            },
            "ledger_obligations": list(_LEDGER_OBLIGATIONS),
        }
        click.echo(json.dumps(payload, indent=2))
        return

    _echo_brief(
        vision=vision,
        lieutenant_provider=resolved_lieutenant,
        lieutenant_model=lieutenant_cfg.default_agent or "(not set)",
        lieutenant_base_url=lieutenant_cfg.base_url,
        worker_provider=resolved_worker,
        worker_model=worker_cfg.default_agent or "(not set)",
        worker_base_url=worker_cfg.base_url,
        lint_cmd=lint_cmd,
        format_check_cmd=format_check_cmd,
        test_cmd=test_cmd,
    )


def _echo_brief(
    *,
    vision: str,
    lieutenant_provider: str,
    lieutenant_model: str,
    lieutenant_base_url: str,
    worker_provider: str,
    worker_model: str,
    worker_base_url: str,
    lint_cmd: str,
    format_check_cmd: str,
    test_cmd: str,
) -> None:
    click.echo("DELEGATION BRIEF")
    click.echo("=" * 64)
    click.echo(f"Vision:           {vision}")
    click.echo("")
    click.echo("LIEUTENANT GOVERNOR")
    click.echo(f"  Provider:       {lieutenant_provider}")
    click.echo(f"  Model:          {lieutenant_model}")
    click.echo(f"  Endpoint:       {lieutenant_base_url}")
    click.echo("")
    click.echo("WORKER PROVIDER")
    click.echo(f"  Provider:       {worker_provider}")
    click.echo(f"  Model:          {worker_model}")
    click.echo(f"  Endpoint:       {worker_base_url}")
    click.echo("")
    click.echo("STOP RULES")
    for rule in _STOP_RULES:
        click.echo(f"  - {rule}")
    click.echo("")
    click.echo("REQUIRED WORKFLOW")
    for step in _WORKFLOW_STEPS:
        click.echo(f"  {step}")
    click.echo("")
    click.echo("VERIFICATION EXPECTATIONS")
    click.echo(f"  Lint:           {lint_cmd}")
    click.echo(f"  Format check:   {format_check_cmd}")
    if test_cmd:
        click.echo(f"  Test:           {test_cmd}")
    click.echo("")
    click.echo("LEDGER OBLIGATIONS (after each dgov run)")
    for obligation in _LEDGER_OBLIGATIONS:
        click.echo(f"  {obligation}")
