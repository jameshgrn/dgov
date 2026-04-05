"""dgov CLI — headless governor surface."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

import click

from dgov import __version__
from dgov.persistence import all_tasks, latest_event_id, read_events, reset_state
from dgov.plan import compile_plan, parse_plan_file, validate_plan
from dgov.runner import EventDagRunner

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("dgov")


def _sentrux_available() -> bool:
    """Check if sentrux binary is available."""
    try:
        subprocess.run(
            ["sentrux", "--version"],
            capture_output=True,
            timeout=5.0,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _run_sentrux(args: list[str], cwd: str | None = None, timeout: float = 30.0) -> str:
    """Run sentrux command, return stdout."""
    result = subprocess.run(
        ["sentrux"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return result.stdout


def want_json() -> bool:
    """Check if JSON output is requested via env var or context."""
    return os.environ.get("DGOV_JSON", "").strip() in ("1", "true", "yes")


def _output(data: dict) -> None:
    """Output data as JSON or human-readable."""
    if want_json():
        click.echo(json.dumps(data, indent=2))
    else:
        for key, value in data.items():
            click.echo(f"{key}: {value}")


@click.group(invoke_without_command=True)
@click.option("--json", is_flag=True, help="Output as JSON")
@click.version_option(version=__version__, prog_name="dgov")
@click.pass_context
def cli(
    ctx: click.Context,
    json: bool,
) -> None:
    """dgov — headless governor.

    \b
    USAGE:
      dgov                    Show status
      dgov status             Show status
      dgov run plan.toml      Run a plan
      dgov validate plan.toml Validate a plan without running
      dgov init               Bootstrap .dgov/project.toml
      dgov watch              Stream events
      dgov sentrux check      Run Sentrux architectural check

    Tasks run in isolated git worktrees. No tmux required.
    """
    if json:
        os.environ["DGOV_JSON"] = "1"

    if ctx.invoked_subcommand is not None:
        return

    # Bare `dgov` → show status
    _cmd_status(str(Path.cwd()))


@cli.command(name="status")
def status_cmd() -> None:
    """Show governor status — what's running now."""
    _cmd_status(str(Path.cwd()))


@cli.command(name="watch")
def watch_cmd() -> None:
    """Stream governor events in real time."""
    _cmd_watch(str(Path.cwd()))


