"""Post-run git and Sentrux checks for `dgov run`."""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import click

from dgov.config import ProjectConfig
from dgov.repo_snapshot import format_structural_offender_report, likely_structural_offenders
from dgov.sentrux_baseline import (
    SentruxBaselineRefreshError,
    refresh_sentrux_baseline_after_clean_run,
    sentrux_baseline_path,
)
from dgov.sentrux_gate import SentruxGateAssessment, assess_sentrux_gate, changed_files_since

SentruxRunner = Callable[..., subprocess.CompletedProcess[str]]
JsonMode = Callable[[], bool]
GitStdout = Callable[[str, list[str]], str | None]
SentruxScan = tuple[
    subprocess.CompletedProcess[str],
    dict[str, object] | None,
    SentruxGateAssessment | None,
]


@contextlib.contextmanager
def _clean_head_worktree(project_root: str) -> Iterator[Path]:
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


@contextlib.contextmanager
def _detached_worktree(project_root: str, ref: str, prefix: str) -> Iterator[Path]:
    tmp_root = Path(tempfile.mkdtemp(prefix=prefix))
    wt_path = tmp_root / "checkout"
    created = False
    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(wt_path), ref],
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


def read_sentrux_baseline_quality(project_root: str) -> int | None:
    baseline_path = sentrux_baseline_path(project_root)
    if not baseline_path.exists():
        return None
    try:
        data = json.loads(baseline_path.read_text())
    except (OSError, ValueError, TypeError):
        return None

    for key in ("quality", "quality_score", "quality_signal"):
        value = data.get(key)
        if isinstance(value, int | float):
            if key == "quality_signal" and isinstance(value, float) and value <= 1.0:
                return int(value * 10000)
            return int(value)
    return None


def require_sentrux_baseline(
    project_root: str,
    *,
    run_sentrux: SentruxRunner,
    sentrux_available: Callable[[], bool],
) -> int | None:
    root = str(Path(project_root).resolve())
    if not sentrux_available():
        click.echo(
            "Error: sentrux not found. Install: https://github.com/sentrux/sentrux",
            err=True,
        )
        raise click.exceptions.Exit(code=1)

    baseline_path = sentrux_baseline_path(root)
    if not baseline_path.exists():
        return _bootstrap_sentrux_baseline(root, baseline_path, run_sentrux=run_sentrux)

    return read_sentrux_baseline_quality(root)


def _bootstrap_sentrux_baseline(
    project_root: str,
    baseline_path: Path,
    *,
    run_sentrux: SentruxRunner,
) -> int | None:
    root = str(Path(project_root).resolve())
    click.echo(f"[sentrux] No baseline found at {baseline_path}; bootstrapping baseline...")
    try:
        run_sentrux(["gate", "--save", root], timeout=30.0)
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
    return read_sentrux_baseline_quality(root)


def parse_sentrux_gate_output(output: str) -> tuple[bool, int | None]:
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


def sentrux_compare(
    project_root: str,
    baseline_quality: int | None,
    *,
    base_ref: str | None,
    config: ProjectConfig | None,
    run_sentrux: SentruxRunner,
    want_json: JsonMode,
) -> dict[str, object]:
    root = str(Path(project_root).resolve())
    gate_result = _initial_sentrux_gate_result(baseline_quality)
    if not want_json():
        click.echo("[sentrux] Comparing against baseline...")

    baseline_path = sentrux_baseline_path(root)
    if _baseline_from_empty_project(baseline_path):
        return _empty_baseline_sentrux_result(gate_result, want_json=want_json)

    scan = _scan_sentrux_baseline_or_error(
        gate_result,
        root,
        baseline_path,
        base_ref=base_ref,
        config=config,
        run_sentrux=run_sentrux,
        want_json=want_json,
    )
    if isinstance(scan, dict):
        return scan
    result, offenders, assessment = scan

    return _build_sentrux_gate_result_from_scan(
        gate_result,
        result,
        offenders,
        assessment,
        want_json=want_json,
    )


def branch_verification_gate(
    project_root: str,
    config: object,
    *,
    git_stdout: GitStdout,
) -> dict[str, object]:
    from dgov.settlement import validate_sandbox

    context = _branch_verification_context(project_root, config, git_stdout=git_stdout)
    if isinstance(context, dict):
        return context
    pc, base_ref, result, changed_files = context
    if not changed_files:
        return result

    with _detached_worktree(project_root, base_ref, "dgov-branch-base-") as baseline_path:
        gate = validate_sandbox(
            Path(project_root),
            base_ref,
            project_root,
            config=pc,
            type_baseline_path=baseline_path,
        )

    if gate.passed:
        return result
    return {
        **result,
        "status": "failed",
        "error": gate.error or "Branch verification failed",
    }


