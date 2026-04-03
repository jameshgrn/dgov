"""Bailiff: Prepare worktree, capture Sentrux baseline, invoke worker."""

from __future__ import annotations
import json
import shutil
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def _load_learned_rules(base: Path) -> str:
    """Load and format learned rules for prompt injection."""
    rules_path = base / ".dgov" / "rules" / "learned.json"
    if not rules_path.exists():
        return ""
    try:
        with open(rules_path) as f:
            data = json.load(f)
            rules = data.get("rules", [])
            if not rules:
                return ""
            lines = ["\n[LEARNED RULES - From previous failures:]"]
            for i, r in enumerate(rules[:5], 1):
                scope = f" ({r.get('target_file')})" if r.get("target_file") else ""
                lines.append(f"{i}. [{r['pattern_type']}]{scope}: {r['instruction']}")
            return "\n".join(lines)
    except Exception:
        return ""


class AttemptState(Enum):
    SNAPSHOTTED = auto()
    DISPATCHED = auto()
    RUNNING = auto()
    PRODUCED = auto()
    TIMEOUT = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ProducedBundle:
    worktree_path: Path
    modified_files: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class DispatchReport:
    attempt_id: str
    state: AttemptState
    success: bool
    worktree_path: Path | None = None
    bundle: ProducedBundle | None = None
    exit_code: int | None = None
    error: str | None = None


class WorktreeManager:
    """Prepare isolated git worktrees in .dgov/worktrees/."""

    def __init__(self, base_path: Path) -> None:
        self.base = Path(base_path).resolve()
        self.root = self.base / ".dgov" / "worktrees"

    def create(self, attempt_id: str, base_ref: str = "main") -> Path:
        path = self.root / f"attempt-{attempt_id}"
        branch_name = f"attempt/{attempt_id}"

        # Clean up any existing worktree/branch for this attempt
        if path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "-f", str(path)],
                cwd=self.base,
                capture_output=True,
            )
            shutil.rmtree(path, ignore_errors=True)

        # Prune stale worktree references and delete existing branch
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=self.base,
            capture_output=True,
        )
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=self.base,
            capture_output=True,  # Ignore error if branch doesn't exist
        )

        path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch_name,
                str(path),
                base_ref,
            ],
            cwd=self.base,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"worktree add failed: {result.stderr}")
        return path

    def has_changes(self, worktree_path: Path) -> bool:
        """Verify worktree has actual modifications (sentinel check)."""
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        return bool(r.stdout.strip())


class PiWorker:
    """Headless pi agent coder with pipe-draining watchdog."""

    def baseline(self, worktree_path: Path) -> tuple[int, str]:
        """Take Sentrux baseline. Returns (score, path_to_baseline)."""
        r = subprocess.run(
            ["sentrux", "gate", "--save", str(worktree_path)],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        return r.returncode, r.stderr or r.stdout

    def execute_watchdog(
        self, prompt: str, worktree_path: Path, timeout: int = 300
    ) -> tuple[int, str, str, bool]:
        """
        Execute pi agent with watchdog timer and pipe draining.

        Returns: (exit_code, stdout, stderr, timed_out)
        """
        # Popen with unbuffered pipes to prevent deadlock
        process = subprocess.Popen(
            ["pi", "agent", "coder", prompt],
            cwd=worktree_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=0,  # Unbuffered
        )

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        timed_out = threading.Event()

        def drain_pipe(pipe, chunks):
            """Thread worker to continuously drain pipe output."""
            try:
                for line in iter(pipe.readline, ""):
                    chunks.append(line)
                pipe.close()
            except Exception:
                pass

        # Start pipe-draining threads
        stdout_thread = threading.Thread(
            target=drain_pipe, args=(process.stdout, stdout_chunks)
        )
        stderr_thread = threading.Thread(
            target=drain_pipe, args=(process.stderr, stderr_chunks)
        )
        stdout_thread.daemon = True
        stderr_thread.daemon = True
        stdout_thread.start()
        stderr_thread.start()

        # Wait with watchdog timeout
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out.set()
            process.kill()
            process.wait()

        # Join threads to ensure all output captured
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)

        return process.returncode, stdout, stderr, timed_out.is_set()

    def collect(self, worktree_path: Path) -> list[Path]:
        r = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        return [
            worktree_path / line[3:].split(" -> ")[-1].strip()
            for line in r.stdout.strip().split("\n")
            if line and len(line) >= 3
        ]


def dispatch(
    base_path: Path, prompt: str, attempt_id: str | None = None
) -> DispatchReport:
    """
    Full dispatch: SNAPSHOTTED -> DISPATCHED -> RUNNING -> PRODUCED.

    Hardened against:
    - Pipe buffer deadlock (threaded draining)
    - Infinite hang (300s watchdog timeout)
    - Silent failures (sentinel change verification)
    """
    attempt_id = attempt_id or uuid.uuid4().hex[:12]
    worktrees, worker = WorktreeManager(base_path), PiWorker()

    try:
        # Phase 1: Create worktree
        worktree = worktrees.create(attempt_id)

        # Phase 2: Sentrux baseline
        code, err = worker.baseline(worktree)
        if code != 0:
            return DispatchReport(
                attempt_id,
                AttemptState.DISPATCHED,
                False,
                worktree,
                error=f"baseline: {err}",
            )

        # Phase 3: Execute worker with learned rules injection
        learned = _load_learned_rules(base_path)
        enhanced_prompt = f"{prompt}{learned}"

        exit_code, stdout, stderr, timed_out = worker.execute_watchdog(
            enhanced_prompt, worktree, timeout=300
        )

        # Phase 4: Handle timeout
        if timed_out:
            # Worker exceeded timeout but may have made changes
            # Sentinel check: verify if worktree has modifications
            if worktrees.has_changes(worktree):
                # Partial success - changes exist but worker didn't finish
                bundle = ProducedBundle(worktree, worker.collect(worktree))
                return DispatchReport(
                    attempt_id,
                    AttemptState.TIMEOUT,
                    False,  # Not fully successful
                    worktree,
                    bundle,
                    exit_code,
                    error=f"Worker timed out after 300s but produced changes. stderr: {stderr[:500]}",
                )
            else:
                # True timeout - no output
                return DispatchReport(
                    attempt_id,
                    AttemptState.TIMEOUT,
                    False,
                    worktree,
                    exit_code=exit_code,
                    error=f"Worker timed out after 300s with no changes. stderr: {stderr[:500]}",
                )

        # Phase 5: Handle explicit failure
        if exit_code != 0:
            return DispatchReport(
                attempt_id,
                AttemptState.FAILED,
                False,
                worktree,
                exit_code=exit_code,
                error=f"Worker failed (exit {exit_code}). stderr: {stderr[:500]}",
            )

        # Phase 6: Sentinel check - verify changes exist
        if not worktrees.has_changes(worktree):
            return DispatchReport(
                attempt_id,
                AttemptState.PRODUCED,
                False,  # No changes is a failure
                worktree,
                exit_code=exit_code,
                error="Worker completed but produced no changes (sentinel check failed)",
            )

        # Phase 7: Success - collect bundle
        bundle = ProducedBundle(worktree, worker.collect(worktree))
        return DispatchReport(
            attempt_id,
            AttemptState.PRODUCED,
            True,
            worktree,
            bundle,
            exit_code,
        )

    except Exception as e:
        return DispatchReport(
            attempt_id,
            AttemptState.DISPATCHED,
            False,
            error=str(e),
        )
