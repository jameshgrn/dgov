"""PlanRunner: Execute plan.json DAG through DispatchAdapter with Settlement v1.0.

Pillar #10: Fail-Closed - Pre-flight checks run before dangerous imports.
"""

from __future__ import annotations
import argparse
import json
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any

# Pre-Flight: Only safe, self-contained imports at module level
# Dangerous imports (planner, runner, settlement) happen inside functions
# after preflight checks pass


MAX_RETRIES = 2


def _preflight_self_test(worktree_path: Path) -> tuple[bool, str | None]:
    """
    Self-contained preflight test. No external imports.
    Tests if core modules are importable in isolated subprocess.
    Specifically tests the cycle: runner -> planner -> runner
    """
    # Test: Can runner.py boot as __main__ with the actual argument parsing?
    # This simulates the real entry point and catches import-time cycles
    result = subprocess.run(
        [sys.executable, f"{worktree_path}/src/kernel/runner.py", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        stderr = (
            result.stderr.strip()
            if result.stderr
            else "Runner failed to boot as __main__"
        )
        return False, f"BOOT_FAIL: {stderr}"

    if "usage:" not in result.stdout:
        return False, "BOOT_FAIL: Unexpected output from --help"

    return True, None


def _sentrux_check() -> tuple[bool, str | None]:
    """Verify Sentrux sensor is available."""
    result = subprocess.run(
        ["sentrux", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return False, "Sentrux not available"
    return True, None


class NodeState(Enum):
    PENDING = auto()
    READY = auto()
    DISPATCHED = auto()
    SETTLED = auto()
    FAILED = auto()


@dataclass
class PlanRunner:
    plan_path: Path
    base_path: Path
    attempt_id: str
    state_path: Path = field(init=False)
    conn: Any = field(default=None, init=False)
    # Lazy imports - loaded after preflight passes
    _Plan: Any = field(default=None, init=False, repr=False)
    _TaskNode: Any = field(default=None, init=False, repr=False)
    _from_json: Any = field(default=None, init=False, repr=False)
    _dispatch: Any = field(default=None, init=False, repr=False)
    _SettlementEngine: Any = field(default=None, init=False, repr=False)
    _SettlementResult: Any = field(default=None, init=False, repr=False)
    _reactor: Any = field(default=None, init=False, repr=False)

    def _load_modules(self):
        """Lazy import dangerous modules after preflight passes."""
        if self._Plan is not None:
            return
        try:
            from planner import Plan, TaskNode, from_json
            from dispatch import dispatch
            from settlement import SettlementEngine, SettlementResult
            import reactor

            self._Plan = Plan
            self._TaskNode = TaskNode
            self._from_json = from_json
            self._dispatch = dispatch
            self._SettlementEngine = SettlementEngine
            self._SettlementResult = SettlementResult
            self._reactor = reactor
        except ImportError:
            from .planner import Plan, TaskNode, from_json
            from .dispatch import dispatch
            from .settlement import SettlementEngine, SettlementResult
            from . import reactor

            self._Plan = Plan
            self._TaskNode = TaskNode
            self._from_json = from_json
            self._dispatch = dispatch
            self._SettlementEngine = SettlementEngine
            self._SettlementResult = SettlementResult
            self._reactor = reactor

    def __post_init__(self):
        self.state_path = (
            self.base_path / ".dgov" / f"runner_state_{self.attempt_id}.db"
        )
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.state_path)
        self._init_db()

    def _init_db(self):
        ddl = """
        CREATE TABLE IF NOT EXISTS nodes (
            task_id TEXT PRIMARY KEY,
            state TEXT,
            attempt_count INTEGER DEFAULT 0,
            error TEXT,
            worktree TEXT
        );
        CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            event TEXT,
            payload TEXT
        );
        """
        self.conn.executescript(ddl)
        self.conn.commit()

    def _log_to_ledger(self, event: dict):
        self.conn.execute(
            "INSERT INTO ledger (ts, event, payload) VALUES (?, ?, ?)",
            (
                datetime.utcnow().isoformat(),
                event.get("type", "Unknown"),
                json.dumps(event),
            ),
        )
        self.conn.commit()

    def _load_plan(self) -> Any:
        return self._from_json(str(self.plan_path))

    def run(self) -> list[Any]:
        """Execute plan with pre-flight gate."""
        # Pillar #10: Pre-flight gate before any dangerous work
        print("[preflight] Running import integrity check...")
        import_ok, import_err = _preflight_self_test(self.base_path)
        if not import_ok:
            print(f"[preflight] IMPORT TIME TOXICITY DETECTED: {import_err[:200]}")
            self._log_to_ledger(
                {
                    "type": "PreFlightFailed",
                    "attempt_id": self.attempt_id,
                    "reason": "import_time_toxicity",
                    "error": import_err,
                }
            )
            sys.exit(1)
        print("[preflight] Import check passed")

        print("[preflight] Checking Sentrux availability...")
        sentrux_ok, sentrux_err = _sentrux_check()
        if not sentrux_ok:
            print(f"[preflight] SENTRUX BLINDED: {sentrux_err}")
            self._log_to_ledger(
                {
                    "type": "PreFlightFailed",
                    "attempt_id": self.attempt_id,
                    "reason": "sentrux_unavailable",
                    "error": sentrux_err,
                }
            )
            sys.exit(1)
        print("[preflight] Sentrux check passed")

        # Now safe to load dangerous modules
        print("[runner] Loading kernel modules...")
        self._load_modules()

        plan = self._load_plan()
        all_ids = {t.id for t in plan.tasks}

        # Initialize nodes
        for t in plan.tasks:
            self.conn.execute(
                "INSERT OR IGNORE INTO nodes (task_id, state) VALUES (?, ?)",
                (t.id, NodeState.PENDING.name),
            )
        self.conn.commit()

        results: list[Any] = []
        done: set[str] = set()
        failed: set[str] = set()

        while done | failed != all_ids:
            ready_ids = self._ready_nodes(plan)
            if not ready_ids:
                if failed:
                    break
                raise RuntimeError("Deadlock: no ready nodes and no failures")

            for tid in ready_ids:
                self._process_node(tid, plan, done, failed, results)

        if failed:
            raise RuntimeError(f"Tasks failed: {failed}")

        return results

    def _ready_nodes(self, plan: Any) -> list[str]:
        cursor = self.conn.execute(
            "SELECT task_id FROM nodes WHERE state = ?", (NodeState.PENDING.name,)
        )
        pending = {row[0] for row in cursor.fetchall()}

        ready = []
        for t in plan.tasks:
            if t.id in pending:
                deps_satisfied = all(
                    d in {NodeState.SETTLED.name}
                    for d in self._get_dep_states(t.depends_on)
                )
                if deps_satisfied:
                    ready.append(t.id)
        return ready

    def _get_dep_states(self, deps: tuple) -> list[str]:
        if not deps:
            return []
        cursor = self.conn.execute(
            f"SELECT state FROM nodes WHERE task_id IN ({','.join('?' * len(deps))})",
            deps,
        )
        return [row[0] for row in cursor.fetchall()]

    def _process_node(self, tid: str, plan: Any, done: set, failed: set, results: list):
        task = next(t for t in plan.tasks if t.id == tid)
        node = type(
            "Node", (), {"task": task, "state": NodeState.PENDING, "error": None}
        )()

        # Update to READY
        self.conn.execute(
            "UPDATE nodes SET state = ? WHERE task_id = ?",
            (NodeState.READY.name, tid),
        )
        self.conn.commit()

        self._log_to_ledger(
            {
                "type": "DispatchStart",
                "task_id": tid,
                "attempt_id": self.attempt_id,
            }
        )

        # Dispatch
        try:
            # Fix interface: dispatch takes (base_path, prompt, attempt_id)
            # task.goal is the prompt, expected_artifacts checked in settlement
            report = self._dispatch(
                self.base_path,
                task.goal,
                self.attempt_id,
            )
        except Exception as e:
            node.state = NodeState.FAILED
            node.error = f"Dispatch exception: {e}"
            self._handle_failure(node, results)
            failed.add(tid)
            return

        if not report.success or not report.worktree_path:
            node.state = NodeState.FAILED
            node.error = report.error or f"Dispatch failed: {report.state.name}"
            self._handle_failure(node, results)
            failed.add(tid)
            return

        # Pre-flight for this specific worktree (redundant but safe)
        import_ok, import_err = _preflight_self_test(report.worktree_path)
        if not import_ok:
            self._log_to_ledger(
                {
                    "type": "PreFlightFailed",
                    "task_id": tid,
                    "attempt_id": report.attempt_id,
                    "reason": "import_time_toxicity_in_worktree",
                    "error": import_err,
                }
            )
            node.error = f"Pre-flight failed: {import_err[:200]}"
            synthetic = self._SettlementResult(
                success=False,
                worktree_path=report.worktree_path,
                copied_files=[],
                error_message=node.error,
                gate_passed=False,
                tests_passed=False,
            )
            results.append(synthetic)
            self._handle_failure(node, results, synthetic)
            failed.add(tid)
            return

        # Settlement
        settler = self._SettlementEngine(
            base_path=self.base_path,
            worktree_path=report.worktree_path,
            expected_files=task.expected_artifacts,
        )
        result = settler.settle()
        results.append(result)

        if result.success:
            self.conn.execute(
                "UPDATE nodes SET state = ?, worktree = ? WHERE task_id = ?",
                (NodeState.SETTLED.name, str(report.worktree_path), tid),
            )
            self.conn.commit()
            done.add(tid)
            self._log_to_ledger(
                {
                    "type": "NodeSettled",
                    "task_id": tid,
                    "attempt_id": report.attempt_id,
                    "worktree": str(report.worktree_path),
                }
            )
        else:
            node.error = result.error_message or "Settlement failed"
            self._handle_failure(node, results, result)
            failed.add(tid)

    def _handle_failure(self, node, results, result=None):
        current = self.conn.execute(
            "SELECT attempt_count FROM nodes WHERE task_id = ?",
            (node.task.id,),
        ).fetchone()
        count = (current[0] if current else 0) + 1

        self.conn.execute(
            "UPDATE nodes SET attempt_count = ?, error = ?, state = ? WHERE task_id = ?",
            (count, node.error, NodeState.FAILED.name, node.task.id),
        )
        self.conn.commit()

        self._log_to_ledger(
            {
                "type": "NodeFailed",
                "task_id": node.task.id,
                "attempt_count": count,
                "error": node.error,
            }
        )

        if count < MAX_RETRIES:
            print(f"[retry] {node.task.id} (attempt {count}/{MAX_RETRIES})")
            self.conn.execute(
                "UPDATE nodes SET state = ? WHERE task_id = ?",
                (NodeState.PENDING.name, node.task.id),
            )
            self.conn.commit()
        else:
            print(f"[escalate] {node.task.id} exhausted retries")
            self._log_to_ledger(
                {
                    "type": "NodeEscalated",
                    "task_id": node.task.id,
                    "error": node.error,
                }
            )

    def audit(self):
        """Audit completed run - query ledger for failures and triage."""
        self._load_modules()  # Ensure modules loaded

        # Query ledger for failed/escalated nodes
        cursor = self.conn.execute(
            "SELECT task_id, error FROM nodes WHERE state = ?", (NodeState.FAILED.name,)
        )
        failures = cursor.fetchall()

        repair_prompts = []
        for task_id, error in failures:
            failure = self._reactor.Failure(
                error=error or "Unknown failure",
                context=f"task_id={task_id}",
                attempt_id=self.attempt_id,
            )
            repair = self._reactor.formulate_repair(failure, retry_count=0)
            repair_prompts.append(repair)
            print(f"[audit] {task_id}: {repair['template_type']}")

        return repair_prompts


def main():
    p = argparse.ArgumentParser(description="PlanRunner with Pre-Flight Gate")
    p.add_argument("-p", "--plan", required=True, type=Path, help="Path to plan.json")
    p.add_argument(
        "-b", "--base", required=True, type=Path, help="Base repository path"
    )
    p.add_argument("-a", "--attempt", default="run-001", help="Attempt ID")
    args = p.parse_args()

    runner = PlanRunner(
        plan_path=args.plan,
        base_path=args.base,
        attempt_id=args.attempt,
    )

    try:
        results = runner.run()
        print(f"[success] All {len(results)} tasks completed")
    except RuntimeError as e:
        print(f"[failure] {e}")
        runner.audit()
        sys.exit(1)


if __name__ == "__main__":
    main()
