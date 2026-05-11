"""dgov pipeline agent for HAL benchmarks.

Full canonical pipeline: init → plan create (planner explores + emits plan)
→ compile → run (settlement + sentrux). No bypassing.

Requires:
  - dgov installed (`pip install -e /path/to/dgov`)
  - FIREWORKS_API_KEY set
  - sentrux installed
  - git available
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path


def _clone_repo(repo: str, base_commit: str, work_dir: Path) -> Path:
    """Clone the target repo and check out the base commit."""
    repo_dir = work_dir / "repo"
    subprocess.run(
        ["git", "clone", "--quiet", f"https://github.com/{repo}.git", str(repo_dir)],
        check=True,
        capture_output=True,
        timeout=300,
    )
    subprocess.run(
        ["git", "checkout", "--quiet", base_commit],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "-b", "dgov-fix"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
    )
    return repo_dir


def _extract_patch(repo_dir: Path, base_commit: str) -> str:
    """Return git diff between the base commit and current HEAD, excluding dgov artifacts."""
    result = subprocess.run(
        [
            "git",
            "diff",
            base_commit,
            "HEAD",
            "--",
            ".",
            ":!.dgov/",
            ":!.sentrux/",
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _empty_response(task_id: str) -> dict:
    return {
        task_id: {
            "history": [{"role": "assistant", "content": ""}],
            "cost": 0.0,
        }
    }


def _patch_response(task_id: str, patch: str) -> dict:
    return {
        task_id: {
            "history": [{"role": "assistant", "content": patch}],
            "cost": 0.0,  # TODO: extract from dgov event log when cost tracking ships
        }
    }


def _log_result(label: str, result: subprocess.CompletedProcess[str], limit: int = 2000) -> None:
    print(f"[dgov] {label} returncode={result.returncode}", file=sys.stderr)
    if result.stdout:
        print(f"[dgov] stdout: {result.stdout[:limit]}", file=sys.stderr)
    if result.stderr:
        print(f"[dgov] stderr: {result.stderr[:limit]}", file=sys.stderr)


def _run_dgov_init(repo_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["dgov", "init", "--yes"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )


def _clear_test_cmd(repo_dir: Path) -> None:
    config_path = repo_dir / ".dgov" / "project.toml"
    config_text = config_path.read_text()
    config_text = re.sub(r'^test_cmd\s*=\s*".*"', 'test_cmd = ""', config_text, flags=re.MULTILINE)
    config_path.write_text(config_text)


def _run_dgov_plan(repo_dir: Path, problem_statement: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["dgov", "plan", "create", "--auto", "--run", "--apply-config", problem_statement],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=2400,
    )


def run(input: dict[str, dict], **kwargs) -> dict:
    """HAL agent entry point.

    Receives a single SWE-bench task, runs the full dgov pipeline
    (init → plan create → compile → run with settlement + sentrux),
    and returns the resulting patch.
    """
    assert len(input) == 1, "HAL sends one task at a time"
    task_id, task = next(iter(input.items()))

    problem_statement = task["problem_statement"]
    repo = task["repo"]
    base_commit = task.get("base_commit", "HEAD")

    with tempfile.TemporaryDirectory(prefix="dgov-hal-") as tmpdir:
        work_dir = Path(tmpdir)
        repo_dir = _clone_repo(repo, base_commit, work_dir)

        # 1. Bootstrap dgov (detects tooling, creates sentrux baseline)
        init_result = _run_dgov_init(repo_dir)
        _log_result("init", init_result, limit=1000)
        if init_result.returncode != 0:
            return _empty_response(task_id)

        # Clear test_cmd — SWE-bench evaluates tests in Docker; dgov's test
        # gate would fail on missing deps in the worktree.
        _clear_test_cmd(repo_dir)

        # .dgov/ and .sentrux/ are untracked — worktrees branch from HEAD
        # without needing these committed. Workers read config from
        # session_root (the main repo), not from the worktree.

        # 2. Planner explores repo + emits plan → compile → run
        #    Full pipeline: settlement (autofix + lint + scope) + sentrux gate
        result = _run_dgov_plan(repo_dir, problem_statement)
        _log_result("plan create", result)

        if result.returncode != 0:
            # Return empty patch on failure — HAL scores it as 0
            return _empty_response(task_id)

        patch = _extract_patch(repo_dir, base_commit)

    return _patch_response(task_id, patch)
