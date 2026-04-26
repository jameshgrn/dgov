"""Run subcommand — plan execution with sentrux quality gates."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import click

from dgov.archive import archive_plan
from dgov.cli import _output, cli, want_json
from dgov.dag_parser import DagDefinition
from dgov.deploy_log import is_plan_complete
from dgov.persistence.events import emit_event
from dgov.plan import PlanSpec, compile_plan, parse_plan_file
from dgov.project_root import resolve_project_root
from dgov.repo_snapshot import format_structural_offender_report, likely_structural_offenders
from dgov.runner import EventDagRunner


@contextlib.contextmanager
def _clean_head_worktree(project_root: str) -> Iterator[Path]:
    """Yield a temporary worktree checked out at HEAD for read-only scanning.

    The post-run sentrux gate needs to measure the committed state, not the
    governor's live working tree. A transient git worktree at HEAD gives us
    a clean view without disturbing the main checkout. See ledger bug #26.
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="dgov-sentrux-"))
    wt_path = tmp_root / "head"
    created = False
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt_path), "HEAD"],
            cwd=project_root,
            check=True,
            capture_output=True,
        )
        created = True
        yield wt_path
    finally:
        if created:
            subprocess.run(
                ["git", "worktree", "remove", "-f", str(wt_path)],
                cwd=project_root,
                capture_output=True,
            )
        shutil.rmtree(tmp_root, ignore_errors=True)


@cli.command(name="run")
@click.argument("plan", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--restart", is_flag=True, help="Restart the plan from the beginning, clearing prior state"
)
@click.option(
    "--continue",
    "continue_failed",
    is_flag=True,
    help="Continue from where you left off, retrying failed tasks",
)
@click.option("--only", default=None, help="Run only this task and its deps")
@click.option(
    "--yes", "-y", is_flag=True, help="Skip interactive prompts (auto-create bootstrap commits)"
)
@click.option(
    "--stream",
    is_flag=True,
    help="Stream worker thoughts and tool calls live (like `dgov watch` inline)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Show richer per-task summary at end of run",
)
@click.pass_context
def run_cmd(
    ctx: click.Context,
    plan: Path,
    restart: bool,
    continue_failed: bool,
    only: str | None,
    yes: bool,
    stream: bool,
    verbose: bool,
) -> None:
    """Compile and run a plan directory.

    Example: dgov run .dgov/plans/my-plan/
    """
    if not plan.is_dir():
        click.echo("Error: dgov run requires a plan directory, not a file path.", err=True)
        click.echo(
            "Fix: run `dgov run <plan-dir>` so dgov compiles the current source first.", err=True
        )
        raise SystemExit(1)

    project_root = str(resolve_project_root())
    plan_dir = plan
    _compile_plan_for_run(plan_dir)
    plan_file = plan_dir / "_compiled.toml"
    _cmd_run_plan(
        str(plan_file),
        project_root,
        restart=restart,
        continue_failed=continue_failed,
        only=only,
        plan_dir=plan_dir,
        yes=yes,
        stream=stream,
        verbose=verbose,
    )


def _compile_plan_for_run(plan_dir: Path) -> None:
    """Compile the current plan tree before every public run."""
    from dgov.cli.compile import _cmd_compile

    _cmd_compile(plan_dir, dry_run=False, recompile_sops=False, graph=False)


def _parse_quality(line: str) -> int | None:
    """Extract quality value from a 'Quality: N' or 'Quality: A -> B' line.

    Supports both integer scores (e.g. 6922) and float signals (e.g. 0.69)
    by scaling signals (x10000) to match the sentrux check scale.
    """
    if not line.startswith("Quality:"):
        return None
    rest = line.split(":", 1)[1].strip()
    token = rest.split("->")[-1].strip() if "->" in rest else rest
    try:
        return int(token)
    except ValueError:
        try:
            val = float(token)
            if val <= 1.0:
                return int(val * 10000)
            return int(val)
        except ValueError:
            return None


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


