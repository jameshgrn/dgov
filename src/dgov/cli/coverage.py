"""Coverage baseline CLI surface."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import click

from dgov.cli import cli
from dgov.config import ProjectConfig, load_project_config
from dgov.project_root import resolve_project_root


def _validate_coverage_cmd(config: ProjectConfig) -> str:
    if not config.coverage_cmd:
        raise click.ClickException(
            "coverage_cmd is not configured in .dgov/project.toml. "
            "Set coverage_cmd before saving a coverage baseline."
        )
    if "{output}" not in config.coverage_cmd:
        raise click.ClickException("coverage_cmd must include an {output} placeholder.")
    return config.coverage_cmd


def _prepare_output_path(project_root: Path) -> tuple[Path, Path]:
    baseline_dir = project_root / ".coverage-baseline"
    baseline_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = baseline_dir / "coverage.json"

    with tempfile.NamedTemporaryFile(
        prefix="coverage-baseline-", suffix=".json", dir=baseline_dir, delete=False
    ) as tmp:
        output_path = Path(tmp.name)

    return baseline_path, output_path


def _run_coverage_command(
    cmd: str, project_root: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        shell=True,
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _validate_json_output(output_path: Path) -> None:
    try:
        json.loads(output_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        message = f"coverage command did not write valid JSON: {exc}"
        raise click.ClickException(message) from exc


def _cleanup_output_path(output_path: Path) -> None:
    if output_path.exists():
        output_path.unlink()


@cli.command(name="coverage-baseline")
def coverage_baseline_cmd() -> None:
    """Create or refresh the coverage baseline for the current project."""
    project_root = resolve_project_root()
    config = load_project_config(project_root)

    coverage_cmd = _validate_coverage_cmd(config)

    baseline_path, output_path = _prepare_output_path(project_root)

    cmd = coverage_cmd.replace("{output}", str(output_path))
    try:
        res = _run_coverage_command(cmd, project_root, config.settlement_timeout)
        if res.returncode != 0:
            output = ((res.stdout or "") + (res.stderr or ""))[-1000:]
            raise click.ClickException(f"coverage baseline failed:\n{output}")
        _validate_json_output(output_path)
        output_path.replace(baseline_path)
    finally:
        _cleanup_output_path(output_path)

    click.echo(f"Coverage baseline saved at {baseline_path}")
