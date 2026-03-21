"""Git merge, conflict resolution, and post-merge operations."""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from dgov.inspection import MergeResult
from dgov.persistence import PROTECTED_FILES, IllegalTransitionError

logger = logging.getLogger(__name__)


def _count_branch_commits(project_root: str, branch_name: str) -> int:
    """Count commits on *branch_name* relative to its merge-base with HEAD."""
    base_r = subprocess.run(
        ["git", "-C", project_root, "merge-base", "HEAD", branch_name],
        capture_output=True,
        text=True,
    )
    if base_r.returncode != 0:
        return 0
    count_r = subprocess.run(
        [
            "git",
            "-C",
            project_root,
            "rev-list",
            "--count",
            f"{base_r.stdout.strip()}..{branch_name}",
        ],
        capture_output=True,
        text=True,
    )
    if count_r.returncode != 0:
        return 0
    try:
        return int(count_r.stdout.strip())
    except ValueError:
        return 0


@contextmanager
def _stash_guard(project_root: str, label: str = "merge"):
    """Stash dirty tracked files, yield, then pop. Warns on pop failure."""
    status = subprocess.run(
        ["git", "-C", project_root, "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    dirty = any(not ln.startswith("??") for ln in status.stdout.strip().splitlines() if ln)
    stashed = False
    if dirty:
        stash = subprocess.run(
            ["git", "-C", project_root, "stash", "push", "-m", f"dgov-{label}-auto"],
            capture_output=True,
            text=True,
        )
        stashed = stash.returncode == 0

    warnings: list[str] = []
    try:
        yield stashed, warnings
    finally:
        if stashed:
            pop = subprocess.run(
                ["git", "-C", project_root, "stash", "pop"],
                capture_output=True,
                text=True,
            )
            if pop.returncode != 0:
                logger.warning(
                    "Merge succeeded but stash pop failed — uncommitted changes "
                    "are preserved in stash. Recover with: git stash show && git stash pop"
                )
                warnings.append(
                    "Stash pop failed after merge. Your uncommitted changes are safe in "
                    "the stash. Recover with: git stash show && git stash pop"
                )


class _MergeLock:
    """File-based lock to serialize concurrent merges on the same repo."""

    def __init__(self, project_root: str) -> None:
        lock_dir = Path(project_root) / ".dgov"
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._path = lock_dir / "merge.lock"
        self._fd: int | None = None

    def __enter__(self) -> "_MergeLock":
        self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


@contextmanager
def _candidate_worktree(project_root: str, slug: str):
    """Create a temporary worktree rooted at current HEAD for merge validation."""
    root = Path(project_root) / ".dgov" / "merge-validation"
    root.mkdir(parents=True, exist_ok=True)
    suffix = f"{int(time.time() * 1_000_000)}"
    branch_name = f"dgov-validate-{slug[:20]}-{suffix}"
    worktree_path = root / branch_name

    add = subprocess.run(
        [
            "git",
            "-C",
            project_root,
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            "HEAD",
        ],
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        raise RuntimeError(f"Failed to create validation worktree: {add.stderr.strip()}")

    try:
        yield str(worktree_path), branch_name
    finally:
        subprocess.run(
            ["git", "-C", project_root, "worktree", "remove", "--force", str(worktree_path)],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", project_root, "branch", "-D", branch_name],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", project_root, "worktree", "prune"],
            capture_output=True,
            text=True,
        )


def _advance_current_branch_to_commit(project_root: str, commit_sha: str) -> MergeResult:
    """Advance the current branch to a validated commit while preserving dirty work."""
    current_branch = subprocess.run(
        ["git", "-C", project_root, "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    if current_branch.returncode != 0:
        return MergeResult(success=False, stderr="Detached HEAD — cannot advance ref")

    with _stash_guard(project_root, "validated-merge") as (_stashed, warnings):
        branch_ref = f"refs/heads/{current_branch.stdout.strip()}"
        update = subprocess.run(
            ["git", "-C", project_root, "update-ref", branch_ref, commit_sha],
            capture_output=True,
            text=True,
        )
        if update.returncode != 0:
            return MergeResult(success=False, stderr=update.stderr.strip())

        reset = subprocess.run(
            ["git", "-C", project_root, "reset", "--hard", "HEAD"],
            capture_output=True,
            text=True,
        )
        if reset.returncode != 0:
            return MergeResult(
                success=False,
                stderr=(
                    f"reset --hard failed after update-ref advanced {branch_ref} to {commit_sha}. "
                    "Working tree is out of sync. Run: git reset --hard HEAD"
                ),
                warnings=warnings,
            )

        return MergeResult(success=True, warnings=warnings)


# -- Plumbing merge --


def _plumbing_merge(
    project_root: str,
    branch_name: str,
    message: str | None = None,
    squash: bool = True,
) -> MergeResult:
    """Merge branch into HEAD using git plumbing (zero side effects on failure).

    When squash=True (default): uses git merge-tree for in-memory merge
    computation, creating a single squash-style merge commit.

    When squash=False: uses ``git merge --no-ff`` to create a merge commit
    that preserves the worker's individual commit history.
    """
    with _MergeLock(project_root):
        if not squash:
            return _no_squash_merge(project_root, branch_name, message)

        head = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        if head.returncode != 0:
            return MergeResult(success=False, stderr=head.stderr.strip())

        head_sha = head.stdout.strip()

        # In-memory merge — no working tree side effects
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", head_sha, branch_name],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return MergeResult(success=False, stdout=result.stdout, stderr=result.stderr)

        tree_hash = result.stdout.strip().splitlines()[0]
        branch_tip = subprocess.run(
            ["git", "-C", project_root, "rev-parse", branch_name],
            capture_output=True,
            text=True,
        )
        if branch_tip.returncode != 0:
            return MergeResult(success=False, stderr=f"Cannot resolve {branch_name}")

        # Create squash commit (single parent — linear history)
        msg = message or f"Merge {branch_name}"
        commit = subprocess.run(
            [
                "git",
                "-C",
                project_root,
                "commit-tree",
                tree_hash,
                "-p",
                head_sha,
                "-m",
                msg,
            ],
            capture_output=True,
            text=True,
        )
        if commit.returncode != 0:
            return MergeResult(success=False, stderr=commit.stderr.strip())

        new_commit = commit.stdout.strip()

        # Advance current branch ref
        current_branch = subprocess.run(
            ["git", "-C", project_root, "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
        )
        if current_branch.returncode != 0:
            return MergeResult(success=False, stderr="Detached HEAD — cannot advance ref")

        with _stash_guard(project_root, "plumbing") as (stashed, warnings):
            branch_ref = f"refs/heads/{current_branch.stdout.strip()}"
            update = subprocess.run(
                ["git", "-C", project_root, "update-ref", branch_ref, new_commit],
                capture_output=True,
                text=True,
            )
            if update.returncode != 0:
                return MergeResult(success=False, stderr=update.stderr.strip())

            # Reset working tree to match new commit
            reset = subprocess.run(
                ["git", "-C", project_root, "reset", "--hard", "HEAD"],
                capture_output=True,
                text=True,
            )
            if reset.returncode != 0:
                logger.error(
                    "reset --hard failed after update-ref advanced %s to %s. "
                    "Working tree is out of sync. Run: git reset --hard HEAD",
                    branch_ref,
                    new_commit,
                )
                return MergeResult(
                    success=False,
                    stderr=(
                        f"reset --hard failed after update-ref advanced "
                        f"{branch_ref} to {new_commit}. "
                        f"Working tree is out of sync. Run: git reset --hard HEAD"
                    ),
                )

            return MergeResult(success=True, warnings=warnings)


def _no_squash_merge(
    project_root: str, branch_name: str, message: str | None = None
) -> MergeResult:
    """Merge branch with --no-ff to preserve worker commit history."""
    commit_count = _count_branch_commits(project_root, branch_name)

    # Extract slug from branch name (dgov-<slug>)
    slug = branch_name.removeprefix("dgov-") if branch_name.startswith("dgov-") else branch_name
    msg = message or f"Merge {slug} ({commit_count} commit{'s' if commit_count != 1 else ''})"

    with _stash_guard(project_root, "no-squash") as (stashed, warnings):
        result = subprocess.run(
            ["git", "-C", project_root, "merge", "--no-ff", "-m", msg, branch_name],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return MergeResult(success=False, stderr=result.stderr.strip())

        return MergeResult(success=True, warnings=warnings)


def _stash_and_rebase(
    project_root: str, label: str, onto_ref: str, branch_to_rebase: str
) -> tuple[MergeResult, str]:
    """Shared helper: stash dirty files, run rebase, restore branch on error.

    Returns (MergeResult, current_branch) where current_branch is the branch
    name we were on before the rebase (for restoration purposes).

    The caller must use _stash_guard to manage stashing; this handles the
    rebase execution and error recovery.

    Special handling: when git refuses to rebase an attached worktree branch,
    we treat it as success since we can't rebase an attached branch anyway.
    """
    # Remember current branch to restore after rebase
    current = subprocess.run(
        ["git", "-C", project_root, "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
    )
    if current.returncode != 0:
        return (
            MergeResult(success=False, stderr="Detached HEAD — cannot rebase"),
            "",
        )

    current_branch = current.stdout.strip()

    # Rebase the branch onto the target ref
    rebase = subprocess.run(
        ["git", "-C", project_root, "rebase", onto_ref, branch_to_rebase],
        capture_output=True,
        text=True,
    )
    if rebase.returncode != 0:
        # Special case: git refuses to rebase an attached worktree branch
        # Error message pattern: 'fatal: \<branch\> is already used by worktree at \<path\>'
        stderr_lower = rebase.stderr.lower()
        if "already used by worktree" in stderr_lower or "attached" in stderr_lower:
            # This branch is attached to a worktree — we can't rebase it anyway,
            # but this isn't a real failure. The plumbing merge will handle it.
            logger.info(
                "Skipping rebase for %s (attached to worktree)",
                branch_to_rebase,
            )
            return (MergeResult(success=True), current_branch)

        subprocess.run(
            ["git", "-C", project_root, "rebase", "--abort"],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", project_root, "checkout", current_branch],
            capture_output=True,
        )
        return (
            MergeResult(
                success=False,
                stderr=f"Rebase failed: {rebase.stderr.strip()}",
            ),
            current_branch,
        )

    # Switch back to the original branch
    subprocess.run(
        ["git", "-C", project_root, "checkout", current_branch],
        capture_output=True,
    )

    logger.info("Auto-rebased %s onto %s", branch_to_rebase, onto_ref)
    return (MergeResult(success=True), current_branch)


def _rebase_onto_head(project_root: str, branch_name: str) -> MergeResult:
    """Rebase branch onto current HEAD so it's up-to-date before merge.

    On success returns MergeResult(success=True).
    On conflict aborts the rebase and returns MergeResult(success=False)
    with conflict details in stderr/warnings.
    """
    # Check if rebase is needed (branch already up-to-date with HEAD)
    base_r = subprocess.run(
        ["git", "-C", project_root, "merge-base", "HEAD", branch_name],
        capture_output=True,
        text=True,
    )
    if base_r.returncode != 0:
        return MergeResult(success=False, stderr=f"Cannot find merge-base for {branch_name}")

    head_sha = subprocess.run(
        ["git", "-C", project_root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    if base_r.stdout.strip() == head_sha:
        # Already based on HEAD — no rebase needed
        return MergeResult(success=True)

    result, _ = _stash_and_rebase(project_root, "onto-head", "HEAD", branch_name)
    return result


def _rebase_merge(project_root: str, branch_name: str, message: str | None = None) -> MergeResult:
    """Rebase branch onto HEAD then fast-forward for linear history with original commits."""
    with _MergeLock(project_root):
        commit_count = _count_branch_commits(project_root, branch_name)

        with _stash_guard(project_root, "rebase") as (stashed, warnings):
            result, current_branch = _stash_and_rebase(
                project_root, "rebase-merge", "HEAD", branch_name
            )
            if not result.success:
                return result

            # Now on branch_name (rebased). Switch back and fast-forward.
            subprocess.run(
                ["git", "-C", project_root, "checkout", current_branch],
                capture_output=True,
                text=True,
            )
            ff = subprocess.run(
                ["git", "-C", project_root, "merge", "--ff-only", branch_name],
                capture_output=True,
                text=True,
            )
            if ff.returncode != 0:
                return MergeResult(
                    success=False, stderr=f"Fast-forward failed: {ff.stderr.strip()}"
                )

            logger.info(
                "Rebase-merged %s (%d commits) onto %s",
                branch_name,
                commit_count,
                current_branch,
            )
            return MergeResult(success=True, warnings=warnings)


# -- Post-merge lint fix --


def _lint_fix_merged_files(project_root: str, changed_files: list[str]) -> dict:
    """Run ruff check --fix + ruff format on changed .py files after merge.

    Returns {"fixed": [...], "unfixable": [...]} or empty dict if nothing to do.
    """
    py_files = [f for f in changed_files if f.endswith(".py")]
    if not py_files:
        return {}

    abs_files = [
        str(Path(project_root) / f) for f in py_files if (Path(project_root) / f).exists()
    ]
    if not abs_files:
        return {}

    fixed = []
    unfixable = []
    result = {}

    # Isolate post-merge linting from unrelated dirty files on main.
    # Otherwise an amend can accidentally absorb the user's restored worktree edits.
    with _stash_guard(project_root, "lint-fix") as (_stashed, warnings):
        # ruff check --fix
        check = subprocess.run(
            ["uv", "run", "ruff", "check", "--fix", "--quiet", *abs_files],
            capture_output=True,
            text=True,
            cwd=project_root,
        )
        if check.returncode != 0 and check.stdout.strip():
            unfixable.extend(check.stdout.strip().splitlines()[:10])

        # ruff format
        subprocess.run(
            ["uv", "run", "ruff", "format", "--quiet", *abs_files],
            capture_output=True,
            text=True,
            cwd=project_root,
        )

        # Restrict amend scope to the merged Python files only.
        diff = subprocess.run(
            ["git", "-C", project_root, "diff", "--name-only", "HEAD", "--", *py_files],
            capture_output=True,
            text=True,
        )
        lint_changed = [f for f in diff.stdout.strip().splitlines() if f]
        if lint_changed:
            fixed = lint_changed
            subprocess.run(
                ["git", "-C", project_root, "add", "--", *lint_changed],
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["git", "-C", project_root, "commit", "--amend", "--no-edit"],
                capture_output=True,
                check=True,
            )

        if warnings:
            result["lint_warnings"] = warnings

    if fixed:
        result["lint_fixed"] = fixed
    if unfixable:
        result["lint_unfixable"] = unfixable
    return result


# -- Conflict detection --


def _detect_conflicts(project_root: str, branch_name: str) -> list[str]:
    """Use git merge-tree to predict conflicts without touching the working tree."""
    # Get the merge base
    base_result = subprocess.run(
        ["git", "-C", project_root, "merge-base", "HEAD", branch_name],
        capture_output=True,
        text=True,
    )
    if base_result.returncode != 0:
        return []

    merge_base = base_result.stdout.strip()
    result = subprocess.run(
        ["git", "-C", project_root, "merge-tree", merge_base, "HEAD", branch_name],
        capture_output=True,
        text=True,
    )
    # merge-tree outputs conflict markers if there are conflicts
    conflicts = []
    for line in result.stdout.splitlines():
        if line.startswith("changed in both"):
            # Extract filename from "changed in both" lines
            parts = line.split()
            if parts:
                conflicts.append(parts[-1])
    return conflicts


def _pick_resolver_agent() -> str:
    """Pick the best available agent for conflict resolution."""
    import shutil

    for agent in ("claude", "codex"):
        if shutil.which(agent):
            return agent
    return "claude"


def _resolve_conflicts_with_agent(
    project_root: str,
    branch_name: str,
    pane_record: dict,
    session_root: str,
    timeout: int = 300,
) -> bool:
    """Attempt to auto-resolve merge conflicts using an AI agent.

    1. Run `git merge --no-commit <branch>` to put conflict markers in working tree
    2. Spawn a resolver pane to fix them
    3. Wait for completion (done signal or output stabilization)
    4. If all resolved, commit. Otherwise abort and return False.
    """
    from dgov.done import _is_done
    from dgov.lifecycle import close_worker_pane, create_worker_pane
    from dgov.status import capture_worker_output

    # Start the merge — puts conflict markers in the working tree
    merge_result = subprocess.run(
        ["git", "-C", project_root, "merge", "--no-commit", branch_name],
        capture_output=True,
        text=True,
    )
    # Check if merge actually produced conflicts (it might just succeed)
    if merge_result.returncode == 0:
        # No conflicts, just commit
        subprocess.run(
            ["git", "-C", project_root, "commit", "--no-edit"],
            capture_output=True,
            text=True,
        )
        return True

    # List conflicted files
    unmerged = subprocess.run(
        ["git", "-C", project_root, "diff", "--name-only", "--diff-filter=U"],
        capture_output=True,
        text=True,
    )
    conflicted_files = [f.strip() for f in unmerged.stdout.strip().splitlines() if f.strip()]
    if not conflicted_files:
        # No unmerged files — merge --no-commit succeeded without conflicts
        subprocess.run(
            ["git", "-C", project_root, "commit", "--no-edit"],
            capture_output=True,
            text=True,
        )
        return True

    # Build resolver prompt
    file_list = "\n".join(f"  - {f}" for f in conflicted_files)
    resolver_prompt = (
        f"Resolve ALL merge conflicts in these files:\n{file_list}\n\n"
        f"For each file: open it, resolve the conflict markers "
        f"(<<<<<<< / ======= / >>>>>>>), pick the correct resolution, "
        f"then `git add` the file. Do NOT commit."
    )

    agent = _pick_resolver_agent()
    slug = f"resolve-{branch_name[:30]}"

    resolved = False
    resolver = None
    try:
        resolver = create_worker_pane(
            project_root=project_root,
            prompt=resolver_prompt,
            agent=agent,
            permission_mode="bypassPermissions",
            slug=slug,
            session_root=session_root,
            existing_worktree=project_root,
        )

        # Wait for done signal with timeout
        start = time.monotonic()
        poll_interval = 3
        last_output = None
        stable_since: float | None = None
        stable_threshold = 15

        while time.monotonic() - start < timeout:
            if _is_done(session_root, resolver.slug):
                break
            # Also check output stabilization
            output = capture_worker_output(
                project_root, resolver.slug, lines=20, session_root=session_root
            )
            if output is not None:
                if output == last_output:
                    if stable_since is None:
                        stable_since = time.monotonic()
                    elif time.monotonic() - stable_since >= stable_threshold:
                        break
                else:
                    last_output = output
                    stable_since = None
            time.sleep(poll_interval)

        # Check if conflicts were resolved
        still_unmerged = subprocess.run(
            ["git", "-C", project_root, "diff", "--name-only", "--diff-filter=U"],
            capture_output=True,
            text=True,
        )
        if not still_unmerged.stdout.strip():
            # All resolved — commit
            subprocess.run(
                ["git", "-C", project_root, "commit", "--no-edit"],
                capture_output=True,
                text=True,
            )
            resolved = True
    finally:
        if resolver is not None:
            close_worker_pane(project_root, resolver.slug, session_root=session_root)
        if not resolved:
            merge_head = Path(project_root) / ".git" / "MERGE_HEAD"
            if merge_head.exists():
                subprocess.run(
                    ["git", "-C", project_root, "merge", "--abort"],
                    capture_output=True,
                )

    return resolved


def _check_dirty_worktree(worktree_path: str, exclude_protected: bool = True) -> list[str]:
    """Check for uncommitted non-protected changes in a worktree.

    Returns a list of dirty file paths (relative to the worktree).
    If exclude_protected=True, protected files are excluded from the list.
    """
    if not worktree_path:
        return []

    if not Path(worktree_path).exists():
        return []

    # Check for uncommitted changes using NUL-delimited porcelain format
    status = subprocess.run(
        ["git", "-C", worktree_path, "status", "--porcelain", "-z"],
        capture_output=True,
    )
    if not status.stdout.strip(b"\x00"):
        return []

    skip = PROTECTED_FILES if exclude_protected else set()
    dirty_files = []
    entries = status.stdout.split(b"\x00")
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) < 4:
            i += 1
            continue
        xy = entry[:2].decode()
        filepath = entry[3:].decode()
        if xy[0] in ("R", "C"):
            i += 1
            if i < len(entries):
                filepath = entries[i].decode()
            continue
        if filepath and os.path.basename(filepath) not in skip:
            dirty_files.append(filepath)
        i += 1

    return dirty_files


def _restore_protected_files(project_root: str, pane_record: dict) -> None:
    """Restore protected files on the worker branch to match HEAD of main.

    Workers routinely clobber CLAUDE.md with unrelated content. This
    checks out the main-branch version of each protected file on the
    worker branch and amends the last commit, so the merge never
    carries the damage forward.
    """
    wt = pane_record.get("worktree_path")
    branch = pane_record.get("branch_name")
    base_sha = pane_record.get("base_sha", "")
    if not wt or not branch or not base_sha:
        return

    # Find which protected files were changed relative to base
    diff_result = subprocess.run(
        ["git", "-C", wt, "diff", "--name-only", base_sha, "HEAD"],
        capture_output=True,
        text=True,
    )
    if diff_result.returncode != 0:
        return

    changed = set(diff_result.stdout.strip().splitlines())
    to_restore = changed & PROTECTED_FILES
    if not to_restore:
        return

    # Restore each file from the base commit
    restored = []
    for fname in to_restore:
        cp = subprocess.run(
            ["git", "-C", wt, "checkout", base_sha, "--", fname],
            capture_output=True,
        )
        if cp.returncode == 0:
            restored.append(fname)
        else:
            logger.warning(
                "Failed to restore protected file %s on %s: %s",
                fname,
                branch,
                cp.stderr.decode().strip() if cp.stderr else "unknown error",
            )

    if not restored:
        logger.warning("No protected files could be restored on %s", branch)
        return

    # Amend the last commit to include the restoration
    subprocess.run(["git", "-C", wt, "add", "--"] + restored, capture_output=True)
    subprocess.run(
        ["git", "-C", wt, "commit", "--amend", "--no-edit"],
        capture_output=True,
    )

    logger.info("Restored protected files on %s: %s", branch, restored)


# -- Public merge API --


def merge_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    resolve: str = "skip",
    squash: bool = True,
    message: str | None = None,
    rebase: bool = False,
) -> dict:
    """Merge a worker pane's branch with configurable conflict resolution.

    Strict preconditions (enforced before any git mutation):
        1. Pane state must be exactly "done"
        2. No agent process may still be attached to the pane
        3. Worktree must have no uncommitted non-protected changes

    Fast path: git merge --ff-only (clean, no conflicts possible).
    Conflict path depends on ``resolve``:
        - "skip": return an error with conflict details and leave the worktree untouched
        - "agent": spawn AI agent to auto-resolve, fall back to manual on failure
        - "manual": leave conflict markers, user resolves

    Returns:
        {"merged": slug, "branch": ...} on success.
        {"conflicts": [...], ...} when conflicts need external resolution.
        {"error": ...} on failure.
    """
    import dgov.persistence as _persist
    from dgov.done import _agent_still_running
    from dgov.lifecycle import _full_cleanup

    session_root = os.path.abspath(session_root or project_root)
    target = _persist.get_pane(session_root, slug)

    if not target:
        return {"error": f"Pane not found: {slug}"}

    branch_name = target.get("branch_name")
    pane_project_root = target.get("project_root") or project_root
    worktree_path = target.get("worktree_path", "")
    if not branch_name:
        return {"error": f"Pane {slug} is missing branch_name"}

    # Precondition 1: Pane state must be exactly "done"
    # If already merged (e.g., monitor auto-merged), treat as success.
    pane_state = target.get("state", "")
    if pane_state == "merged":
        return {"merged": slug, "branch": branch_name, "already_merged": True}
    if pane_state not in ("done", "reviewed_pass"):
        return {
            "error": f"Pane {slug} is in state '{pane_state}', not 'done'",
            "slug": slug,
            "current_state": pane_state,
            "hint": "Worker must complete successfully before merge.",
        }

    # Precondition 2: No agent process may still be attached
    # Brief retry: --land can trigger merge before agent fully exits
    pane_id = target.get("pane_id", "")
    if pane_id and _agent_still_running(pane_id):
        import time as _time

        for _ in range(3):
            _time.sleep(1)
            if not _agent_still_running(pane_id):
                break
        else:
            return {
                "error": f"Agent process still attached to pane {slug}",
                "slug": slug,
                "pane_id": pane_id,
                "hint": "Wait for worker to complete or manually clean up the pane.",
            }

    # Precondition 3: Worktree must have no uncommitted non-protected changes
    dirty_files = _check_dirty_worktree(worktree_path, exclude_protected=True)
    if dirty_files:
        return {
            "error": f"Worktree for pane {slug} has uncommitted changes",
            "slug": slug,
            "dirty_files": dirty_files,
            "hint": "Commit or stash changes in the worktree before merging.",
        }

    # Pre-merge: restore protected files clobbered by workers
    _restore_protected_files(pane_project_root, target)

    # Capture diff stat before merge (for enriched return)
    base_sha = target.get("base_sha", "")
    merge_stat = ""
    merge_files_changed = 0
    changed_file_names: list[str] = []
    if base_sha:
        wt = target.get("worktree_path", "")
        if wt and Path(wt).exists():
            stat_r = subprocess.run(
                ["git", "-C", wt, "diff", "--stat", f"{base_sha}..HEAD"],
                capture_output=True,
                text=True,
            )
            if stat_r.returncode == 0:
                merge_stat = stat_r.stdout.strip()
            names_r = subprocess.run(
                ["git", "-C", wt, "diff", "--name-only", f"{base_sha}..HEAD"],
                capture_output=True,
                text=True,
            )
            if names_r.returncode == 0:
                changed_file_names = [f for f in names_r.stdout.strip().splitlines() if f]
                merge_files_changed = len(changed_file_names)

    with _MergeLock(pane_project_root):
        # Auto-rebase worker branch onto HEAD to prevent stale-branch conflicts.
        # Skip for rebase merges — _rebase_merge already rebases internally.
        rebase_fallback = False
        if not rebase:
            pre_rebase = _rebase_onto_head(pane_project_root, branch_name)
            if not pre_rebase.success:
                logger.warning(
                    "Auto-rebase failed for %s, falling back to plumbing merge: %s",
                    branch_name,
                    pre_rebase.stderr,
                )
                rebase_fallback = True

        with _candidate_worktree(pane_project_root, slug) as (candidate_root, _candidate_branch):
            if rebase:
                merge = _rebase_merge(candidate_root, branch_name, message=message)
            else:
                merge = _plumbing_merge(
                    candidate_root,
                    branch_name,
                    message=message,
                    squash=squash,
                )

            if merge.success:
                # Post-merge: lint + verify protected files BEFORE mutating main
                damaged: list[str] = []
                lint_result: dict = {}
                test_result: dict = {}
                base_sha = target.get("base_sha", "")
                if base_sha:
                    for fname in PROTECTED_FILES:
                        check = subprocess.run(
                            ["git", "-C", candidate_root, "diff", base_sha, "HEAD", "--", fname],
                            capture_output=True,
                        )
                        if check.stdout.strip():
                            damaged.append(fname)
                lint_result = _lint_fix_merged_files(candidate_root, changed_file_names)
                from dgov.inspection import _run_related_tests

                test_result = _run_related_tests(candidate_root, changed_file_names)
                if test_result:
                    logger.info(
                        "Post-merge tests: passed=%s files=%s",
                        test_result.get("tests_passed"),
                        test_result.get("tests_ran"),
                    )

                validation_failed = False
                validation_error: str | None = None

                tests_failed = False
                if test_result:
                    if not test_result.get("tests_passed"):
                        tests_failed = True
                    elif test_result.get("tests_failed", 0) > 0:
                        tests_failed = True

                warning_msg: str | None = None
                if damaged:
                    warning_msg = f"protected files changed: {damaged}"
                    logger.warning("Protected files changed after merge: %s", damaged)

                if tests_failed:
                    validation_failed = True
                    validation_error = (
                        f"Post-merge tests failed: "
                        f"{test_result.get('tests_failed', 'unknown')} failures "
                        f"in {test_result.get('tests_ran', 0)} tests ran"
                    )
                    logger.error("%s", validation_error)

                if lint_result.get("lint_unfixable"):
                    validation_failed = True
                    n_unfixable = len(lint_result["lint_unfixable"])
                    lint_error = f"Post-merge lint found unfixable issues: {n_unfixable} files"
                    if validation_error is None:
                        validation_error = lint_error
                    logger.error("%s", lint_error)

                if validation_failed:
                    _persist.emit_event(
                        session_root, "pane_merge_failed", slug, error=validation_error
                    )
                    return {
                        "error": validation_error,
                        "slug": slug,
                        "branch": branch_name,
                        "validation_failed": True,
                        "test_result": test_result,
                        "lint_result": lint_result,
                    }

                merge_sha_r = subprocess.run(
                    ["git", "-C", candidate_root, "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                merge_sha = merge_sha_r.stdout.strip() if merge_sha_r.returncode == 0 else ""
                apply_result = _advance_current_branch_to_commit(pane_project_root, merge_sha)
                if not apply_result.success:
                    error_msg = apply_result.stderr or "Failed to apply validated merge to main"
                    _persist.emit_event(session_root, "pane_merge_failed", slug, error=error_msg)
                    return {"error": error_msg}

                try:
                    _persist.update_pane_state(session_root, slug, "merged")
                except IllegalTransitionError as e:
                    if e.current == "abandoned":
                        logger.warning("Merge succeeded for stale abandoned pane: %s", slug)
                    else:
                        raise
                target["state"] = "merged"
                _full_cleanup(pane_project_root, session_root, slug, target)
                _persist.remove_pane(session_root, slug)
                _persist.emit_event(
                    session_root, "pane_merged", slug, merge_sha=merge_sha, branch=branch_name
                )

                # Regenerate codebase map so future workers get fresh context
                try:
                    if (Path(pane_project_root) / "src" / "dgov").is_dir():
                        from dgov.cli.admin import regenerate_codebase_md

                        regenerate_codebase_md(pane_project_root)
                except Exception:
                    logger.debug("CODEBASE.md regeneration skipped (non-critical)")

                result = {
                    "merged": slug,
                    "branch": branch_name,
                    "stat": merge_stat,
                    "files_changed": merge_files_changed,
                }
                if rebase_fallback:
                    result["rebase_fallback"] = True
                combined_warnings = [*merge.warnings, *apply_result.warnings]
                if combined_warnings:
                    result["stash_warnings"] = combined_warnings
                if lint_result:
                    result.update(lint_result)
                if test_result:
                    result.update(test_result)
                if warning_msg:
                    result["warning"] = warning_msg
                return result

    # Plumbing merge failed — detect conflicts for resolution
    conflicts = _detect_conflicts(pane_project_root, branch_name)

    if conflicts:
        _persist.update_pane_state(session_root, slug, "merge_conflict")
        if resolve == "agent":
            resolved = _resolve_conflicts_with_agent(
                pane_project_root, branch_name, target, session_root
            )
            if resolved:
                try:
                    _persist.update_pane_state(session_root, slug, "merged")
                except IllegalTransitionError as e:
                    if e.current == "abandoned":
                        logger.warning("Merge succeeded for stale abandoned pane: %s", slug)
                    else:
                        raise
                target["state"] = "merged"
                _full_cleanup(pane_project_root, session_root, slug, target)
                _persist.remove_pane(session_root, slug)
                _persist.emit_event(session_root, "pane_merged", slug, branch=branch_name)
                return {"merged": slug, "branch": branch_name, "resolved_by": "agent"}
            return {
                "slug": slug,
                "branch": branch_name,
                "conflicts": conflicts,
                "hint": "Agent resolution failed. Resolve conflicts manually.",
            }
        if resolve == "manual":
            subprocess.run(
                ["git", "-C", pane_project_root, "merge", "--no-commit", branch_name],
                capture_output=True,
                text=True,
            )
            return {
                "slug": slug,
                "branch": branch_name,
                "conflicts": conflicts,
                "resolve": "manual",
                "hint": "Conflict markers left in working tree. Resolve manually.",
            }
        if resolve == "skip":
            return {
                "error": f"Merge conflict in {branch_name}",
                "slug": slug,
                "branch": branch_name,
                "conflicts": conflicts,
                "hint": "Re-run with --resolve agent or --resolve manual.",
            }
        return {"error": f"Unknown resolve strategy: {resolve}"}

    error_msg = merge.stderr.strip() if merge.stderr else f"Merge failed for {branch_name}"
    _persist.emit_event(session_root, "pane_merge_failed", slug, error=error_msg)
    return {"error": error_msg}
