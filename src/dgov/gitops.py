"""Low-level git plumbing helpers (no dgov imports)."""

from __future__ import annotations

import subprocess


def _remove_worktree(project_root: str, worktree_path: str, branch_name: str) -> None:
    subprocess.run(
        ["git", "-C", project_root, "worktree", "remove", "--force", worktree_path],
        capture_output=True,
    )
    subprocess.run(["git", "-C", project_root, "branch", "-D", branch_name], capture_output=True)
    subprocess.run(["git", "-C", project_root, "worktree", "prune"], capture_output=True)