@cli.command(name="validate")
@click.argument("plan_file", type=click.Path(path_type=Path, exists=True))
def validate_cmd(plan_file: Path) -> None:
    """Validate a plan file without running it.

    Parses the TOML, checks dependencies, detects file-claim conflicts,
    and prints a summary of the DAG.

    \b
    Example: dgov validate plan.toml
    """
    if plan_file.suffix != ".toml":
        click.echo(f"Error: Plan file must be .toml, got: {plan_file}", err=True)
        raise SystemExit(1)

    try:
        plan = parse_plan_file(str(plan_file))
    except (ValueError, FileNotFoundError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise click.exceptions.Exit(code=1) from None

    issues = validate_plan(plan)
    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    if want_json():
        data: dict[str, object] = {
            "valid": len(errors) == 0,
            "name": plan.name,
            "tasks": len(plan.units),
            "errors": [{"message": i.message, "unit": i.unit} for i in errors],
            "warnings": [{"message": i.message, "unit": i.unit} for i in warnings],
        }
        click.echo(json.dumps(data, indent=2))
    else:
        click.echo(f"Plan: {plan.name}")
        click.echo(f"Tasks: {len(plan.units)}")
        for slug, unit in plan.units.items():
            deps = f" (depends: {', '.join(unit.depends_on)})" if unit.depends_on else ""
            files_parts = []
            if unit.files.create:
                files_parts.append(f"create={list(unit.files.create)}")
            if unit.files.edit:
                files_parts.append(f"edit={list(unit.files.edit)}")
            if unit.files.delete:
                files_parts.append(f"delete={list(unit.files.delete)}")
            files_str = f"  files: {', '.join(files_parts)}" if files_parts else ""
            click.echo(f"  - {slug}: {unit.summary}{deps}")
            if files_str:
                click.echo(f"  {files_str}")

        if errors:
            click.echo("")
            for issue in errors:
                click.echo(f"  ERROR: {issue.message}", err=True)
        if warnings:
            click.echo("")
            for issue in warnings:
                click.echo(f"  WARNING: {issue.message}")

        if errors:
            click.echo(f"\nValidation FAILED ({len(errors)} error(s))")
        else:
            click.echo("\nValidation passed.")

    if errors:
        raise click.exceptions.Exit(code=1)


@cli.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite existing project.toml")
def init_cmd(force: bool) -> None:
    """Bootstrap .dgov/project.toml for the current repository.

    Auto-detects language, source directory, and test directory.
    """
    project_root = Path.cwd()
    dgov_dir = project_root / ".dgov"
    config_path = dgov_dir / "project.toml"

    if config_path.exists() and not force:
        click.echo(f"Already exists: {config_path}")
        click.echo("Use --force to overwrite.")
        raise click.exceptions.Exit(code=1)

    language, src_dir, test_dir, extensions = _detect_project(project_root)

    toml_content = _render_project_toml(language, src_dir, test_dir, extensions)

    dgov_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(toml_content)
    click.echo(f"Created {config_path}")
    click.echo(f"  language: {language}")
    click.echo(f"  src_dir:  {src_dir}")
    click.echo(f"  test_dir: {test_dir}")


@cli.command(name="run")
@click.argument("plan_file", type=click.Path(path_type=Path, exists=True))
@click.pass_context
def run_cmd(ctx: click.Context, plan_file: Path) -> None:
    """Run a plan file (TOML).

    Example: dgov run plan.toml
    """
    if plan_file.suffix != ".toml":
        click.echo(f"Error: Plan file must be .toml, got: {plan_file}", err=True)
        raise SystemExit(1)
    project_root = str(Path.cwd())
    _cmd_run_plan(str(plan_file), project_root)


def _cmd_status(project_root: str) -> None:
    """Show governor status — what's running now."""
    try:
        tasks = all_tasks(project_root)
    except Exception as exc:
        _output({"status": "error", "message": str(exc)})
        return

    if not tasks:
        _output({"status": "idle", "tasks": 0})
        return

    active = [t for t in tasks if t.get("state") == "active"]
    _output(
        {
            "status": "active" if active else "idle",
            "tasks": len(tasks),
            "active": len(active),
            "task_list": [{"slug": t.get("slug"), "state": t.get("state")} for t in tasks[:10]],
        }
    )


def _cmd_watch(project_root: str) -> None:
    """Stream events from the current run. Open in a second tab."""
    import time

    click.echo("dgov watch (Ctrl-C to exit)")

    last_id = 0
    last_task = ""
    try:
        while True:
            # Detect DB reset (new run started) — last_id would be ahead of max
            current_max = latest_event_id(project_root)
            if current_max < last_id:
                click.echo("\n  --- new run ---\n")
                last_id = 0
                last_task = ""

            events = read_events(project_root, after_id=last_id)
            for ev in events:
                last_id = max(last_id, ev.get("id", 0))
                line = _format_event(ev)
                if line is None:
                    continue

                # Blank line between tasks
                task = ev.get("task_slug") or ev.get("slug") or ""
                event_type = ev.get("event", "")
                if event_type == "dag_task_dispatched" and last_task:
                    click.echo("")
                if task:
                    last_task = task

                click.echo(line)

            time.sleep(0.5)
    except KeyboardInterrupt:
        click.echo("")


def _format_event(ev: dict) -> str | None:
    """Format a single event. Returns None to suppress."""
    event_type = ev.get("event", "?")
    task_slug = ev.get("task_slug") or ev.get("slug") or ""
    ts_raw = ev.get("ts", "")
    ts = ts_raw[11:19] if len(ts_raw) >= 19 else ts_raw

    # Suppress lifecycle done — worker_log done already has the summary
    if event_type == "task_done":
        return None
    # Suppress review_pass — merged line is enough for happy path
    if event_type == "review_pass":
        return None

    if event_type == "worker_log":
        return _format_worker_log(ts, task_slug, ev)

    # Lifecycle events
    label = _EVENT_LABELS.get(event_type, event_type)
    suffix = ""
    error = ev.get("error")
    if error:
        suffix = f" — {error[:100]}"
    verdict = ev.get("verdict")
    if verdict and verdict != "ok":
        suffix = f" ({verdict})"

    # Dispatch gets a header style
    if event_type == "dag_task_dispatched":
        agent = ev.get("agent", "")
        agent_short = agent.rsplit("/", 1)[-1] if agent else ""
        return f"{ts}  >> {task_slug} ({agent_short})"

    return f"{ts} {label:>12s}  {task_slug}{suffix}"


def _format_worker_log(ts: str, task_slug: str, ev: dict) -> str | None:
    """Format worker_log events. Returns None to suppress."""
    log_type = ev.get("log_type", "")
    content = ev.get("content")

    if log_type == "error":
        return f"{ts}       ERROR  {task_slug}: {content}"
    if log_type == "done":
        # Summary line for task completion
        text = str(content)[:150] if content else ""
        return f"{ts}          ok  {task_slug}: {text}"
    if log_type == "thought":
        text = str(content)[:120] if content else ""
        return f"{ts}             {task_slug}: {text}"
    if log_type == "call":
        if isinstance(content, dict):
            tool = content.get("tool", "?")
            args = content.get("args", {})
            summary = ", ".join(f"{k}={repr(v)[:40]}" for k, v in args.items())
            return f"{ts}        call  {task_slug}: {tool}({summary})"
        return f"{ts}        call  {task_slug}: {content}"
    if log_type == "result":
        # Only show failures — success is noise
        if isinstance(content, dict) and content.get("status") == "failed":
            tool = content.get("tool", "?")
            return f"{ts}        FAIL  {task_slug}: {tool}"
        return None

    return f"{ts}             {task_slug}: [{log_type}] {content}"


_EVENT_LABELS: dict[str, str] = {
    "dag_task_dispatched": ">>",
    "task_done": "done",
    "task_failed": "FAILED",
    "review_pass": "review ok",
    "review_fail": "review FAIL",
    "merge_completed": "merged",
    "task_merge_failed": "merge FAIL",
    "shutdown_requested": "shutdown",
    "dag_completed": "dag done",
    "dag_failed": "dag FAILED",
}


def _cmd_run_plan(plan_file: str, project_root: str) -> None:
    """Execute a plan TOML with Sentrux quality gates."""

    plan = parse_plan_file(plan_file)
    dag = compile_plan(plan)

    # Clean slate — no stale events/tasks from prior runs
    reset_state(project_root)

    # Pre-flight: check sentrux availability and save baseline if available
    sentrux_available = False
    baseline_quality: int | None = None
    try:
        result = subprocess.run(
            ["sentrux", "--version"],
            capture_output=True,
            timeout=5.0,
            check=True,
        )
        sentrux_available = True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass

    if sentrux_available:
        if not want_json():
            click.echo("[sentrux] Saving baseline...")
        try:
            result = subprocess.run(
                ["sentrux", "gate", "--save", project_root],
                capture_output=True,
                text=True,
                timeout=30.0,
                check=True,
            )
            # Parse quality from output
            for line in result.stdout.splitlines():
                if line.startswith("Quality: "):
                    try:
                        baseline_quality = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                    break
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            if not want_json():
                click.echo(f"[sentrux] Baseline save failed: {e}", err=True)

    # Live event callback — print worker activity to stderr
    def _on_worker_event(task_slug: str, log_type: str, content: object) -> None:
        if want_json():
            return
        if log_type == "error":
            click.echo(f"  [{task_slug}] ERROR: {content}", err=True)
        elif log_type == "thought":
            click.echo(f"  [{task_slug}] {str(content)[:120]}", err=True)
        elif log_type == "call":
            if isinstance(content, dict):
                tool = content.get("tool", "?")
                args = content.get("args", {})
                summary = ", ".join(f"{k}={repr(v)[:40]}" for k, v in args.items())
                click.echo(f"  [{task_slug}] {tool}({summary})", err=True)
        elif log_type == "done":
            click.echo(f"  [{task_slug}] done: {content}", err=True)

    runner = EventDagRunner(dag, session_root=project_root, on_event=_on_worker_event)

    if want_json():
        click.echo(
            json.dumps(
                {"status": "starting", "dag": dag.name, "sentrux_baseline": baseline_quality}
            )
        )
    elif sentrux_available:
        click.echo(f"[sentrux] Baseline quality: {baseline_quality}")

    try:
        results = asyncio.run(runner.run())
    except KeyboardInterrupt:
        _output({"status": "interrupted"})
        raise click.Exit(code=130)

    # Post-flight: compare against baseline
    gate_result: dict[str, object] = {
        "degradation": None,
        "quality_before": baseline_quality,
        "quality_after": None,
    }
    if sentrux_available:
        if not want_json():
            click.echo("[sentrux] Comparing against baseline...")
        try:
            result = subprocess.run(
                ["sentrux", "gate", project_root],
                capture_output=True,
                text=True,
                timeout=30.0,
                check=True,
            )
            gate_output = result.stdout
            degradation = False
            quality_after = None
            for line in gate_output.splitlines():
                if line.startswith("Quality:") and "->" in line:
                    parts = line.split("->")
                    if len(parts) == 2:
                        try:
                            quality_after = int(parts[1].strip())
                        except ValueError:
                            pass
                elif "No degradation detected" in line or "✓ No degradation" in line:
                    degradation = False
                elif "degradation" in line.lower():
                    degradation = True
            gate_result["degradation"] = degradation
            gate_result["quality_after"] = quality_after
            if not want_json():
                status = "✓ clean" if not degradation else "✗ degradation detected"
                click.echo(f"[sentrux] Gate result: {status}")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            gate_result["error"] = str(e)
            if not want_json():
                click.echo(f"[sentrux] Gate comparison failed: {e}", err=True)

    succeeded = [s for s, st in results.items() if st == "merged"]
    failed = [s for s, st in results.items() if st == "failed"]

    output = {
        "status": "complete" if not failed else "failed",
        "succeeded": len(succeeded),
        "failed": len(failed),
        "failed_tasks": failed if failed else None,
        "sentrux": gate_result if sentrux_available else None,
    }
    _output(output)

    # Permanent run log — append-only, git-tracked
    _append_run_log(project_root, dag.name, plan_file, results, gate_result)

    if failed:
        raise click.exceptions.Exit(code=1)


def _append_run_log(
    project_root: str,
    plan_name: str,
    plan_file: str,
    results: dict[str, str],
    gate_result: dict[str, object],
) -> None:
    """Append a run summary to .dgov/runs.log — permanent, git-tracked."""
    from datetime import datetime, timezone

    log_path = Path(project_root) / ".dgov" / "runs.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    merged = [s for s, st in results.items() if st == "merged"]
    failed = [s for s, st in results.items() if st == "failed"]
    status = "ok" if not failed else "fail"

    lines = [f"[{ts}] {plan_name} ({plan_file}) — {status}"]
    if merged:
        lines.append(f"  merged: {', '.join(merged)}")
    if failed:
        lines.append(f"  failed: {', '.join(failed)}")
    quality_before = gate_result.get("quality_before")
    quality_after = gate_result.get("quality_after")
    if quality_before is not None:
        lines.append(f"  sentrux: {quality_before} -> {quality_after}")
    lines.append("")

    with open(log_path, "a") as f:
        f.write("\n".join(lines) + "\n")


def _detect_project(root: Path) -> tuple[str, str, str, list[str]]:
    """Auto-detect language, src dir, test dir, and extensions."""
    language = "python"
    src_dir = "src/"
    test_dir = "tests/"
    extensions = [".py"]

    # Language detection by file prevalence
    py_files = list(root.glob("**/*.py"))
    js_files = list(root.glob("**/*.js")) + list(root.glob("**/*.ts"))
    rs_files = list(root.glob("**/*.rs"))
    go_files = list(root.glob("**/*.go"))

    counts = {
        "python": len(py_files),
        "javascript": len(js_files),
        "rust": len(rs_files),
        "go": len(go_files),
    }
    language = max(counts, key=counts.get)  # type: ignore[arg-type]
    if counts[language] == 0:
        language = "python"

    # Source dir detection
    if (root / "src").is_dir():
        src_dir = "src/"
    elif (root / "lib").is_dir():
        src_dir = "lib/"
    else:
        src_dir = "."

    # Test dir detection
    if (root / "tests").is_dir():
        test_dir = "tests/"
    elif (root / "test").is_dir():
        test_dir = "test/"
    else:
        test_dir = "tests/"

    # Extensions by language
    ext_map = {
        "python": [".py"],
        "javascript": [".js", ".ts", ".tsx"],
        "rust": [".rs"],
        "go": [".go"],
    }
    extensions = ext_map.get(language, [".py"])

    return language, src_dir, test_dir, extensions


_LANG_TEMPLATES: dict[str, dict[str, str]] = {
    "python": {
        "test_cmd": "python -m pytest {test_dir} -q --tb=short",
        "lint_cmd": "python -m ruff check {file}",
        "format_cmd": "python -m ruff format {file}",
        "lint_fix_cmd": "python -m ruff check --fix {file}",
        "format_check_cmd": "python -m ruff format --check {file}",
    },
    "javascript": {
        "test_cmd": "npx vitest run {test_dir}",
        "lint_cmd": "npx eslint {file}",
        "format_cmd": "npx prettier --write {file}",
        "lint_fix_cmd": "npx eslint --fix {file}",
        "format_check_cmd": "npx prettier --check {file}",
    },
    "rust": {
        "test_cmd": "cargo test",
        "lint_cmd": "cargo clippy -- -D warnings",
        "format_cmd": "cargo fmt",
        "lint_fix_cmd": "cargo clippy --fix --allow-dirty",
        "format_check_cmd": "cargo fmt --check",
    },
    "go": {
        "test_cmd": "go test ./...",
        "lint_cmd": "golangci-lint run {file}",
        "format_cmd": "gofmt -w {file}",
        "lint_fix_cmd": "golangci-lint run --fix {file}",
        "format_check_cmd": "gofmt -l {file}",
    },
}


def _render_project_toml(language: str, src_dir: str, test_dir: str, extensions: list[str]) -> str:
    """Render a project.toml string."""
    cmds = _LANG_TEMPLATES.get(language, _LANG_TEMPLATES["python"])
    ext_str = ", ".join(f'"{e}"' for e in extensions)

    lines = [
        "[project]",
        f'language = "{language}"',
        f'src_dir = "{src_dir}"',
        f'test_dir = "{test_dir}"',
        f"source_extensions = [{ext_str}]",
        f'test_cmd = "{cmds["test_cmd"]}"',
        f'lint_cmd = "{cmds["lint_cmd"]}"',
        f'format_cmd = "{cmds["format_cmd"]}"',
        f'lint_fix_cmd = "{cmds["lint_fix_cmd"]}"',
        f'format_check_cmd = "{cmds["format_check_cmd"]}"',
        "",
        "[conventions]",
    ]
    return "\n".join(lines) + "\n"


@cli.group(name="sentrux")
def sentrux_cmd() -> None:
    """Sentrux architectural sensing commands."""
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
        raise click.exceptions.Exit(code=1)
    except subprocess.TimeoutExpired:
        click.echo("Error: sentrux check timed out", err=True)
        raise click.exceptions.Exit(code=1)

    # Parse quality from output
    quality = 0
    for line in output.splitlines():
        if line.startswith("Quality: "):
            try:
                quality = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
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
        raise click.exceptions.Exit(code=1)
    except subprocess.TimeoutExpired:
        click.echo("Error: sentrux gate-save timed out", err=True)
        raise click.exceptions.Exit(code=1)

    # Parse quality from output
    quality = 0
    for line in output.splitlines():
        if line.startswith("Quality: "):
            try:
                quality = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
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
        raise click.exceptions.Exit(code=1)
    except subprocess.TimeoutExpired:
        click.echo("Error: sentrux gate timed out", err=True)
        raise click.exceptions.Exit(code=1)

    # Detect degradation
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
