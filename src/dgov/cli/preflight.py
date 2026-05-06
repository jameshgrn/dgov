"""Local acceptance-gate preflight for hand edits."""

from __future__ import annotations

import click

from dgov.cli import _output, cli, want_json
from dgov.policy_drift import find_policy_drift
from dgov.project_root import resolve_project_root
from dgov.settlement import GateResult, preflight_sandbox


def _merge_preflight_results(settlement_result: GateResult, policy_drift: list[str]) -> GateResult:
    errors: list[str] = []
    if policy_drift:
        errors.append("Policy drift:\n" + "\n".join(f"- {issue}" for issue in policy_drift))
    if not settlement_result.passed:
        errors.append(settlement_result.error or "Settlement preflight failed")
    if errors:
        return GateResult(passed=False, error="\n\n".join(errors))
    return GateResult(passed=True)


@cli.command(name="preflight")
def preflight_cmd() -> None:
    """Run settlement acceptance gates against local working-tree changes."""
    project_root = resolve_project_root()
    policy_drift = find_policy_drift(project_root)
    settlement_result = preflight_sandbox(project_root, str(project_root))
    result = _merge_preflight_results(settlement_result, policy_drift)

    if want_json():
        _output({
            "passed": result.passed,
            "project_root": str(project_root),
            "error": result.error,
            "policy_drift": policy_drift,
        })
    elif result.passed:
        click.echo("Preflight passed.")
    else:
        click.echo(f"Preflight failed:\n{result.error}", err=True)

    if not result.passed:
        raise click.exceptions.Exit(code=1)
