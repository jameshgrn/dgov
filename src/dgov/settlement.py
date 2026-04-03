"""Settlement Layer: Validation Gates and Commit-or-Kill logic.

Pillar #8: Falsifiable Validation - All work is machine-verified before merge.
Pillar #10: Fail-Closed - Rejected work is never merged.

Two phases:
1. autofix_sandbox() — mechanical fixes (format, lint --fix) BEFORE commit
2. validate_sandbox() — read-only gate AFTER commit
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
