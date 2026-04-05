"""Git worktree operations for fast, isolated agent execution.

Follows Lacustrine Pillars:
- Pillar #2: The Atomic Attempt (isolated checkout)
- Pillar #3: Snapshot Isolation (independent git state)
- Pillar #10: Fail-Closed (cleanup on failure)
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from dgov.types import Worktree

logger = logging.getLogger(__name__)


def _git_env(cwd: str | Path | None = None) -> dict[str, str]:
    """Return clean git environment for subprocess calls.

    Removes GIT_DIR and GIT_WORK_TREE to prevent recursion issues.
    """
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    env["GIT_CONFIG_SYSTEM"] = "/dev/null"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if cwd:
        env["PWD"] = str(cwd)
    return env


def create_worktree(project_root: str, slug: str, base_ref: str = "HEAD") -> Worktree:
    """Create an isolated worktree for an Atomic Attempt.

    Worktrees are created as siblings of the project root (not inside it)
    to avoid git nesting issues where the worktree resolves to the parent
    repo's tree root instead of its own.
    """
    root = Path(project_root)
    worktrees_dir = root.parent / f".dgov-worktrees-{root.name}"
    worktrees_dir.mkdir(parents=True, exist_ok=True)

    wt_path = worktrees_dir / slug
    branch_name = f"dgov/{slug}"
    git_env = _git_env(project_root)

    # Idempotent cleanup of existing worktree/branch (skip prune — done once at end)
    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", "-f", str(wt_path)],
            cwd=project_root,
            env=git_env,
            capture_output=True,
        )
    subprocess.run(
        ["git", "branch", "-D", branch_name],
        cwd=project_root,
        env=git_env,
        capture_output=True,
    )

    # Pillar #3: Add worktree with a dedicated branch
    subprocess.run(
        ["git", "worktree", "add", "-b", branch_name, str(wt_path), base_ref],
        cwd=project_root,
        check=True,
        capture_output=True,
        env=git_env,
    )

    # Capture Snapshot SHA
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        env=git_env,
        check=True,
        capture_output=True,
        text=True,
    )
    commit = res.stdout.strip()

    logger.debug("Created isolated worktree at %s", wt_path)
    return Worktree(path=wt_path, branch=branch_name, commit=commit)


def merge_worktree(project_root: str, wt: Worktree) -> str:
    """Commit-or-Kill: Merge the worktree branch into base."""
    git_env = _git_env(project_root)

    # Try fast-forward first (hot-path)
    try:
        subprocess.run(
            ["git", "merge", "--ff-only", wt.branch],
            cwd=project_root,
            env=git_env,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        # Fallback: cherry-pick if main moved (Lacustrine safety)
        res = subprocess.run(
            ["git", "rev-list", "--reverse", f"{wt.commit}..{wt.branch}"],
            cwd=project_root,
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        )
        commits = [c for c in res.stdout.strip().split("\n") if c]
        for c in commits:
            try:
                subprocess.run(
                    ["git", "cherry-pick", c],
                    cwd=project_root,
                    env=git_env,
                    check=True,
                    capture_output=True,
                )
            except subprocess.CalledProcessError:
                # Abort cherry-pick to leave repo in clean state, then re-raise
                subprocess.run(
                    ["git", "cherry-pick", "--abort"],
                    cwd=project_root,
                    env=git_env,
                    capture_output=True,
                )
                raise

    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        env=git_env,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def remove_worktree(project_root: str, wt: Worktree) -> None:
    """Annihilate the sandbox (Pillar #10). Prune deferred to caller."""
    git_env = _git_env(project_root)
    subprocess.run(
        ["git", "worktree", "remove", "-f", str(wt.path)],
        cwd=project_root,
        env=git_env,
        capture_output=True,
    )
    subprocess.run(
        ["git", "branch", "-D", wt.branch],
        cwd=project_root,
        env=git_env,
        capture_output=True,
    )


def commit_in_worktree(wt: Worktree, message: str, file_claims: tuple[str, ...] = ()) -> str:
    """Stage claimed files + commit. Falls back to git add . if no claims."""
    env = _git_env(wt.path)
    env["GIT_AUTHOR_NAME"] = "dgov-worker"
    env["GIT_AUTHOR_EMAIL"] = "agent@dgov.local"
    env["GIT_COMMITTER_NAME"] = "dgov-worker"
    env["GIT_COMMITTER_EMAIL"] = "agent@dgov.local"

    if file_claims:
        # Stage only claimed files (avoids pre-existing lint failures)
        existing = [f for f in file_claims if (wt.path / f).exists()]
        if existing:
            subprocess.run(["git", "add", *existing], cwd=wt.path, env=env, check=True)
    else:
        subprocess.run(["git", "add", "."], cwd=wt.path, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=wt.path,
        env=env,
        check=True,
        capture_output=True,
    )
    sha_res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt.path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return sha_res.stdout.strip()
