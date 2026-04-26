"""Coverage baseline CLI surface."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import click

from dgov.cli import cli
from dgov.config import load_project_config
from dgov.project_root import resolve_project_root


@cli.command(name="coverage-baseline")
def coverage_baseline_cmd() -> None:
    """Create or refresh the coverage baseline for the current project."""
    project_root = resolve_project_root()
    config = load_project_config(project_root)
    if not config.coverage_cmd:
        raise click.ClickException(
            "coverage_cmd is not configured in .dgov/project.toml. "
            "Set coverage_cmd before saving a coverage baseline."
        )
    if "{output}" not in config.coverage_cmd:
        raise click.ClickException("coverage_cmd must include an {output} placeholder.")

    baseline_dir = project_root / ".coverage-baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = baseline_dir / "coverage.json"

    with tempfile.NamedTemporaryFile(
        prefix="coverage-baseline-", suffix=".json", dir=baseline_dir, delete=False
    ) as tmp:
        output_path = Path(tmp.name)

    cmd = config.coverage_cmd.replace("{output}", str(output_path))
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=config.settlement_timeout,
            check=False,
        )
        if res.returncode != 0:
            output = ((res.stdout or "") + (res.stderr or ""))[-1000:]
            raise click.ClickException(f"coverage baseline failed:\n{output}")
        try:
            json.loads(output_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            message = f"coverage command did not write valid JSON: {exc}"
            raise click.ClickException(message) from exc
        output_path.replace(baseline_path)
    finally:
        if output_path.exists():
            output_path.unlink()

    click.echo(f"Coverage baseline saved at {baseline_path}")