def _scan_sentrux_baseline_or_error(
    gate_result: dict[str, object],
    root: str,
    baseline_path: Path,
    *,
    base_ref: str | None,
    config: ProjectConfig | None,
    run_sentrux: SentruxRunner,
    want_json: JsonMode,
) -> SentruxScan | dict[str, object]:
    try:
        return _scan_head_against_sentrux_baseline(
            root,
            baseline_path,
            base_ref=base_ref,
            config=config,
            run_sentrux=run_sentrux,
        )
    except subprocess.CalledProcessError as exc:
        return _record_sentrux_compare_error(
            gate_result,
            message=f"Sentrux gate setup failed: {exc}",
            echo=f"[sentrux] Gate setup failed: {exc}",
            want_json=want_json,
        )
    except subprocess.TimeoutExpired as exc:
        return _record_sentrux_compare_error(
            gate_result,
            message=f"Sentrux gate timed out: {exc}",
            echo=f"[sentrux] Gate comparison failed: {exc}",
            want_json=want_json,
        )


def _branch_verification_context(
    project_root: str,
    config: object,
    *,
    git_stdout: GitStdout,
) -> tuple[ProjectConfig, str, dict[str, object], list[str]] | dict[str, object]:
    if not isinstance(config, ProjectConfig):
        return {"status": "skipped", "reason": "invalid project config"}
    base_ref = _branch_verification_base(project_root, git_stdout=git_stdout)
    if not base_ref:
        return {"status": "skipped", "reason": "no merge base found"}
    changed_files = _branch_changed_source_files(
        project_root,
        base_ref,
        config.source_extensions,
        git_stdout=git_stdout,
    )
    return (
        config,
        base_ref,
        {
            "status": "clean",
            "base": base_ref,
            "head": git_stdout(project_root, ["rev-parse", "HEAD"]),
            "changed_files": len(changed_files),
        },
        changed_files,
    )


def refresh_accepted_sentrux_baseline(
    project_root: str,
    *,
    run_sentrux: SentruxRunner,
) -> bool:
    root = Path(project_root).resolve()
    try:
        return refresh_sentrux_baseline_after_clean_run(str(root), run_sentrux=run_sentrux)
    except SentruxBaselineRefreshError as exc:
        raise click.ClickException(str(exc)) from exc


def format_offender_report(offenders: Mapping[object, object]) -> str:
    return format_structural_offender_report({str(k): v for k, v in offenders.items()})


def _parse_quality(line: str) -> int | None:
    if not line.startswith("Quality:"):
        return None
    rest = line.split(":", 1)[1].strip()
    token = rest.split("->")[-1].strip() if "->" in rest else rest
    try:
        return int(token)
    except ValueError:
        try:
            val = float(token)
        except ValueError:
            return None
    return int(val * 10000) if val <= 1.0 else int(val)


def _baseline_from_empty_project(baseline_path: Path) -> bool:
    if not baseline_path.exists():
        return False
    try:
        bdata = json.loads(baseline_path.read_text())
    except Exception:
        return False
    return bdata.get("total_import_edges") == 0


def _head_structural_offenders(scan_dir: Path, project_root: str) -> dict[str, object] | None:
    try:
        return likely_structural_offenders(
            scan_dir,
            cache_root=Path(project_root),
        )
    except Exception:
        return None


def _assess_head_sentrux_degradation(
    *,
    scan_dir: Path,
    project_root: str,
    baseline_path: Path,
    output: str,
    returncode: int,
    base_ref: str | None,
    config: ProjectConfig | None,
) -> SentruxGateAssessment:
    pc = config or ProjectConfig()
    changed_files = (
        changed_files_since(Path(project_root), base_ref, pc.source_extensions) if base_ref else []
    )
    return assess_sentrux_gate(
        scan_root=scan_dir,
        project_root=Path(project_root),
        baseline_path=baseline_path,
        sentrux_output=output,
        sentrux_returncode=returncode,
        changed_files=changed_files,
        base_ref=base_ref,
        mode=pc.sentrux_mode,
        stale_commits=pc.sentrux_stale_commits,
        stale_days=pc.sentrux_stale_days,
    )


def _scan_head_against_sentrux_baseline(
    project_root: str,
    baseline_path: Path,
    *,
    base_ref: str | None,
    config: ProjectConfig | None,
    run_sentrux: SentruxRunner,
) -> tuple[
    subprocess.CompletedProcess[str],
    dict[str, object] | None,
    SentruxGateAssessment | None,
]:
    with _clean_head_worktree(project_root) as scan_dir:
        scan_sentrux_dir = scan_dir / ".sentrux"
        if scan_sentrux_dir.exists():
            shutil.rmtree(scan_sentrux_dir)
        shutil.copytree(baseline_path.parent, scan_sentrux_dir)
        result = run_sentrux(["gate", str(scan_dir)], timeout=30.0, check=False)
        offenders = _head_structural_offenders(scan_dir, project_root)
        output = (result.stdout or "") + (result.stderr or "")
        degradation, _ = parse_sentrux_gate_output(output)
        assessment = None
        if degradation:
            assessment = _assess_head_sentrux_degradation(
                scan_dir=scan_dir,
                project_root=project_root,
                baseline_path=baseline_path,
                output=output,
                returncode=result.returncode,
                base_ref=base_ref,
                config=config,
            )
        return result, offenders, assessment


