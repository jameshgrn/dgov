"""Git worktree operations for fast, isolated agent execution.

Follows Lacustrine Pillars:
- Pillar #2: The Atomic Attempt (isolated checkout)
- Pillar #3: Snapshot Isolation (independent git state)
- Pillar #10: Fail-Closed (cleanup on failure)
"""

from __future__ import annotations

import contextlib
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
    if _should_link_shared_venv(root):
        _link_shared_venv(root, wt_path)

    # Capture Snapshot SHA
    res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        env=git_env,
        check=True,
        capture_output=True,
        text=True,
    )
    commit = res.stdout.strip()

    logger.debug("Created isolated worktree at %s", wt_path)
    return Worktree(path=wt_path, branch=branch_name, commit=commit)


def _should_link_shared_venv(project_root: Path) -> bool:
    """Only reuse a shared venv when the repo is not an editable Python project."""
    return (project_root / ".venv").exists() and not (project_root / "pyproject.toml").exists()


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


def prepare_worktree(
    wt: Worktree,
    *,
    language: str = "",
    setup_cmd: str = "",
    timeout_s: int = 300,
) -> None:
    """Prepare a worktree before dispatch so workers and gates see the right env."""
    env = _git_env(wt.path)
    if setup_cmd:
        _run_prepare_shell(wt.path, setup_cmd, timeout_s=timeout_s, env=env)
        return

    if language != "python":
        return
    if not (wt.path / "pyproject.toml").is_file():
        return

    cmd = ["uv", "sync", "--locked"] if (wt.path / "uv.lock").is_file() else ["uv", "sync"]
    _run_prepare_cmd(wt.path, cmd, timeout_s=timeout_s, env=env)


def _run_prepare_shell(
    worktree_path: Path,
    setup_cmd: str,
    *,
    timeout_s: int,
    env: dict[str, str],
) -> None:
    """Run a configured setup command inside the worktree."""
    try:
        result = subprocess.run(
            setup_cmd,
            shell=True,
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Worktree preparation timed out after {timeout_s}s running setup_cmd in "
            f"{worktree_path}. Fix: update [project].setup_cmd or run it manually in the "
            "worktree and verify it succeeds."
        ) from exc

    if result.returncode == 0:
        return

    output = ((result.stdout or "") + (result.stderr or "")).strip()[-500:]
    raise RuntimeError(
        "Worktree preparation failed running setup_cmd in "
        f"{worktree_path}. Fix: update [project].setup_cmd or run it manually in the "
        f"worktree.\n{output}"
    )


