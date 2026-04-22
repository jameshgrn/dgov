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

import subprocess
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
    """Return git diff between the base commit and current HEAD."""
    result = subprocess.run(
        ["git", "diff", base_commit, "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    return result.stdout


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
        subprocess.run(
            ["dgov", "init", "--yes"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
        )

        # 2. Planner explores repo + emits plan → compile → run
        #    Full pipeline: settlement (autofix + lint + scope) + sentrux gate
        result = subprocess.run(
            ["dgov", "plan", "create", "--auto", "--run", "--apply-config", problem_statement],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            # Return empty patch on failure — HAL scores it as 0
            return {
                task_id: {
                    "history": [{"role": "assistant", "content": ""}],
                    "cost": 0.0,
                }
            }

        patch = _extract_patch(repo_dir, base_commit)

    return {
        task_id: {
            "history": [{"role": "assistant", "content": patch}],
            "cost": 0.0,  # TODO: extract from dgov event log when cost tracking ships
        }
    }
