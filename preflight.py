"""
Pre-Flight Subprocess Gate (Pillar #10: Fail-Closed).

Self-contained module with no external dependencies.
Can be imported even when planner/runner have circular import failures.
"""

import subprocess
import sys
from pathlib import Path


def check_import_integrity(worktree_path: Path) -> tuple[bool, str | None]:
    """
    Test if candidate code is importable in isolated subprocess.
    Returns (ok, error_message). If not ok, error_message contains stderr.
    """
    # Test 1: Can we import the modules?
    import_test = f"""
import sys
sys.path.insert(0, '{worktree_path}/src/kernel')
sys.path.insert(0, '{worktree_path}')

try:
    from planner import Plan, TaskNode, from_json
    from runner import PlanRunner
    from settlement import SettlementEngine, SettlementResult
    # Force actual usage to trigger deferred imports
    _ = Plan.__name__
    _ = TaskNode.__name__
    _ = PlanRunner.__name__
    _ = SettlementEngine.__name__
    print("PREFLIGHT_IMPORT_OK")
except Exception as e:
    print(f"PREFLIGHT_IMPORT_FAIL: {{e}}", file=sys.stderr)
    sys.exit(1)
"""

    result = subprocess.run(
        [sys.executable, "-c", import_test],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip() if result.stderr else "Unknown import failure"
        return False, f"IMPORT_FAIL: {stderr}"

    # Test 2: Can runner.py boot as __main__? (The real killer)
    boot_test = subprocess.run(
        [sys.executable, f"{worktree_path}/src/kernel/runner.py", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if boot_test.returncode != 0:
        stderr = (
            boot_test.stderr.strip() if boot_test.stderr else "Runner failed to boot"
        )
        return False, f"BOOT_FAIL: {stderr}"

    return True, None


def check_sentrux_availability() -> tuple[bool, str | None]:
    """Verify Sentrux sensor is not blinded."""
    result = subprocess.run(
        ["sentrux", "--help"],
        capture_output=True,
        text=True,
        timeout=10,
    )

    if result.returncode != 0:
        return False, "Sentrux not available or broken"

    return True, None


if __name__ == "__main__":
    # CLI usage for direct testing
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("-w", type=Path, default=Path("."), help="Worktree path")
    args = p.parse_args()

    sentrux_ok, sentrux_err = check_sentrux_availability()
    print(f"Sentrux: {'OK' if sentrux_ok else 'FAIL'} - {sentrux_err}")

    import_ok, import_err = check_import_integrity(args.w)
    print(
        f"Import: {'OK' if import_ok else 'FAIL'} - {import_err[:200] if import_err else 'None'}"
    )

    sys.exit(0 if (sentrux_ok and import_ok) else 1)