def _run_prepare_cmd(
    worktree_path: Path,
    cmd: list[str],
    *,
    timeout_s: int,
    env: dict[str, str],
) -> None:
    """Run a built-in worktree preparation command."""
    try:
        result = subprocess.run(
            cmd,
            cwd=worktree_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except FileNotFoundError as exc:
        binary = cmd[0]
        raise RuntimeError(
            f"Worktree preparation failed because '{binary}' is not in PATH. "
            f"Fix: install {binary} or set [project].setup_cmd to prepare {worktree_path}."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        command = " ".join(cmd)
        raise RuntimeError(
            f"Worktree preparation timed out after {timeout_s}s running '{command}' in "
            f"{worktree_path}. Fix: run the command manually in the worktree and verify it "
            "succeeds."
        ) from exc

    if result.returncode == 0:
        return

    command = " ".join(cmd)
    output = ((result.stdout or "") + (result.stderr or "")).strip()[-500:]
    raise RuntimeError(
        f"Worktree preparation failed running '{command}' in {worktree_path}. "
        "Fix: run the command manually in the worktree and resolve the dependency error.\n"
        f"{output}"
    )


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


class IntegrationCandidateResult:
    """Outcome of creating and validating an integration candidate."""

    def __init__(
        self,
        passed: bool,
        candidate_path: Path | None = None,
        candidate_sha: str = "",
        error: str | None = None,
    ) -> None:
        self.passed = passed
        self.candidate_path = candidate_path
        self.candidate_sha = candidate_sha
        self.error = error


def _integration_candidate_failure(error: str) -> IntegrationCandidateResult:
    return IntegrationCandidateResult(passed=False, error=error)


def _git_rev_parse(cwd: str | Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        env=_git_env(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _task_commits_to_replay(project_root: str, task_wt: Worktree) -> list[str]:
    commits_res = subprocess.run(
        ["git", "rev-list", "--reverse", f"{task_wt.commit}..{task_wt.branch}"],
        cwd=project_root,
        env=_git_env(project_root),
        check=True,
        capture_output=True,
        text=True,
    )
    return [commit for commit in commits_res.stdout.strip().split("\n") if commit]


def _replay_commit(candidate_wt: Worktree, commit_sha: str) -> str | None:
    pick_res = subprocess.run(
        ["git", "cherry-pick", commit_sha],
        cwd=candidate_wt.path,
        env=_git_env(candidate_wt.path),
        capture_output=True,
        text=True,
    )
    if pick_res.returncode == 0:
        return None
    subprocess.run(
        ["git", "cherry-pick", "--abort"],
        cwd=candidate_wt.path,
        env=_git_env(candidate_wt.path),
        capture_output=True,
    )
    return pick_res.stderr


def _cleanup_candidate_worktree(project_root: str, candidate_wt: Worktree | None) -> None:
    if candidate_wt is None:
        return
    with contextlib.suppress(Exception):
        remove_worktree(project_root, candidate_wt)


def create_integration_candidate(
    project_root: str,
    task_wt: Worktree,
    candidate_slug: str,
) -> IntegrationCandidateResult:
    """Replay task commits onto a temporary worktree at current HEAD."""
    candidate_wt: Worktree | None = None

    try:
        candidate_wt = create_worktree(
            project_root,
            candidate_slug,
            base_ref=_git_rev_parse(project_root),
        )
        commits = _task_commits_to_replay(project_root, task_wt)
        if not commits:
            _cleanup_candidate_worktree(project_root, candidate_wt)
            return _integration_candidate_failure("No commits to replay from task worktree")

        for commit_sha in commits:
            replay_error = _replay_commit(candidate_wt, commit_sha)
            if replay_error is not None:
                _cleanup_candidate_worktree(project_root, candidate_wt)
                return _integration_candidate_failure(
                    error=f"Failed to replay commit {commit_sha[:8]} onto current HEAD: "
                    f"{replay_error}",
                )

        return IntegrationCandidateResult(
            passed=True,
            candidate_path=candidate_wt.path,
            candidate_sha=_git_rev_parse(candidate_wt.path),
        )

    except subprocess.CalledProcessError as exc:
        _cleanup_candidate_worktree(project_root, candidate_wt)
        return _integration_candidate_failure(
            error=f"Git operation failed: {exc.stderr if hasattr(exc, 'stderr') else str(exc)}",
        )
    except Exception as exc:
        _cleanup_candidate_worktree(project_root, candidate_wt)
        return _integration_candidate_failure(
            error=f"Unexpected error creating integration candidate: {exc}",
        )


def remove_integration_candidate(project_root: str, candidate_path: Path) -> None:
    """Remove an ephemeral integration candidate worktree.

    Uses the same cleanup logic as regular worktrees but handles the
    ephemeral candidate naming convention.
    """
    git_env = _git_env(project_root)
    # Find branch from worktree listing
    res = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=project_root,
        env=git_env,
        capture_output=True,
        text=True,
    )
    branch_name: str | None = None
    current_path: str | None = None
    for line in res.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :].strip()
        elif (
            line.startswith("branch ")
            and current_path
            and Path(current_path).resolve() == candidate_path.resolve()
        ):
            branch_name = line[len("branch ") :].strip().removeprefix("refs/heads/")
            break

    # Remove worktree directory
    subprocess.run(
        ["git", "worktree", "remove", "-f", str(candidate_path)],
        cwd=project_root,
        env=git_env,
        capture_output=True,
    )
    # Remove branch if we found it
    if branch_name:
        subprocess.run(
            ["git", "branch", "-D", branch_name],
            cwd=project_root,
            env=git_env,
            capture_output=True,
        )


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
