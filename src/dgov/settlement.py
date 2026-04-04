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
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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


def _check_git_diff(worktree_path: Path, max_diff_lines: int) -> ReviewResult | None:
    """Check git diff --stat for size limits. Returns ReviewResult on failure, None on OK."""
    diff_stat = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if diff_stat.returncode != 0:
        return ReviewResult(passed=False, verdict="git_error", error="git diff --stat failed")

    diff_lines = diff_stat.stdout.strip()
    if not diff_lines:
        return ReviewResult(passed=False, verdict="empty_diff", error="No changes produced")

    stat_lines = diff_lines.split("\n")
    if len(stat_lines) > max_diff_lines:
        return ReviewResult(
            passed=False,
            verdict="diff_too_large",
            error=f"Diff has {len(stat_lines)} files/lines, max is {max_diff_lines}",
        )
    return None


def _get_changed_files(worktree_path: Path) -> frozenset[str] | ReviewResult:
    """Get set of changed files. Returns ReviewResult on failure."""
    diff_names = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if diff_names.returncode != 0:
        return ReviewResult(passed=False, verdict="git_error", error="git diff --name-only failed")

    actual_files = frozenset(f for f in diff_names.stdout.strip().split("\n") if f)
    if not actual_files:
        return ReviewResult(passed=False, verdict="empty_diff", error="No files changed")
    return actual_files


def _check_dirty_worktree(
    worktree_path: Path, actual_files: frozenset[str]
) -> ReviewResult | None:
    """Check for untracked/unstaged files. Returns ReviewResult on failure, None on OK."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return ReviewResult(
            passed=False, verdict="git_error", error="git status --porcelain failed"
        )

    # Parse porcelain output: XY PATH or XY ORIG_PATH -> PATH (for renames)
    # X = index status, Y = working tree status
    # ?? = untracked, anything else in Y = unstaged changes
    for line in status.stdout.strip().split("\n"):
        if not line:
            continue
        xy = line[:2]
        # Untracked files or unstaged modifications
        if xy == "??" or (xy[1] != " " and xy[1] != "?"):
            return ReviewResult(
                passed=False,
                verdict="dirty_worktree",
                actual_files=actual_files,
                error="Worker left untracked or unstaged files",
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
    2. Scope enforcement (touched unclaimed files)
    3. Diff size (runaway worker)
    4. Dirty worktree (left mess)

    Returns ReviewResult with actual_files for downstream settlement.
    """
    try:
        # 1. Check diff size
        result = _check_git_diff(worktree_path, max_diff_lines)
        if result is not None:
            return result

        # 2. Get changed files
        files_result = _get_changed_files(worktree_path)
        if isinstance(files_result, ReviewResult):
            return files_result
        actual_files = files_result

        # 3. Check dirty worktree
        result = _check_dirty_worktree(worktree_path, actual_files)
        if result is not None:
            return result

        # 4. Scope enforcement
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


def autofix_sandbox(worktree_path: Path) -> None:
    """Mechanical auto-fix: format + lint fix. Called BEFORE commit.

    Modifies files in-place. Safe because nothing is committed yet.
    """
    py_files = list(worktree_path.rglob("*.py"))
    if not py_files:
        return
    rel = [str(f.relative_to(worktree_path)) for f in py_files]
    subprocess.run(["ruff", "format", *rel], cwd=worktree_path, capture_output=True)
    subprocess.run(["ruff", "check", "--fix", *rel], cwd=worktree_path, capture_output=True)


def validate_sandbox(worktree_path: Path, base_commit: str, project_root: str) -> GateResult:
    """Read-only validation gate. Called AFTER commit. No mutations."""
    try:
        # 1. Identify changed python files
        diff_res = subprocess.run(
            ["git", "diff", "--name-only", base_commit, "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        )
        changed_files = [f for f in diff_res.stdout.strip().split("\n") if f.endswith(".py")]

        if not changed_files:
            return GateResult(passed=True)

        # 2. Lint gate (no --fix)
        res_ruff = subprocess.run(
            ["ruff", "check", *changed_files],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if res_ruff.returncode != 0:
            return GateResult(passed=False, error=f"Lint failure:\n{res_ruff.stdout}")

        # 3. Format check (no modification)
        res_fmt = subprocess.run(
            ["ruff", "format", "--check", *changed_files],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if res_fmt.returncode != 0:
            return GateResult(passed=False, error=f"Format failure:\n{res_fmt.stdout}")

        # 4. Sentrux gate (policy) — skipped if no baseline exists
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
                return GateResult(passed=False, error=f"Policy violation (Sentrux):\n{sx_output}")

        return GateResult(passed=True)

    except Exception as exc:
        return GateResult(passed=False, error=f"Unexpected validation error: {exc}")