def _run_sentrux(
    args: list[str], cwd: str | None = None, timeout: float = 30.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run sentrux command."""
    result = subprocess.run(
        ["sentrux", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )
    return result


def _git_env(cwd: str | None = None) -> dict[str, str]:
    """Return clean git environment for local repo operations."""
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    if cwd is not None:
        env["PWD"] = cwd
    return env


def _working_tree_files(project_root: str) -> list[str]:
    """Return changed/untracked paths for a repo without assuming HEAD exists."""
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=project_root,
        capture_output=True,
        text=True,
        env=_git_env(project_root),
        check=False,
    )
    files: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        files.append(path_part)
    return files


def _create_bootstrap_commit(project_root: str, files: list[str]) -> None:
    """Create an initial snapshot commit for a repo that has no HEAD yet."""
    env = _git_env(project_root)
    env["GIT_AUTHOR_NAME"] = "dgov-bootstrap"
    env["GIT_AUTHOR_EMAIL"] = "bootstrap@dgov.local"
    env["GIT_COMMITTER_NAME"] = "dgov-bootstrap"
    env["GIT_COMMITTER_EMAIL"] = "bootstrap@dgov.local"

    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "chore: bootstrap repo for dgov"],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or str(exc)).strip()
        click.echo("Error: failed to create bootstrap commit.", err=True)
        if details:
            click.echo(details, err=True)
        raise click.exceptions.Exit(code=1) from exc

    click.echo(f"Created bootstrap commit from current working tree ({len(files)} file(s)).")


def _ensure_git_ready(project_root: str, yes: bool = False) -> None:
    """Fail fast unless the current directory is a git repo with a clean working tree."""
    repo_check = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if repo_check.returncode != 0:
        click.echo("Error: dgov run requires a git repository.", err=True)
        click.echo("Fix: run `git init` in this project first.", err=True)
        raise click.exceptions.Exit(code=1)

    head_check = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if head_check.returncode != 0:
        files = _working_tree_files(project_root)
        if not files:
            click.echo(
                "Error: repository has no commits and nothing to snapshot for dgov.",
                err=True,
            )
            click.echo("Fix: run `dgov init` or add files, then try again.", err=True)
            raise click.exceptions.Exit(code=1)

        if all(path.startswith(".dgov/") for path in files):
            _create_bootstrap_commit(project_root, files)
            return

        headless = not sys.stdin.isatty()
        if yes or headless:
            # Auto-create bootstrap commit if --yes flag or headless mode
            _create_bootstrap_commit(project_root, files)
            return

        create_bootstrap = click.confirm(
            (
                "Repository has no commits. Create a bootstrap commit from the current working "
                f"tree ({len(files)} file(s))?"
            ),
            default=True,
        )
        if create_bootstrap:
            _create_bootstrap_commit(project_root, files)
            return

        click.echo(
            "Error: repository has no commits. dgov needs an initial snapshot before it can "
            "create worktrees.",
            err=True,
        )
        click.echo("Fix: create a bootstrap commit or commit manually, then try again.", err=True)
        raise click.exceptions.Exit(code=1)

    # HEAD exists — check for uncommitted changes. Worktrees branch from HEAD,
    # so uncommitted files cause cherry-pick conflicts at merge time.
    dirty = _working_tree_files(project_root)
    dirty = [f for f in dirty if not f.startswith(".dgov/")]
    if dirty:
        click.echo("Error: working tree has uncommitted changes.", err=True)
        click.echo(
            "Worktrees branch from HEAD — uncommitted files cause merge conflicts.",
            err=True,
        )
        for f in dirty[:10]:
            click.echo(f"  {f}", err=True)
        if len(dirty) > 10:
            click.echo(f"  ... and {len(dirty) - 10} more", err=True)
        click.echo("Fix: commit or stash your changes, then retry.", err=True)
        raise click.exceptions.Exit(code=1)


def _sentrux_baseline_path(project_root: str) -> Path:
    return Path(project_root) / ".sentrux" / "baseline.json"


def _read_sentrux_baseline_quality(project_root: str) -> int | None:
    """Read the saved baseline quality from .sentrux/baseline.json when available.

    Supports quality_signal floats by scaling them (x10000) to match the
    sentrux check scale.
    """
    baseline_path = _sentrux_baseline_path(project_root)
    if not baseline_path.exists():
        return None
    try:
        data = json.loads(baseline_path.read_text())
    except (OSError, ValueError, TypeError):
        return None

    for key in ("quality", "quality_score", "quality_signal"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            if key == "quality_signal" and isinstance(value, float) and value <= 1.0:
                return int(value * 10000)
            return int(value)
    return None


def _baseline_from_empty_project(baseline_path: Path) -> bool:
    if not baseline_path.exists():
        return False
    try:
        bdata = json.loads(baseline_path.read_text())
    except Exception:
        return False
    return bdata.get("total_import_edges") == 0


def _bootstrap_sentrux_baseline(project_root: str, baseline_path: Path) -> int | None:
    """Create a missing baseline once so fresh repos/worktrees can run."""
    click.echo(f"[sentrux] No baseline found at {baseline_path}; bootstrapping baseline...")
    try:
        _run_sentrux(["gate", "--save", project_root], timeout=30.0)
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or str(exc)).strip()
        click.echo(f"Error: failed to create sentrux baseline at {baseline_path}.", err=True)
        if details:
            click.echo(details, err=True)
        raise click.exceptions.Exit(code=1) from exc
    except subprocess.TimeoutExpired as exc:
        click.echo(f"Error: timed out creating sentrux baseline at {baseline_path}.", err=True)
        raise click.exceptions.Exit(code=1) from exc

    click.echo(f"[sentrux] Baseline saved at {baseline_path}")
    return _read_sentrux_baseline_quality(project_root)


def _require_sentrux_baseline(project_root: str) -> int | None:
    """Ensure sentrux is installed and a baseline exists for comparison."""
    if not _sentrux_available():
        click.echo(
            "Error: sentrux not found. Install: https://github.com/sentrux/sentrux",
            err=True,
        )
        raise click.exceptions.Exit(code=1)

    baseline_path = _sentrux_baseline_path(project_root)
    if not baseline_path.exists():
        return _bootstrap_sentrux_baseline(project_root, baseline_path)

    return _read_sentrux_baseline_quality(project_root)


def _parse_sentrux_gate_output(output: str) -> tuple[bool, int | None]:
    degradation = False
    quality_after: int | None = None
    for line in output.splitlines():
        if line.startswith("Quality:") and "->" in line:
            quality_after = _parse_quality(line)
        elif "No degradation" in line or "✓ No degradation" in line:
            degradation = False
        elif "degradation" in line.lower() or "degraded" in line.lower():
            degradation = True
    return degradation, quality_after


def _scan_head_against_sentrux_baseline(
    project_root: str,
    baseline_path: Path,
) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
    with _clean_head_worktree(project_root) as scan_dir:
        scan_sentrux_dir = scan_dir / ".sentrux"
        if scan_sentrux_dir.exists():
            shutil.rmtree(scan_sentrux_dir)
        shutil.copytree(baseline_path.parent, scan_sentrux_dir)
        result = _run_sentrux(["gate", str(scan_dir)], timeout=30.0, check=False)
        offenders: dict[str, object] | None = None
        try:
            offenders = likely_structural_offenders(
                scan_dir,
                cache_root=Path(project_root),
            )
        except Exception:
            offenders = None
        return result, offenders


def _sentrux_compare(project_root: str, baseline_quality: int | None) -> dict[str, object]:
    """Run `sentrux gate` and build a gate_result dict comparing against baseline."""
    gate_result: dict[str, object] = {
        "degradation": None,
        "quality_before": baseline_quality,
        "quality_after": None,
        "structural_offenders": None,
    }
    if not want_json():
        click.echo("[sentrux] Comparing against baseline...")

    baseline_path = _sentrux_baseline_path(project_root)
    if _baseline_from_empty_project(baseline_path):
        gate_result["degradation"] = False
        if not want_json():
            click.echo("[sentrux] Gate result: ✓ clean (empty baseline skipped)")
        return gate_result

    try:
        result, offenders = _scan_head_against_sentrux_baseline(project_root, baseline_path)
    except subprocess.CalledProcessError as e:
        gate_result["error"] = f"Sentrux gate setup failed: {e}"
        if not want_json():
            click.echo(f"[sentrux] Gate setup failed: {e}", err=True)
        return gate_result
    except subprocess.TimeoutExpired as e:
        gate_result["error"] = f"Sentrux gate timed out: {e}"
        if not want_json():
            click.echo(f"[sentrux] Gate comparison failed: {e}", err=True)
        return gate_result

    output = (result.stdout or "") + (result.stderr or "")
    degradation, quality_after = _parse_sentrux_gate_output(output)

    if result.returncode != 0 and not degradation:
        gate_result["error"] = output.strip() or "Sentrux gate failed."
        if not want_json():
            click.echo(f"[sentrux] Gate comparison failed: {gate_result['error']}", err=True)
        return gate_result

    gate_result["degradation"] = degradation
    gate_result["quality_after"] = quality_after
    if degradation and offenders is not None:
        gate_result["structural_offenders"] = offenders
    if not want_json():
        status = "✓ clean" if not degradation else "✗ degradation detected"
        click.echo(f"[sentrux] Gate result: {status}")
    return gate_result


def _make_worker_event_callback(stream: bool = False) -> Callable[[str, str, object], None]:
    """Build a callback that prints worker activity to stderr.

    In the default (non-stream) mode, only `error` and `done` events are
    surfaced. Pass `stream=True` to also print the full thought / tool-call
    firehose — equivalent to the old default behavior, and what `dgov watch`
    shows in a second pane.

    JSON mode suppresses all event callback output regardless.
    """

    def _on_event(task_slug: str, log_type: str, content: object) -> None:
        if want_json():
            return
        if log_type == "error":
            click.echo(f"  [{task_slug}] ERROR: {content}", err=True)
            return
        if log_type == "done":
            click.echo(f"  [{task_slug}] done: {content}", err=True)
            return
        if not stream:
            return
        if log_type == "thought":
            click.echo(f"  [{task_slug}] {str(content)[:120]}", err=True)
        elif log_type == "call" and isinstance(content, dict):
            data = cast("dict[str, object]", content)
            tool = data.get("tool", "?")
            args = cast("dict[str, object]", data.get("args", {}))
            summary = ", ".join(f"{k}={repr(v)[:40]}" for k, v in args.items())
            click.echo(f"  [{task_slug}] {tool}({summary})", err=True)

    return _on_event


def _ensure_compiled_plan(plan: PlanSpec, plan_file: str) -> None:
    if getattr(plan, "source_mtime_max", None) or os.environ.get("DGOV_ALLOW_UNCOMPILED"):
        return
    click.echo(f"Error: Plan {plan_file} is not compiled.", err=True)
    click.echo("dgov requires plans to be compiled via the Plan Tree pipeline.", err=True)
    click.echo("To fix this:", err=True)
    click.echo("1. Ensure your plan is in a directory with a _root.toml.", err=True)
    click.echo("2. Run: dgov compile <dir>", err=True)
    click.echo("3. Run: dgov run <dir>", err=True)
    raise click.exceptions.Exit(code=1)


def _filter_dag_to_task(dag: DagDefinition, only: str | None) -> DagDefinition:
    if only is None:
        return dag
    if only not in dag.tasks:
        click.echo(f"Error: Task '{only}' not found in plan", err=True)
        raise click.exceptions.Exit(code=1)

    to_keep: set[str] = set()
    queue = [only]
    while queue:
        slug = queue.pop()
        if slug in to_keep or slug not in dag.tasks:
            continue
        to_keep.add(slug)
        queue.extend(dag.tasks[slug].depends_on)
    return dag.model_copy(update={"tasks": {k: v for k, v in dag.tasks.items() if k in to_keep}})


def _emit_run_start(dag_name: str, baseline_quality: int | None) -> None:
    if want_json():
        click.echo(
            json.dumps({
                "status": "starting",
                "dag": dag_name,
                "sentrux_baseline": baseline_quality,
            })
        )
        return
    click.echo(f"[sentrux] Baseline quality: {baseline_quality}")


def _emit_run_completed(
    project_root: str,
    plan_name: str,
    run_status: str,
    duration: timedelta,
    gate_result: dict[str, object],
) -> None:
    """Emit run_completed event with final status and Sentrux gate result."""
    emit_event(
        project_root,
        event="run_completed",
        pane=plan_name,
        plan_name=plan_name,
        run_status=run_status,
        duration_s=round(duration.total_seconds(), 2),
        sentrux=gate_result,
    )


def _run_plan_runner(runner: EventDagRunner) -> tuple[dict[str, str], timedelta]:
    try:
        start_time = datetime.now(UTC)
        results = asyncio.run(runner.run())
        end_time = datetime.now(UTC)
        return results, end_time - start_time
    except KeyboardInterrupt:
        _output({"status": "interrupted"})
        raise click.exceptions.Exit(code=130) from None


def _classify_task_results(
    results: dict[str, str],
) -> tuple[list[str], list[str], list[str], list[str]]:
    failed = [slug for slug, status in results.items() if status == "failed"]
    abandoned = [slug for slug, status in results.items() if status in ("abandoned", "timed_out")]
    skipped = [slug for slug, status in results.items() if status == "skipped"]
    succeeded = [slug for slug, status in results.items() if status == "merged"]
    return failed, abandoned, skipped, succeeded


def _sentrux_failed(gate_result: dict[str, object]) -> bool:
    return bool(gate_result.get("degradation")) or bool(gate_result.get("error"))


def _derive_run_status(
    *,
    failed: list[str],
    abandoned: list[str],
    succeeded: list[str],
    sentrux_failed: bool,
) -> str:
    if not failed and not abandoned and not sentrux_failed:
        return "complete"
    if sentrux_failed and not failed and not abandoned:
        return "degraded"
    if succeeded:
        return "partial"
    return "failed"


def _stale_run_state(
    *,
    duration: timedelta,
    failed: list[str],
    skipped: list[str],
    succeeded: list[str],
    task_errors: dict[str, str],
) -> bool:
    return bool(
        duration.total_seconds() < 1.0
        and bool(failed or skipped)
        and not succeeded
        and not task_errors
    )


def _emit_stale_run_warning() -> None:
    click.echo(
        "No tasks were dispatched — prior run state is still in the database.",
        err=True,
    )
    click.echo("  To retry failed tasks:  dgov run --continue <plan>", err=True)
    click.echo("  To start fresh:         dgov run --restart <plan>", err=True)


def _emit_sentrux_warning(gate_result: dict[str, object]) -> None:
    sentrux_message = gate_result.get("error") or "Architectural degradation detected."
    click.echo(f"  sentrux: {sentrux_message}", err=True)
    offenders = gate_result.get("structural_offenders")
    if isinstance(offenders, dict):
        report = format_structural_offender_report({str(k): v for k, v in offenders.items()})
        click.echo(report, err=True)


def _run_log_status(
    *,
    failed: list[str],
    abandoned: list[str],
    gate_result: dict[str, object],
) -> str:
    if _sentrux_failed(gate_result) and not failed and not abandoned:
        return "warn"
    return "ok" if not failed and not abandoned else "fail"


def _append_task_error_lines(lines: list[str], task_errors: dict[str, str] | None) -> None:
    if not task_errors:
        return
    for slug, err in task_errors.items():
        lines.append(f"    error[{slug}]: {err[:200]}")


def _append_task_duration_line(lines: list[str], task_durations: dict[str, float] | None) -> None:
    if not task_durations:
        return
    dur_str = ", ".join(f"{slug}: {duration}s" for slug, duration in task_durations.items())
    lines.append(f"  durations: {dur_str}")


def _format_token_totals(prompt_tokens: int, completion_tokens: int) -> str:
    return f"{prompt_tokens:,} prompt + {completion_tokens:,} completion"


def _append_token_usage_lines(
    lines: list[str],
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    lines.append(f"  prompt_tokens: {prompt_tokens:,}")
    lines.append(f"  completion_tokens: {completion_tokens:,}")


def _append_sentrux_log_lines(lines: list[str], gate_result: dict[str, object]) -> None:
    quality_before = gate_result.get("quality_before")
    quality_after = gate_result.get("quality_after")
    if quality_before is not None:
        lines.append(f"  sentrux: {quality_before} -> {quality_after}")
    if gate_result.get("degradation"):
        lines.append("  sentrux_status: degradation")
    if gate_result.get("error"):
        lines.append(f"  sentrux_error: {str(gate_result['error'])[:200]}")
    offenders = gate_result.get("structural_offenders")
    if not isinstance(offenders, dict):
        return
    summary = format_structural_offender_report({str(k): v for k, v in offenders.items()}).replace(
        "\n", " | "
    )
    lines.append(f"  sentrux_offenders: {summary[:400]}")


def _run_status_and_summary(
    results: dict[str, str],
    task_errors: dict[str, str],
    gate_result: dict[str, object],
    duration: timedelta,
) -> tuple[str, list[str], list[str], list[str], list[str], bool]:
    failed, abandoned, skipped, succeeded = _classify_task_results(results)
    sentrux_failure = _sentrux_failed(gate_result)
    run_status = _derive_run_status(
        failed=failed,
        abandoned=abandoned,
        succeeded=succeeded,
        sentrux_failed=sentrux_failure,
    )
    stale_state = _stale_run_state(
        duration=duration,
        failed=failed,
        skipped=skipped,
        succeeded=succeeded,
        task_errors=task_errors,
    )
    return run_status, failed, abandoned, skipped, succeeded, stale_state


def _emit_run_warnings(
    *,
    failed: list[str],
    abandoned: list[str],
    skipped: list[str],
    succeeded: list[str],
    task_errors: dict[str, str],
    gate_result: dict[str, object],
    duration: timedelta,
) -> None:
    if want_json():
        return
    if _stale_run_state(
        duration=duration,
        failed=failed,
        skipped=skipped,
        succeeded=succeeded,
        task_errors=task_errors,
    ):
        _emit_stale_run_warning()
    for slug, err in task_errors.items():
        click.echo(f"  {slug}: {err[:200]}")
    if abandoned:
        click.echo(
            f"  {len(abandoned)} task(s) abandoned from a prior crashed run. "
            "Use `dgov run --continue` to retry them.",
            err=True,
        )
    if _sentrux_failed(gate_result):
        _emit_sentrux_warning(gate_result)


def _emit_verbose_task_durations(
    *,
    verbose: bool,
    task_durations: dict[str, float],
    token_usage: dict[str, tuple[int, int]],
    results: dict[str, str],
) -> None:
    if not verbose or want_json() or not task_durations:
        return
    click.echo("  per-task:", err=True)
    for slug in sorted(task_durations):
        status = results.get(slug, "?")
        line = f"    {slug}: {task_durations[slug]}s"
        if slug in token_usage:
            prompt_tokens, completion_tokens = token_usage[slug]
            line = f"{line}  ({prompt_tokens:,} + {completion_tokens:,} tokens)"
        click.echo(f"{line}  {status}", err=True)


def _emit_post_run_hint(
    *,
    stream: bool,
    plan_dir: Path | None,
    plan_file: str,
) -> None:
    if stream or want_json():
        return
    hint_target = str(plan_dir) if plan_dir is not None else plan_file
    click.echo(
        f"  Live stream: dgov watch   |   Debrief: dgov plan review {hint_target}",
        err=True,
    )


def _maybe_archive_completed_plan(
    *,
    run_status: str,
    only: str | None,
    plan_dir: Path | None,
    project_root: str,
    dag: DagDefinition,
) -> None:
    if (
        run_status != "complete"
        or only is not None
        or plan_dir is None
        or not is_plan_complete(project_root, dag.name, set(dag.tasks))
    ):
        return
    dest = archive_plan(plan_dir)
    if not want_json():
        click.echo(f"Plan fully deployed → archived to {dest}")


def _cmd_run_plan(
    plan_file: str,
    project_root: str,
    restart: bool = False,
    continue_failed: bool = False,
    only: str | None = None,
    plan_dir: Path | None = None,
    yes: bool = False,
    stream: bool = False,
    verbose: bool = False,
) -> str:
    """Execute a plan TOML with Sentrux quality gates."""
    from dgov.config import load_project_config
    from dgov.plan import PlanValidationError
    from dgov.types import ConstitutionalViolation

    plan = parse_plan_file(plan_file)
    _ensure_compiled_plan(plan, plan_file)

    pc = load_project_config(project_root)
    try:
        dag = compile_plan(
            plan,
            project_agent=pc.default_agent,
            departments=pc.departments,
        )
    except (ConstitutionalViolation, PlanValidationError) as exc:
        raise click.ClickException(str(exc)) from None
    dag = _filter_dag_to_task(dag, only)

    _ensure_git_ready(project_root, yes=yes)

    baseline_quality = _require_sentrux_baseline(project_root)

    runner = EventDagRunner(
        dag,
        session_root=project_root,
        on_event=_make_worker_event_callback(stream=stream),
        restart=restart,
        continue_failed=continue_failed,
    )
    _emit_run_start(dag.name, baseline_quality)

    results, duration = _run_plan_runner(runner)
    gate_result = _sentrux_compare(project_root, baseline_quality)
    failed_now = [s for s, st in results.items() if st == "failed"]
    task_errors = {slug: err for slug, err in runner.task_errors.items() if slug in failed_now}
    token_usage = cast(dict[str, tuple[int, int]], getattr(runner, "token_usage", {}))
    total_prompt_tokens = sum(prompt for prompt, _ in token_usage.values())
    total_completion_tokens = sum(completion for _, completion in token_usage.values())
    run_status, failed, abandoned, skipped, succeeded, _ = _run_status_and_summary(
        results,
        task_errors,
        gate_result,
        duration,
    )
    _emit_run_warnings(
        failed=failed,
        abandoned=abandoned,
        skipped=skipped,
        succeeded=succeeded,
        task_errors=task_errors,
        gate_result=gate_result,
        duration=duration,
    )

    output_data = {
        "status": run_status,
        "succeeded": len(succeeded),
        "failed": len(failed),
        "abandoned": len(abandoned) if abandoned else None,
        "skipped": len(skipped) if skipped else None,
        "failed_tasks": failed if failed else None,
        "abandoned_tasks": abandoned if abandoned else None,
        "task_errors": task_errors if task_errors else None,
        "sentrux": gate_result,
        "duration_s": round(duration.total_seconds(), 2),
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
    }
    if want_json():
        _output(output_data)
    else:
        hidden_human_fields = {"total_prompt_tokens", "total_completion_tokens"}
        _output({k: v for k, v in output_data.items() if k not in hidden_human_fields})
        click.echo(f"tokens: {_format_token_totals(total_prompt_tokens, total_completion_tokens)}")
    _emit_verbose_task_durations(
        verbose=verbose,
        task_durations=runner.task_durations,
        token_usage=token_usage,
        results=results,
    )
    _emit_post_run_hint(stream=stream, plan_dir=plan_dir, plan_file=plan_file)

    _append_run_log(
        project_root,
        dag.name,
        plan_file,
        results,
        gate_result,
        duration,
        runner.task_durations,
        task_errors,
        total_prompt_tokens,
        total_completion_tokens,
    )
    _maybe_archive_completed_plan(
        run_status=run_status,
        only=only,
        plan_dir=plan_dir,
        project_root=project_root,
        dag=dag,
    )

    _emit_run_completed(
        project_root=project_root,
        plan_name=dag.name,
        run_status=run_status,
        duration=duration,
        gate_result=gate_result,
    )

    if run_status in ("failed", "partial"):
        raise click.exceptions.Exit(code=1)
    return run_status


def _append_run_log(
    project_root: str,
    plan_name: str,
    plan_file: str,
    results: dict[str, str],
    gate_result: dict[str, object],
    duration: timedelta,
    task_durations: dict[str, float] | None = None,
    task_errors: dict[str, str] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """Append a run summary to .dgov/runs.log — permanent, git-tracked."""
    log_path = Path(project_root) / ".dgov" / "runs.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    failed, abandoned, _, merged = _classify_task_results(results)
    status = _run_log_status(failed=failed, abandoned=abandoned, gate_result=gate_result)

    lines = [
        f"[{ts}] {plan_name} ({plan_file}) — {status} ({round(duration.total_seconds(), 2)}s)"
    ]
    if merged:
        lines.append(f"  merged: {', '.join(merged)}")
    if failed:
        lines.append(f"  failed: {', '.join(failed)}")
    if abandoned:
        lines.append(f"  abandoned: {', '.join(abandoned)}")
    _append_task_error_lines(lines, task_errors)
    _append_token_usage_lines(lines, prompt_tokens, completion_tokens)
    _append_task_duration_line(lines, task_durations)
    _append_sentrux_log_lines(lines, gate_result)
    lines.append("")

    with log_path.open("a") as f:
        f.write("\n".join(lines) + "\n")