def _initial_sentrux_gate_result(baseline_quality: int | None) -> dict[str, object]:
    return {
        "degradation": None,
        "quality_before": baseline_quality,
        "quality_after": None,
        "structural_offenders": None,
    }


def _record_sentrux_compare_error(
    gate_result: dict[str, object],
    *,
    message: str,
    echo: str,
    want_json: JsonMode,
) -> dict[str, object]:
    gate_result["error"] = message
    if not want_json():
        click.echo(echo, err=True)
    return gate_result


def _empty_baseline_sentrux_result(
    gate_result: dict[str, object],
    *,
    want_json: JsonMode,
) -> dict[str, object]:
    gate_result["degradation"] = False
    if not want_json():
        click.echo("[sentrux] Gate result: ✓ clean (empty baseline skipped)")
    return gate_result


def _failed_sentrux_process_result(
    gate_result: dict[str, object],
    *,
    result: subprocess.CompletedProcess[str],
    output: str,
    degradation: bool,
    want_json: JsonMode,
) -> dict[str, object] | None:
    if result.returncode == 0 or degradation:
        return None
    error = output.strip() or "Sentrux gate failed."
    return _record_sentrux_compare_error(
        gate_result,
        message=error,
        echo=f"[sentrux] Gate comparison failed: {error}",
        want_json=want_json,
    )


def _complete_sentrux_gate_result(
    gate_result: dict[str, object],
    *,
    degradation: bool,
    quality_after: int | None,
    offenders: dict[str, object] | None,
    error: str | None,
    warning: str | None,
    want_json: JsonMode,
) -> dict[str, object]:
    gate_result["degradation"] = degradation
    gate_result["quality_after"] = quality_after
    if error:
        gate_result["error"] = error
    if warning:
        gate_result["warning"] = warning
    if degradation and offenders is not None:
        gate_result["structural_offenders"] = offenders
    if not want_json():
        status = "✓ clean" if not degradation else "✗ degradation detected"
        click.echo(f"[sentrux] Gate result: {status}")
        if warning:
            click.echo(warning, err=True)
    return gate_result


def _normalize_sentrux_assessment(
    assessment: SentruxGateAssessment | None,
    offenders: dict[str, object] | None,
    degradation: bool,
) -> tuple[bool, dict[str, object] | None, str | None, str | None]:
    if assessment is None:
        return degradation, offenders, None, None
    return (
        assessment.should_fail,
        assessment.current_report or offenders,
        assessment.error if assessment.should_fail else None,
        assessment.warning,
    )


def _build_sentrux_gate_result_from_scan(
    gate_result: dict[str, object],
    result: subprocess.CompletedProcess[str],
    offenders: dict[str, object] | None,
    assessment: SentruxGateAssessment | None,
    *,
    want_json: JsonMode,
) -> dict[str, object]:
    output = (result.stdout or "") + (result.stderr or "")
    degradation, quality_after = parse_sentrux_gate_output(output)
    failed_result = _failed_sentrux_process_result(
        gate_result,
        result=result,
        output=output,
        degradation=degradation,
        want_json=want_json,
    )
    if failed_result is not None:
        return failed_result
    degradation, offenders, error, warning = _normalize_sentrux_assessment(
        assessment, offenders, degradation
    )
    return _complete_sentrux_gate_result(
        gate_result,
        degradation=degradation,
        quality_after=quality_after,
        offenders=offenders,
        error=error,
        warning=warning,
        want_json=want_json,
    )


def _branch_verification_base(project_root: str, *, git_stdout: GitStdout) -> str | None:
    candidates: list[str] = []
    origin_head = git_stdout(project_root, ["rev-parse", "--abbrev-ref", "origin/HEAD"])
    if origin_head and origin_head != "origin/HEAD":
        candidates.append(origin_head)
    candidates.extend(["origin/main", "origin/master", "main", "master"])

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        base = git_stdout(project_root, ["merge-base", "HEAD", candidate])
        if base:
            return base
    return None


def _branch_changed_source_files(
    project_root: str,
    base_ref: str,
    source_extensions: tuple[str, ...],
    *,
    git_stdout: GitStdout,
) -> list[str]:
    output = git_stdout(project_root, ["diff", "--name-only", base_ref, "HEAD"])
    if not output:
        return []
    seen: set[str] = set()
    files: list[str] = []
    for path in output.splitlines():
        if path in seen or not any(path.endswith(ext) for ext in source_extensions):
            continue
        seen.add(path)
        files.append(path)
    return files
