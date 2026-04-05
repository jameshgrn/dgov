"""Settlement Layer: Validation Gates and Commit-or-Kill logic.

Pillar #8: Falsifiable Validation - All work is machine-verified before merge.
Pillar #10: Fail-Closed - Rejected work is never merged.

Three phases:
1. review_sandbox() — FAST git sanity checks BEFORE settlement (microseconds)
2. autofix_sandbox() — mechanical fixes (format, lint --fix) BEFORE commit
3. validate_sandbox() — read-only gate AFTER commit (milliseconds)
"""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dgov.config import ProjectConfig, load_project_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateResult:
    """The outcome of a validation gate."""

    passed: bool
    error: Optional[str] = None


@dataclass(frozen=True)
class ReviewResult:
    """The outcome of a fast review gate."""

    passed: bool
    verdict: str
    actual_files: frozenset[str] = frozenset()
    error: Optional[str] = None


def _run_cmd(
    cmd_template: str, files: list[str], cwd: Path, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    """Run a command template, substituting {file} with the file list."""
    file_args = " ".join(shlex.quote(f) for f in files)
    cmd = cmd_template.replace("{file}", file_args)
    return subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def _get_all_changes(worktree_path: Path) -> frozenset[str] | ReviewResult:
    """Get ALL changed/new files via git status --porcelain.

    Unlike git diff, this catches untracked files too — critical for workers
    that create new files.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return ReviewResult(passed=False, verdict="git_error", error="git status failed")

    files = set()
    for line in status.stdout.rstrip("\n").split("\n"):
        if not line:
            continue
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        files.add(path_part)

    if not files:
        return ReviewResult(passed=False, verdict="empty_diff", error="No changes produced")
    return frozenset(files)


def _check_size(actual_files: frozenset[str], max_diff_lines: int) -> ReviewResult | None:
    """Check file count against size limit."""
    if len(actual_files) > max_diff_lines:
        return ReviewResult(
            passed=False,
            verdict="diff_too_large",
            error=f"Diff has {len(actual_files)} files, max is {max_diff_lines}",
        )
    return None


def _check_scope(
    actual_files: frozenset[str], claimed_files: list[str] | None
) -> ReviewResult | None:
    """Check that changed files are within claimed scope. Returns ReviewResult on failure."""
    if not claimed_files:
        return None

    claimed = frozenset(claimed_files)
    unclaimed = actual_files - claimed
    if unclaimed:
        return ReviewResult(
            passed=False,
            verdict="scope_violation",
            actual_files=actual_files,
            error=f"Touched unclaimed files: {sorted(unclaimed)}",
        )
    return None


def review_sandbox(
    worktree_path: Path, claimed_files: Optional[list[str]] = None, max_diff_lines: int = 100
) -> ReviewResult:
    """FAST review gate — git sanity checks in microseconds.

    Checks:
    1. Empty diff (worker produced nothing)
    2. Diff size (runaway worker)
    3. Scope enforcement (touched unclaimed files)

    Uses git status --porcelain to see ALL changes including new files.
    """
    try:
        # 1. Get all changed files (tracked + untracked)
        files_result = _get_all_changes(worktree_path)
        if isinstance(files_result, ReviewResult):
            return files_result
        actual_files = files_result

        # 2. Check size
        result = _check_size(actual_files, max_diff_lines)
        if result is not None:
            return result

        # 3. Scope enforcement
        result = _check_scope(actual_files, claimed_files)
        if result is not None:
            return result

        # All checks passed
        return ReviewResult(
            passed=True,
            verdict="ok",
            actual_files=actual_files,
        )

    except Exception as exc:
        return ReviewResult(passed=False, verdict="exception", error=f"Review failed: {exc}")


def autofix_sandbox(
    worktree_path: Path,
    file_claims: tuple[str, ...] = (),
    config: ProjectConfig | None = None,
) -> None:
    """Mechanical auto-fix: lint fix then format. Called BEFORE commit.

    Order matters: lint fix can change formatting, so format runs LAST.
    Scoped to claimed files if provided, otherwise all source files.
    """
    if config is None:
        config = load_project_config(worktree_path)

    extensions = config.source_extensions

    if file_claims:
        rel = [
            f
            for f in file_claims
            if any(f.endswith(ext) for ext in extensions) and (worktree_path / f).exists()
        ]
    else:
        source_files: list[Path] = []
        for ext in extensions:
            source_files.extend(worktree_path.rglob(f"*{ext}"))
        if not source_files:
            return
        rel = [str(f.relative_to(worktree_path)) for f in source_files]

    if not rel:
        return

    # Lint fix first (may remove imports, change lines)
    _run_cmd(config.lint_fix_cmd, rel, worktree_path)
    # Format LAST (canonical formatting after all mutations)
    _run_cmd(config.format_cmd, rel, worktree_path)


def validate_sandbox(
    worktree_path: Path,
    base_commit: str,
    project_root: str,
    config: ProjectConfig | None = None,
) -> GateResult:
    """Read-only validation gate. Called AFTER commit. No mutations."""
    if config is None:
        config = load_project_config(project_root)

    extensions = config.source_extensions

    try:
        # 1. Identify changed source files
        diff_res = subprocess.run(
            ["git", "diff", "--name-only", base_commit, "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        changed_files = [
            f
            for f in diff_res.stdout.strip().split("\n")
            if any(f.endswith(ext) for ext in extensions)
        ]

        if not changed_files:
            return GateResult(passed=True)

        # 2. Lint gate (no --fix)
        res_lint = _run_cmd(config.lint_cmd, changed_files, worktree_path)
        if res_lint.returncode != 0:
            return GateResult(passed=False, error=f"Lint failure:\n{res_lint.stdout}")

        # 3. Format check (no modification)
        res_fmt = _run_cmd(config.format_check_cmd, changed_files, worktree_path)
        if res_fmt.returncode != 0:
            return GateResult(passed=False, error=f"Format failure:\n{res_fmt.stdout}")

        # 4. Test gate — run full suite if source files changed, else only changed tests
        test_dir = config.test_dir.rstrip("/")
        test_dir_exists = (worktree_path / test_dir).is_dir()
        test_files = [f for f in changed_files if f.startswith(test_dir)]
        source_files = [f for f in changed_files if not f.startswith(test_dir)]
        if source_files and test_dir_exists:
            # Source code changed — must run the full test suite to catch regressions
            test_cmd = config.test_cmd.replace("{test_dir}", config.test_dir)
        elif test_files:
            # Only test files changed — run just those
            test_cmd = config.test_cmd.replace(
                "{test_dir}", " ".join(shlex.quote(f) for f in test_files)
            )
        else:
            test_cmd = ""
        if test_cmd:
            res_test = subprocess.run(
                test_cmd,
                shell=True,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if res_test.returncode != 0:
                output = (res_test.stdout + res_test.stderr)[-500:]
                return GateResult(passed=False, error=f"Test failure:\n{output}")

        # 5. Sentrux gate (policy) — warn on degradation, don't hard-fail
        baseline = Path(project_root) / ".sentrux" / "baseline.json"
        if baseline.exists():
            sx_dst = worktree_path / ".sentrux"
            if not sx_dst.exists():
                shutil.copytree(baseline.parent, sx_dst, dirs_exist_ok=True)
            with tempfile.TemporaryFile(mode="w+") as tmp:
                res_sx = subprocess.run(
                    ["sentrux", "gate", "."],
                    cwd=worktree_path,
                    stdout=tmp,
                    stderr=subprocess.STDOUT,
                )
                tmp.seek(0)
                sx_output = tmp.read()

            if res_sx.returncode != 0:
                logger.warning("Sentrux policy warning:\n%s", sx_output)

        return GateResult(passed=True)

    except Exception as exc:
        return GateResult(passed=False, error=f"Unexpected validation error: {exc}")
