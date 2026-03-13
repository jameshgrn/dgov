"""Git merge, conflict resolution, and post-merge operations."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from dgov.models import MergeResult
from dgov.persistence import _PROTECTED_FILES

logger = logging.getLogger(__name__)


# -- Plumbing merge --


def _plumbing_merge(
    project_root: str, branch_name: str, message: str | None = None
) -> MergeResult:
    """Merge branch into HEAD using git plumbing (zero side effects on failure).

    Uses git merge-tree for in-memory merge computation. If the merge fails,
    no working tree changes occur — safer than porcelain git merge.
    """
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

    # Create merge commit
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
            "-p",
            branch_tip.stdout.strip(),
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
        return MergeResult(success=False, stderr="reset --hard failed after ref update")

    return MergeResult(success=True)


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

    # ruff check --fix
    check = subprocess.run(
        ["ruff", "check", "--fix", "--quiet", *abs_files],
        capture_output=True,
        text=True,
        cwd=project_root,
    )
    if check.returncode != 0 and check.stdout.strip():
        unfixable.extend(check.stdout.strip().splitlines()[:10])

    # ruff format
    subprocess.run(
        ["ruff", "format", "--quiet", *abs_files],
        capture_output=True,
        text=True,
        cwd=project_root,
    )

    # Check if lint changed anything
    diff = subprocess.run(
        ["git", "-C", project_root, "diff", "--name-only"],
        capture_output=True,
        text=True,
    )
    lint_changed = [f for f in diff.stdout.strip().splitlines() if f]
    if lint_changed:
        fixed = lint_changed
        # Amend merge commit with lint fixes
        subprocess.run(
            ["git", "-C", project_root, "add", *lint_changed],
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", project_root, "commit", "--amend", "--no-edit"],
            capture_output=True,
        )

    result = {}
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
    # Access functions through dgov.panes so test mocks propagate
    import dgov.panes as _p

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

    agent = _p._pick_resolver_agent()
    slug = f"resolve-{branch_name[:30]}"

    resolved = False
    resolver = None
    try:
        resolver = _p.create_worker_pane(
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
            if _p._is_done(session_root, resolver.slug):
                break
            # Also check output stabilization
            output = _p.capture_worker_output(
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
            _p.close_worker_pane(project_root, resolver.slug, session_root=session_root)
        if not resolved:
            merge_head = Path(project_root) / ".git" / "MERGE_HEAD"
            if merge_head.exists():
                subprocess.run(
                    ["git", "-C", project_root, "merge", "--abort"],
                    capture_output=True,
                )

    return resolved


def _commit_worktree(pane_record: dict) -> dict:
    """Auto-commit uncommitted changes in a worker's worktree.

    Stages all modified/new files except hook artifacts like CLAUDE.md.
    Returns {"committed": True, "files": [...]} or {"committed": False}.
    """
    wt = pane_record.get("worktree_path")
    if not wt or not Path(wt).exists():
        return {"committed": False}

    # Check for uncommitted changes using NUL-delimited porcelain format
    status = subprocess.run(["git", "-C", wt, "status", "--porcelain", "-z"], capture_output=True)
    if not status.stdout.strip(b"\x00"):
        return {"committed": False}

    skip = _PROTECTED_FILES
    files_to_add = []
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
        if filepath and os.path.basename(filepath) not in skip:
            files_to_add.append(filepath)
        i += 1

    if not files_to_add:
        return {"committed": False}

    # Stage files
    subprocess.run(
        ["git", "-C", wt, "add", "--"] + files_to_add,
        capture_output=True,
        check=True,
    )

    prompt = pane_record.get("prompt", "worker changes")
    slug = pane_record.get("slug", "worker")
    subject = prompt.split("\n")[0][:72].rstrip(".")

    subprocess.run(
        ["git", "-C", wt, "commit", "-m", f"{subject}\n\nWorker: {slug}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {"committed": True, "files": files_to_add}


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
    to_restore = changed & _PROTECTED_FILES
    if not to_restore:
        return

    # Restore each file from the base commit
    for fname in to_restore:
        subprocess.run(
            ["git", "-C", wt, "checkout", base_sha, "--", fname],
            capture_output=True,
        )

    # Amend the last commit to include the restoration
    subprocess.run(["git", "-C", wt, "add", "--"] + list(to_restore), capture_output=True)
    subprocess.run(
        ["git", "-C", wt, "commit", "--amend", "--no-edit"],
        capture_output=True,
    )

    logger.info("Restored protected files on %s: %s", branch, to_restore)


# -- Public merge API --


def merge_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    resolve: str = "agent",
) -> dict:
    """Merge a worker pane's branch with configurable conflict resolution.

    Fast path: git merge --ff-only (clean, no conflicts possible).
    Conflict path depends on ``resolve``:
        - "agent": spawn AI agent to auto-resolve, fall back to manual on failure
        - "manual": leave conflict markers, user resolves

    Returns:
        {"merged": slug, "branch": ...} on success.
        {"conflicts": [...], ...} when conflicts need external resolution.
        {"error": ...} on failure.
    """
    # Access functions through dgov.panes so test mocks propagate
    import dgov.panes as _p

    session_root = os.path.abspath(session_root or project_root)
    target = _p._get_pane(session_root, slug)

    if not target:
        return {"error": f"Pane not found: {slug}"}

    branch_name = target.get("branch_name")
    pane_project_root = target.get("project_root") or project_root

    # Auto-commit uncommitted changes in worktree
    commit_result = _p._commit_worktree(target)

    # Pre-merge hook: restore protected files, etc.
    pre_merge_env = {
        "DGOV_PROJECT_ROOT": pane_project_root,
        "DGOV_WORKTREE_PATH": target.get("worktree_path", ""),
        "DGOV_BRANCH": branch_name or "",
        "DGOV_BASE_SHA": target.get("base_sha", ""),
        "DGOV_SLUG": slug,
        "DGOV_PROTECTED_FILES": " ".join(sorted(_PROTECTED_FILES)),
    }
    if not _p._trigger_hook("pre_merge", pane_project_root, pre_merge_env, timeout=30):
        _p._restore_protected_files(pane_project_root, target)

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

    # Plumbing merge — zero working-tree side effects on failure
    merge = _p._plumbing_merge(pane_project_root, branch_name)

    if merge.success:
        _p._update_pane_state(session_root, slug, "merged")
        merge_sha_r = subprocess.run(
            ["git", "-C", pane_project_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        merge_sha = merge_sha_r.stdout.strip() if merge_sha_r.returncode == 0 else ""
        _p._emit_event(session_root, "pane_merged", slug, merge_sha=merge_sha, branch=branch_name)
        _p._full_cleanup(pane_project_root, session_root, slug, target)

        # Post-merge hook: lint, verify protected files, etc.
        post_merge_env = {
            "DGOV_PROJECT_ROOT": pane_project_root,
            "DGOV_BASE_SHA": target.get("base_sha", ""),
            "DGOV_SLUG": slug,
            "DGOV_BRANCH": branch_name or "",
            "DGOV_CHANGED_FILES": "\n".join(changed_file_names),
            "DGOV_PROTECTED_FILES": " ".join(sorted(_PROTECTED_FILES)),
        }
        hook_ran = _p._trigger_hook("post_merge", pane_project_root, post_merge_env, timeout=30)

        # Fallback: inline lint + verification if no hook
        damaged: list[str] = []
        lint_result: dict = {}
        if not hook_ran:
            base_sha = target.get("base_sha", "")
            if base_sha:
                for fname in _PROTECTED_FILES:
                    check = subprocess.run(
                        ["git", "-C", pane_project_root, "diff", base_sha, "HEAD", "--", fname],
                        capture_output=True,
                    )
                    if check.stdout.strip():
                        damaged.append(fname)
                if damaged:
                    logger.warning("Protected files changed after merge: %s", damaged)
            lint_result = _p._lint_fix_merged_files(pane_project_root, changed_file_names)

        result = {
            "merged": slug,
            "branch": branch_name,
            "stat": merge_stat,
            "files_changed": merge_files_changed,
        }
        if commit_result.get("committed"):
            result["auto_committed"] = commit_result["files"]
        if damaged:
            result["warning"] = f"protected files changed: {damaged}"
        if lint_result:
            result.update(lint_result)
        return result

    # Plumbing merge failed — detect conflicts for resolution
    conflicts = _p._detect_conflicts(pane_project_root, branch_name)

    if conflicts:
        _p._update_pane_state(session_root, slug, "merge_conflict")
        if resolve == "agent":
            resolved = _p._resolve_conflicts_with_agent(
                pane_project_root, branch_name, target, session_root
            )
            if resolved:
                _p._update_pane_state(session_root, slug, "merged")
                return {"merged": slug, "branch": branch_name, "resolved_by": "agent"}
            return {
                "slug": slug,
                "branch": branch_name,
                "conflicts": conflicts,
                "hint": "Agent resolution failed. Resolve conflicts manually.",
            }
        else:
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

    error_msg = merge.stderr.strip() if merge.stderr else f"Merge failed for {branch_name}"
    _p._emit_event(session_root, "pane_merge_failed", slug, error=error_msg)
    return {"error": error_msg}


def merge_worker_pane_with_close(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    resolve: str = "agent",
) -> dict:
    """Merge the branch and then close the worker pane.

    Args:
        project_root: Git repo root (where worktree is).
        slug: Pane slug to merge.
        session_root: Where .dgov/state.json lives. Defaults to project_root.
        resolve: Conflict resolution mode ("agent", "manual").

    Returns:
        {"merged": slug, "branch": branch_name} after successful merge and close.
        {"error": error_message} on failure.
    """
    # Access functions through dgov.panes so test mocks propagate
    import dgov.panes as _p

    session_root = os.path.abspath(session_root or project_root)
    result = _p.merge_worker_pane(project_root, slug, session_root, resolve=resolve)

    if "error" in result:
        return result

    # Close the pane after successful merge. Note: merge_worker_pane already cleans up on success,
    # so close may fail silently because pane is no longer in state — that's expected.
    if not _p.close_worker_pane(project_root, slug, session_root):
        logger.debug("Pane %s already cleaned up by merge worker", slug)

    return {"merged": slug, "branch": result["branch"]}
