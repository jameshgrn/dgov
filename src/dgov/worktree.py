"""Git worktree operations for fast, isolated agent execution.

Follows Lacustrine Pillars:
- Pillar #2: The Atomic Attempt (isolated checkout)
- Pillar #3: Snapshot Isolation (independent git state)
- Pillar #10: Fail-Closed (cleanup on failure)
"""

from __future__ import annotations

import logging
import os
import shutil
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
    _link_shared_venv(root, wt_path)

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


def _link_shared_venv(project_root: Path, worktree_path: Path) -> None:
    """Reuse the repo venv in worktrees so uv/ty resolve third-party imports."""
    source = project_root / ".venv"
    target = worktree_path / ".venv"
    if not source.exists() or target.exists():
        return
    try:
        target.symlink_to(source, target_is_directory=True)
    except OSError as exc:
        logger.warning("Could not link shared .venv into %s: %s", worktree_path, exc)


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


def _worktrees_dir(project_root: str) -> Path:
    """Return the sibling worktrees directory for a project."""
    root = Path(project_root).resolve()
    return root.parent / f".dgov-worktrees-{root.name}"


def _list_git_worktrees(
    project_root: str, git_env: dict[str, str]
) -> tuple[set[str], set[str]] | None:
    """Parse `git worktree list --porcelain` once, return (paths, attached_branches).

    Returns None if the command fails (e.g. project_root is not a git repo).
    Callers should treat None as "nothing to prune".
    """
    res = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project_root,
        env=git_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    paths: set[str] = set()
    branches: set[str] = set()
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(line[len("worktree ") :].strip())
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branches.add(ref.removeprefix("refs/heads/"))
    return paths, branches


def prune_orphans(project_root: str, dry_run: bool = False) -> dict[str, int]:
    """Remove orphaned dgov worktrees and merged branches from prior sessions.

    Safe + idempotent. Only touches:
      - Directories under <project>/.dgov-worktrees-<name>/ not tracked by git
      - dgov/* branches with no live worktree AND fully merged into HEAD

    When `dry_run=True`, counts what would be removed without touching anything.

    Returns counts under keys "worktrees" and "branches".
    """
    git_env = _git_env(project_root)

    # Clear stale .git/worktrees/<name>/ metadata so the listing is accurate.
    # Skipped in dry-run so we don't mutate git state.
    if not dry_run:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=project_root,
            env=git_env,
            capture_output=True,
            check=False,
        )

    listing = _list_git_worktrees(project_root, git_env)
    if listing is None:
        # Not a git repo (or git failed to enumerate). Nothing to prune.
        return {"worktrees": 0, "branches": 0}
    live_paths, attached_branches = listing
    removed_dirs = _prune_orphan_dirs(project_root, git_env, live_paths, dry_run)
    removed_branches = _prune_orphan_branches(project_root, git_env, attached_branches, dry_run)

    if removed_dirs or removed_branches:
        logger.info(
            "%s %d orphan worktree(s) and %d merged dgov/* branch(es)",
            "Would prune" if dry_run else "Pruned",
            removed_dirs,
            removed_branches,
        )
    return {"worktrees": removed_dirs, "branches": removed_branches}


def _prune_orphan_dirs(
    project_root: str,
    git_env: dict[str, str],
    live_paths: set[str],
    dry_run: bool,
) -> int:
    """Remove worktree directories git no longer tracks."""
    worktrees_dir = _worktrees_dir(project_root)
    if not worktrees_dir.exists():
        return 0

    live = {Path(p).resolve() for p in live_paths}
    removed = 0
    for child in worktrees_dir.iterdir():
        if not child.is_dir() or child.resolve() in live:
            continue
        if dry_run:
            removed += 1
            continue
        # Try git first (handles metadata); fall back to rm if git is unaware.
        rc = subprocess.run(
            ["git", "worktree", "remove", "-f", str(child)],
            cwd=project_root,
            env=git_env,
            capture_output=True,
        )
        if rc.returncode != 0:
            shutil.rmtree(child, ignore_errors=True)
        removed += 1
    return removed


def _prune_orphan_branches(
    project_root: str,
    git_env: dict[str, str],
    attached: set[str],
    dry_run: bool,
) -> int:
    """Delete dgov/* branches with no live worktree AND fully merged into HEAD."""
    res = subprocess.run(
        ["git", "branch", "--list", "dgov/*", "--format=%(refname:short)"],
        cwd=project_root,
        env=git_env,
        capture_output=True,
        text=True,
        check=True,
    )
    candidates = [b.strip() for b in res.stdout.splitlines() if b.strip()]

    removed = 0
    for branch in candidates:
        if branch in attached:
            continue
        if dry_run:
            # Check --merged status without deleting, so dry-run matches reality.
            check = subprocess.run(
                ["git", "branch", "--merged", "HEAD", "--list", branch],
                cwd=project_root,
                env=git_env,
                capture_output=True,
                text=True,
            )
            if check.returncode == 0 and check.stdout.strip():
                removed += 1
            continue
        # -d (safe) — only deletes if merged into HEAD. Skips unmerged branches.
        rc = subprocess.run(
            ["git", "branch", "-d", branch],
            cwd=project_root,
            env=git_env,
            capture_output=True,
        )
        if rc.returncode == 0:
            removed += 1
    return removed


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
