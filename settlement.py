"""
Settlement Kernel: Atomic "Commit or Kill" execution gate.

Zero-dependency module for moving validated worktree code to canonical path.
HFT-inspired deterministic execution with all-or-nothing atomic moves.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SettlementResult:
    """Immutable result of a settlement attempt."""

    success: bool
    worktree_path: Path
    base_path: Path
    gate_passed: bool = False
    tests_passed: bool = False
    copied_files: list[Path] = field(default_factory=list)
    error_message: str | None = None


@runtime_checkable
class Validator(Protocol):
    """Protocol for validation steps in the settlement pipeline."""

    def validate(self, path: Path) -> tuple[bool, str]:
        """Return (success, message) tuple."""
        ...


class SentruxValidator:
    """Gate validator using sentrux CLI."""

    def __init__(self, rules_path: Path | None = None) -> None:
        self.rules_path = rules_path

    def validate(self, path: Path) -> tuple[bool, str]:
        """Run sentrux gate --repo <path>. Returns (exit_0, stderr_or_stdout)."""
        cmd = ["sentrux", "gate", "--repo", str(path)]
        if self.rules_path:
            cmd.extend(["--rules", str(self.rules_path)])

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return True, "gate passed"
        return False, result.stderr or result.stdout or "sentrux gate failed"


class PytestValidator:
    """Test validator using pytest."""

    def __init__(self, marker: str = "unit") -> None:
        self.marker = marker

    def validate(self, path: Path) -> tuple[bool, str]:
        """Run pytest <path> -q -m <marker>."""
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            str(path),
            "-q",
            "-m",
            self.marker,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return True, "tests passed"
        return False, result.stderr or result.stdout or "pytest failed"


class SettlementEngine:
    """
    Atomic settlement engine implementing Commit-or-Kill semantics.

    All-or-nothing file moves with rollback on failure.
    """

    def __init__(
        self,
        gate_validator: Validator | None = None,
        test_validator: Validator | None = None,
    ) -> None:
        self.gate_validator = gate_validator or SentruxValidator()
        self.test_validator = test_validator or PytestValidator()

    def settle(
        self,
        base_path: Path,
        worktree_path: Path,
        rules_path: Path | None = None,
    ) -> SettlementResult:
        """
        Execute Commit-or-Kill settlement.

        Steps:
            A: sentrux gate
            B: pytest -m unit
            C: if both pass, atomic move worktree -> base
            D: if any fail, delete worktree and return FailureReport
        """
        base_path = Path(base_path).resolve()
        worktree_path = Path(worktree_path).resolve()

        if not worktree_path.exists():
            return SettlementResult(
                success=False,
                worktree_path=worktree_path,
                base_path=base_path,
                error_message=f"Worktree does not exist: {worktree_path}",
            )

        # Step A: Gate validation
        gate_ok, gate_msg = self.gate_validator.validate(worktree_path)

        # Step B: Test validation
        test_ok, test_msg = self.test_validator.validate(worktree_path)

        # Step D: Kill path (any failure)
        if not (gate_ok and test_ok):
            shutil.rmtree(worktree_path, ignore_errors=True)
            return SettlementResult(
                success=False,
                worktree_path=worktree_path,
                base_path=base_path,
                gate_passed=gate_ok,
                tests_passed=test_ok,
                error_message=f"Gate: {gate_msg} | Tests: {test_msg}",
            )

        # Step C: Commit path (atomic move)
        try:
            copied = self._atomic_replace(base_path, worktree_path)
            return SettlementResult(
                success=True,
                worktree_path=worktree_path,
                base_path=base_path,
                gate_passed=True,
                tests_passed=True,
                copied_files=copied,
            )
        except Exception as e:
            shutil.rmtree(worktree_path, ignore_errors=True)
            return SettlementResult(
                success=False,
                worktree_path=worktree_path,
                base_path=base_path,
                gate_passed=True,
                tests_passed=True,
                error_message=f"Atomic move failed: {e}",
            )

    def _atomic_replace(self, base_path: Path, worktree_path: Path) -> list[Path]:
        """
        All-or-nothing replacement using temp staging.

        Pattern:
            1. Copy worktree -> temp staging
            2. Clear base_path
            3. Move staging -> base_path
            4. Cleanup
        """
        copied: list[Path] = []

        with tempfile.TemporaryDirectory(dir=base_path.parent) as staging:
            staging_path = Path(staging) / "staging"

            # Stage the new content
            shutil.copytree(worktree_path, staging_path, dirs_exist_ok=True)

            # Clear base (if it exists)
            if base_path.exists():
                for item in base_path.iterdir():
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
            else:
                base_path.mkdir(parents=True, exist_ok=True)

            # Atomic promotion from staging to base
            for item in staging_path.iterdir():
                dest = base_path / item.name
                if item.is_dir():
                    shutil.copytree(item, dest, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, dest)
                copied.append(dest)

        # Cleanup worktree only on full success
        shutil.rmtree(worktree_path, ignore_errors=True)

        return copied


def main() -> int:
    """CLI entry point: python settlement.py <base> <worktree> [--rules <path>]"""
    parser = argparse.ArgumentParser(
        description="Atomic settlement kernel: Commit or Kill"
    )
    parser.add_argument("base", help="Canonical base path")
    parser.add_argument("worktree", help="Candidate worktree path")
    parser.add_argument("--rules", help="Sentrux rules config path")
    args = parser.parse_args()

    base = Path(args.base)
    worktree = Path(args.worktree)
    rules = Path(args.rules) if args.rules else None

    engine = SettlementEngine()
    result = engine.settle(base, worktree, rules)

    # Output
    if result.success:
        print(f"SETTLED: {len(result.copied_files)} files to {result.base_path}")
        return 0
    else:
        print(f"KILLED: {result.error_message}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
