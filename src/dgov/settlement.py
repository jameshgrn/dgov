"""Settlement Layer: Validation Gates and Commit-or-Kill logic.

Pillar #8: Falsifiable Validation - All work is machine-verified before merge.
Pillar #10: Fail-Closed - Cleanup happens automatically on rejection.
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


def validate_sandbox(worktree_path: Path, base_commit: str, project_root: str) -> GateResult:
    """Run Ruff and Sentrux checks on changed files in the sandbox."""
    try:
        # 0. Pillar #3: Inject policy context
        sx_src = Path(project_root) / ".sentrux"
        sx_dst = worktree_path / ".sentrux"
        if sx_src.exists():
            shutil.copytree(sx_src, sx_dst, dirs_exist_ok=True)

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

        # 2. Pillar #8: Auto-Fix (Mechanical Quality)
        subprocess.run(["ruff", "format", *changed_files], cwd=worktree_path, capture_output=True)
        subprocess.run(
            ["ruff", "check", "--fix", *changed_files], cwd=worktree_path, capture_output=True
        )

        # 3. Final Lint Check
        res_ruff = subprocess.run(
            ["ruff", "check", *changed_files], cwd=worktree_path, capture_output=True, text=True
        )
        if res_ruff.returncode != 0:
            return GateResult(
                passed=False, error=f"Lint failure after auto-fix:\n{res_ruff.stdout}"
            )

        # 4. Sentrux Gate (Policy)
        # Use temp file to avoid pipe deadlock (Pillar #9)
        with tempfile.TemporaryFile(mode="w+") as tmp:
            res_sx = subprocess.run(
                ["sentrux", "gate", "."], cwd=worktree_path, stdout=tmp, stderr=subprocess.STDOUT
            )
            tmp.seek(0)
            sx_output = tmp.read()

        if res_sx.returncode != 0:
            # NO LAZY FIX: If it's degraded, it's rejected.
            return GateResult(passed=False, error=f"Policy violation (Sentrux):\n{sx_output}")

        return GateResult(passed=True)

    except Exception as exc:
        return GateResult(passed=False, error=f"Unexpected validation error: {exc}")
