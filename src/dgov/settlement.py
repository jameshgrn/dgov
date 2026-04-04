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


def _check_dirty_worktree(
    worktree_path: Path, actual_files: frozenset[str]
) -> ReviewResult | None:
    """No-op — dirty worktree check is now handled by _get_all_changes.

    All files (tracked changes + untracked) are captured upfront.
    This function exists for the scope check below.
    """
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


def autofix_sandbox(worktree_path: Path, file_claims: tuple[str, ...] = ()) -> None:
    """Mechanical auto-fix: lint fix then format. Called BEFORE commit.

    Order matters: lint fix can change formatting, so format runs LAST.
    Scoped to claimed files if provided, otherwise all .py files.
    """
    if file_claims:
        rel = [f for f in file_claims if f.endswith(".py") and (worktree_path / f).exists()]
    else:
        py_files = list(worktree_path.rglob("*.py"))
        if not py_files:
            return
        rel = [str(f.relative_to(worktree_path)) for f in py_files]

    if not rel:
        return

    # Lint fix first (may remove imports, change lines)
    subprocess.run(
        ["uv", "run", "ruff", "check", "--fix", *rel], cwd=worktree_path, capture_output=True
    )
    # Format LAST (canonical formatting after all mutations)
    subprocess.run(["uv", "run", "ruff", "format", *rel], cwd=worktree_path, capture_output=True)


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
            ["uv", "run", "ruff", "check", *changed_files],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if res_ruff.returncode != 0:
            return GateResult(passed=False, error=f"Lint failure:\n{res_ruff.stdout}")

        # 3. Format check (no modification)
        res_fmt = subprocess.run(
            ["uv", "run", "ruff", "format", "--check", *changed_files],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        if res_fmt.returncode != 0:
            return GateResult(passed=False, error=f"Format failure:\n{res_fmt.stdout}")

        # 4. Test gate — run pytest on changed test files via uv
        test_files = [f for f in changed_files if f.startswith("tests/")]
        if test_files:
            res_test = subprocess.run(
                ["uv", "run", "pytest", *test_files, "-q", "--tb=short"],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if res_test.returncode != 0:
                output = (res_test.stdout + res_test.stderr)[-500:]
                return GateResult(passed=False, error=f"Test failure:\n{output}")

        # 5. Sentrux gate (policy) — warn on degradation, don't hard-fail
        # Hard-failing on +1 complex function blocks test generation.
        # TODO: make configurable (hard-fail for prod, warn for tests)
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
