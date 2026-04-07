"""Sentrux subcommands — architectural sensing CLI surface."""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import click

from dgov.cli import _output, cli, want_json
from dgov.cli.run import _run_sentrux, _sentrux_available


@cli.group(name="sentrux")
def sentrux_cmd() -> None:
    """Sentrux architectural sensing commands.

    NOTE: Sentrux currently lacks path exclusion. Feature request filed for
    'ignored_dirs' in rules.toml to exclude test files from coupling scores.
    """
    pass


@sentrux_cmd.command(name="check")
@click.argument("path", required=False, type=click.Path(path_type=Path, exists=True))
@click.option("--json-output", "json_fmt", is_flag=True, help="Output as JSON")
def sentrux_check(path: Path | None, json_fmt: bool) -> None:
    """Run Sentrux check on a directory.

    PATH defaults to current directory if not specified.
    """
    if not _sentrux_available():
        click.echo(
            "Error: sentrux not found. Install: https://github.com/sentrux/sentrux", err=True
        )
        raise click.exceptions.Exit(code=1)

    target = str(path) if path else "."
    try:
        output = _run_sentrux(["check", target], cwd=target)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error: sentrux check failed: {e.stderr or e.stdout}", err=True)
        raise click.exceptions.Exit(code=1) from e
    except subprocess.TimeoutExpired as e:
        click.echo("Error: sentrux check timed out", err=True)
        raise click.exceptions.Exit(code=1) from e

    # Parse quality from output
    quality = 0
    for line in output.splitlines():
        if line.startswith("Quality: "):
            with contextlib.suppress(ValueError):
                quality = int(line.split(":", 1)[1].strip())
            break

    if json_fmt or want_json():
        _output({"quality": quality, "path": target})
    else:
        click.echo(output)
        click.echo(f"\nQuality: {quality}")


@sentrux_cmd.command(name="gate-save")
@click.argument("path", required=False, type=click.Path(path_type=Path, exists=True))
def sentrux_gate_save(path: Path | None) -> None:
    """Save Sentrux baseline before making changes.

    PATH defaults to current directory if not specified.
    """
    if not _sentrux_available():
        click.echo(
            "Error: sentrux not found. Install: https://github.com/sentrux/sentrux", err=True
        )
        raise click.exceptions.Exit(code=1)

    target = str(path) if path else "."
    try:
        output = _run_sentrux(["gate", "--save", target], cwd=target)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error: sentrux gate-save failed: {e.stderr or e.stdout}", err=True)
        raise click.exceptions.Exit(code=1) from e
    except subprocess.TimeoutExpired as e:
        click.echo("Error: sentrux gate-save timed out", err=True)
        raise click.exceptions.Exit(code=1) from e

    quality = 0
    for line in output.splitlines():
        if line.startswith("Quality: "):
            with contextlib.suppress(ValueError):
                quality = int(line.split(":", 1)[1].strip())
            break

    click.echo(f"Baseline saved at {Path(target) / '.sentrux' / 'baseline.json'}")
    click.echo(f"Quality: {quality}")


@sentrux_cmd.command(name="gate")
@click.argument("path", required=False, type=click.Path(path_type=Path, exists=True))
@click.option(
    "--fail-on-degradation", is_flag=True, help="Exit with error code if degradation detected"
)
def sentrux_gate(path: Path | None, fail_on_degradation: bool) -> None:
    """Compare current state against saved Sentrux baseline.

    PATH defaults to current directory if not specified.
    """
    if not _sentrux_available():
        click.echo(
            "Error: sentrux not found. Install: https://github.com/sentrux/sentrux", err=True
        )
        raise click.exceptions.Exit(code=1)

    target = str(path) if path else "."
    try:
        output = _run_sentrux(["gate", target], cwd=target)
    except subprocess.CalledProcessError as e:
        click.echo(f"Error: sentrux gate failed: {e.stderr or e.stdout}", err=True)
        raise click.exceptions.Exit(code=1) from e
    except subprocess.TimeoutExpired as e:
        click.echo("Error: sentrux gate timed out", err=True)
        raise click.exceptions.Exit(code=1) from e

    degradation = "degradation" in output.lower() and "no degradation" not in output.lower()

    click.echo(output)

    if degradation and fail_on_degradation:
        click.echo("\nDegradation detected — failing.", err=True)
        raise click.exceptions.Exit(code=1)


@sentrux_cmd.command(name="status")
def sentrux_status() -> None:
    """Check if Sentrux is installed and available."""
    if _sentrux_available():
        click.echo("sentrux: installed and available")
    else:
        click.echo("sentrux: not found in PATH")
        click.echo("Install: https://github.com/sentrux/sentrux")
        raise click.exceptions.Exit(code=1)
