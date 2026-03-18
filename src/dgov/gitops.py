"""Low-level git plumbing helpers for worktree and branch management."""

from __future__ import annotations

import subprocess


def _remove_worktree(project_root: str, worktree_path: str, branch_name: str) -> dict:
    result = subprocess.run(
        ["git", "-C", project_root, "worktree", "remove", "--force", worktree_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"success": False, "error": result.stderr.strip()}

    subprocess.run(["git", "-C", project_root, "branch", "-D", branch_name], capture_output=True)

    prune_result = subprocess.run(
        ["git", "-C", project_root, "worktree", "prune"],
        capture_output=True,
        text=True,
    )
    if prune_result.returncode != 0:
        return {"success": False, "error": prune_result.stderr.strip()}

    return {"success": True}
