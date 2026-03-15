"""Pane lifecycle: create, close, resume, and cleanup."""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from dgov.agents import AgentDef, build_launch_command, load_registry
from dgov.backend import get_backend
from dgov.gitops import _remove_worktree
from dgov.persistence import (
    PROTECTED_FILES,
    STATE_DIR,
    WorkerPane,
    _get_db,
    _insert_pane_dict,
    _row_to_dict,
    add_pane,
    emit_event,
    get_pane,
    remove_pane,
    update_pane_state,
)
from dgov.strategy import _generate_slug, _structure_pi_prompt, _validate_slug
from dgov.waiter import _wrap_done_signal

logger = logging.getLogger(__name__)


# -- Git worktree helpers --


def _create_worktree(project_root: str, worktree_path: str, branch_name: str) -> None:
    subprocess.run(["git", "-C", project_root, "worktree", "prune"], capture_output=True)

    # If worktree directory already exists for this branch, reuse it.
    if Path(worktree_path).is_dir():
        git_check = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
        if git_check.returncode == 0:
            return

    result = subprocess.run(
        ["git", "-C", project_root, "rev-parse", "--verify", branch_name],
        capture_output=True,
        text=True,
    )
    try:
        if result.returncode == 0:
            subprocess.run(
                ["git", "-C", project_root, "worktree", "add", worktree_path, branch_name],
                capture_output=True,
                text=True,
                check=True,
            )
        else:
            subprocess.run(
                ["git", "-C", project_root, "worktree", "add", "-b", branch_name, worktree_path],
                capture_output=True,
                text=True,
                check=True,
            )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"Failed to create worktree for branch {branch_name!r} "
            f"at path {worktree_path!r}: {e.stderr.strip()}"
        ) from e


# -- Hook trigger --


def _trigger_hook(
    hook_name: str,
    project_root: str,
    env_extra: dict[str, str],
    *,
    timeout: int = 10,
) -> bool:
    """Run a hook script if it exists. Returns True if a hook ran successfully.

    Searches directories in priority order (first match wins):
    1. .dgov/hooks/ (gitignored, local overrides)
    2. .dgov-hooks/ (version controlled, team hooks)
    3. ~/.dgov/hooks/ (global user hooks)
    """
    hook_dirs = [
        Path(project_root) / ".dgov" / "hooks",
        Path(project_root) / ".dgov-hooks",
        Path.home() / ".dgov" / "hooks",
    ]
    for hook_dir in hook_dirs:
        hook_path = hook_dir / hook_name
        if hook_path.is_file() and os.access(hook_path, os.X_OK):
            try:
                result = subprocess.run(
                    [str(hook_path)],
                    env={**os.environ, **env_extra},
                    cwd=project_root,
                    timeout=timeout,
                    capture_output=True,
                )
                return result.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                return False
    return False


# -- Pane title --


def _build_pane_title(slug: str, project_root: str) -> str:
    """Build pane title for tmux pane border display.

    Format: ``slug@project_name-hash`` where *hash* is the first 4 hex
    chars of the MD5 digest of *project_root*.
    """
    import hashlib

    project_name = os.path.basename(project_root)
    hash_prefix = hashlib.md5(project_root.encode()).hexdigest()[:4]
    return f"{slug}@{project_name}-{hash_prefix}"


# -- Shared launch pipeline --


def _setup_and_launch_agent(
    *,
    pane_id: str,
    slug: str,
    project_root: str,
    session_root: str,
    worktree_path: str,
    branch_name: str,
    agent_id: str,
    agent_def: AgentDef,
    registry: dict,
    permission_mode: str,
    prompt: str,
    hook_prompt: str,
    all_env: dict[str, str],
    extra_flags: str = "",
    owns_worktree: bool = True,
    base_sha: str = "",
    skip_auto_structure: bool = False,
    clear_done_signal: bool = False,
) -> None:
    """Lock pane, inject env, trigger hook, rewrite prompt, launch agent."""
    backend = get_backend()

    # 1. Lock pane title (prevent agent/tmux from overwriting)
    backend.set_pane_option(pane_id, "allow-rename", "off")
    backend.set_pane_option(pane_id, "automatic-rename", "off")
    title = _build_pane_title(slug, project_root)
    backend.set_title(pane_id, title)
    backend.style(pane_id, agent_id, color=agent_def.color)
    backend.set_pane_option(pane_id, "allow-set-title", "off")

    # 2. Scrub env vars that cause worker auth issues
    for var in ("CLAUDECODE", "ANTHROPIC_API_KEY", "CLAUDE_CODE_API_KEY"):
        backend.send_input(pane_id, f"unset {var}")

    # 4. Start persistent logging
    logs_dir = Path(session_root) / STATE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(logs_dir / f"{slug}.log")
    backend.start_logging(pane_id, log_file)

    # 5. Inject env vars
    for key, val in all_env.items():
        backend.send_input(pane_id, f"export {key}={val!r}")

    # 6. Export dgov state vars
    dgov_env = {
        "DGOV_ROOT": project_root,
        "DGOV_SESSION_ROOT": session_root,
        "DGOV_SLUG": slug,
        "DGOV_AGENT": agent_id,
        "DGOV_BRANCH": branch_name,
        "DGOV_BASE_SHA": base_sha,
        "DGOV_WORKTREE_PATH": worktree_path,
    }
    for key, val in dgov_env.items():
        backend.send_input(pane_id, f"export {key}={val!r}")

    # 7. Trigger worktree_created hook
    hook_env = {
        "DGOV_ROOT": project_root,
        "DGOV_PANE_ID": pane_id,
        "DGOV_SLUG": slug,
        "DGOV_PROMPT": hook_prompt,
        "DGOV_AGENT": agent_id,
        "DGOV_WORKTREE_PATH": worktree_path,
        "DGOV_BRANCH": branch_name,
        "DGOV_OWNS_WORKTREE": "1" if owns_worktree else "0",
    }
    hook_ran = _trigger_hook("worktree_created", project_root, hook_env)

    # 8. Auto-structure pi prompts
    if agent_id == "pi" and not skip_auto_structure:
        prompt = _structure_pi_prompt(prompt)

    # 9. Rewrite absolute paths so agent edits worktree, not main repo
    rewritten_prompt = re.sub(
        re.escape(project_root) + r"(?!/.dgov/worktrees/)", worktree_path, prompt
    )

    # 10. Fallback protected-file warning if hook didn't write CLAUDE.md
    if not hook_ran:
        protected_warning = (
            "\n\nIMPORTANT: Do NOT modify or overwrite these files: "
            + ", ".join(sorted(PROTECTED_FILES))
            + ". Do NOT create new documentation files."
        )
        if protected_warning.strip() not in rewritten_prompt:
            rewritten_prompt += protected_warning

    # 11. Build done-signal path
    done_signal = str(Path(session_root) / STATE_DIR / "done" / slug)
    Path(done_signal).parent.mkdir(parents=True, exist_ok=True)
    if clear_done_signal:
        Path(done_signal).unlink(missing_ok=True)

    # 12. Launch agent (with done-signal wrapper)
    if agent_def.prompt_transport == "send-keys":
        base_cmd = build_launch_command(
            agent_id,
            None,
            permission_mode,
            project_root=worktree_path,
            slug=slug,
            extra_flags=extra_flags,
            registry=registry,
        )
        wrapped_cmd = _wrap_done_signal(base_cmd, done_signal)
        backend.send_input(pane_id, wrapped_cmd)
        if agent_def.send_keys_ready_delay_ms > 0:
            time.sleep(agent_def.send_keys_ready_delay_ms / 1000)
        for key in agent_def.send_keys_pre_prompt:
            backend.send_keys(pane_id, [key])
        backend.send_prompt_via_buffer(pane_id, rewritten_prompt)
    else:
        launch_cmd = build_launch_command(
            agent_id,
            rewritten_prompt,
            permission_mode,
            project_root=worktree_path,
            slug=slug,
            extra_flags=extra_flags,
            registry=registry,
        )
        wrapped_cmd = _wrap_done_signal(launch_cmd, done_signal)
        backend.send_input(pane_id, wrapped_cmd)


# -- Public API --


def create_worker_pane(
    project_root: str,
    prompt: str,
    agent: str = "claude",
    permission_mode: str = "bypassPermissions",
    slug: str | None = None,
    env_vars: dict[str, str] | None = None,
    extra_flags: str = "",
    session_root: str | None = None,
    existing_worktree: str | None = None,
    skip_auto_structure: bool = False,
) -> WorkerPane:
    """Create a worker pane: worktree + tmux split + agent launch.

    Args:
        project_root: Git repo for the worktree (where the work happens).
        session_root: Where .dgov/state.json lives. Defaults to project_root.
        existing_worktree: Use this path as CWD instead of creating a new worktree.
            Useful for conflict resolution where we operate on the main repo directly.
    """
    from dgov.status import _count_active_agent_workers

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    slug = slug or _generate_slug(prompt)
    _validate_slug(slug)
    owns_worktree = existing_worktree is None
    branch_name = slug
    worktree_path = (
        existing_worktree
        if existing_worktree
        else str(Path(project_root) / ".dgov" / "worktrees" / slug)
    )

    # 0. Validate env vars BEFORE any side effects
    all_env: dict[str, str] = {}
    registry = load_registry(project_root)
    agent_def = registry.get(agent)
    if agent_def is None:
        raise ValueError(f"Unknown agent {agent!r}. Available: {sorted(registry)}")
    all_env.update(agent_def.env)
    if env_vars:
        all_env.update(env_vars)
    for key in all_env:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            raise ValueError(f"Invalid environment variable name: {key!r}")

    # 1. Capture base SHA (HEAD of project_root before worktree creation)
    base_sha_result = subprocess.run(
        ["git", "-C", project_root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    base_sha = base_sha_result.stdout.strip() if base_sha_result.returncode == 0 else ""

    # From here on, side effects need cleanup on failure
    pane_id: str | None = None
    try:
        # 2. Create git worktree (skip if using existing path)
        if owns_worktree:
            _create_worktree(project_root, worktree_path, branch_name)

        # 2b. Generic health check (config-driven)
        if agent_def.health_check:
            hc = subprocess.run(agent_def.health_check, shell=True, capture_output=True, text=True)
            if hc.returncode != 0 and agent_def.health_fix:
                subprocess.run(agent_def.health_fix, shell=True, capture_output=True, text=True)
                hc = subprocess.run(
                    agent_def.health_check, shell=True, capture_output=True, text=True
                )
            if hc.returncode != 0:
                raise RuntimeError(f"Health check failed for {agent}: {agent_def.health_check}")

        # 2c. Generic concurrency guard (config-driven)
        if agent_def.max_concurrent is not None:
            active = _count_active_agent_workers(session_root, agent)
            if active >= agent_def.max_concurrent:
                raise RuntimeError(
                    f"Concurrency limit: {active} {agent} workers already running "
                    f"(max {agent_def.max_concurrent}). "
                    f"Wait for one to finish or use a different agent."
                )

        # 3. Create background worker pane
        startup_env = {
            "DISABLE_AUTO_UPDATE": "true",
            "DISABLE_UPDATE_PROMPT": "true",
        }
        get_backend().setup_pane_borders()
        pane_id = get_backend().create_worker_pane(cwd=worktree_path, env=startup_env, name=slug)
        time.sleep(0.25)

        # 4. Setup and launch agent
        _setup_and_launch_agent(
            pane_id=pane_id,
            slug=slug,
            project_root=project_root,
            session_root=session_root,
            worktree_path=worktree_path,
            branch_name=branch_name,
            agent_id=agent,
            agent_def=agent_def,
            registry=registry,
            permission_mode=permission_mode,
            prompt=prompt,
            hook_prompt=prompt,
            all_env=all_env,
            extra_flags=extra_flags,
            owns_worktree=owns_worktree,
            base_sha=base_sha,
            skip_auto_structure=skip_auto_structure,
        )

        # 5. Build pane record and save to state
        pane = WorkerPane(
            slug=slug,
            prompt=prompt,
            pane_id=pane_id,
            agent=agent,
            project_root=project_root,
            worktree_path=worktree_path,
            branch_name=branch_name,
            owns_worktree=owns_worktree,
            base_sha=base_sha,
        )
        add_pane(session_root, pane)

        emit_event(
            session_root,
            "pane_created",
            slug,
            agent=agent,
            prompt=prompt[:200],
            base_sha=base_sha,
        )

        return pane

    except BaseException:
        if pane_id:
            get_backend().destroy(pane_id)
        if owns_worktree and Path(worktree_path).exists():
            _remove_worktree(project_root, worktree_path, branch_name)
        raise


def _full_cleanup(
    project_root: str,
    session_root: str,
    slug: str,
    pane_record: dict,
    *,
    remove_worktree: bool = True,
    skip_worktree_if_dirty: bool = False,
) -> dict:
    """Single cleanup function for all pane teardown paths.

    Handles: kill tmux pane, remove from state, delete done signal,
    remove git worktree + branch.

    Returns {"cleaned": True, "skipped_worktree": bool}.
    """
    # 1. Delete done signal
    done_path = Path(session_root) / STATE_DIR / "done" / slug
    done_path.unlink(missing_ok=True)

    # 2. Kill tmux pane
    pane_id = pane_record.get("pane_id")
    if pane_id:
        get_backend().destroy(pane_id)
        if get_backend().is_alive(pane_id):
            time.sleep(0.2)
            get_backend().destroy(pane_id)

    # 3. Remove worktree + branch
    skipped_worktree = False
    branch_kept = False
    if remove_worktree and pane_record.get("owns_worktree", True):
        wt = pane_record.get("worktree_path")
        branch = pane_record.get("branch_name")

        if skip_worktree_if_dirty and wt and Path(wt).exists():
            check = subprocess.run(
                ["git", "-C", wt, "status", "--porcelain"], capture_output=True, text=True
            )
            if check.stdout.strip():
                logger.warning(
                    "Worktree %s has uncommitted changes"
                    " — skipping removal (use --force to override)",
                    wt,
                )
                skipped_worktree = True

        if not skipped_worktree and wt:
            subprocess.run(
                ["git", "-C", project_root, "worktree", "remove", "--force", wt],
                capture_output=True,
            )
            if branch:
                br_result = subprocess.run(
                    ["git", "-C", project_root, "branch", "-d", branch],
                    capture_output=True,
                    text=True,
                )
                if br_result.returncode != 0:
                    logger.warning(
                        "Branch %s not deleted (has unmerged commits). "
                        "Use git branch -D %s to force.",
                        branch,
                        branch,
                    )
                    branch_kept = True
        if not skipped_worktree:
            subprocess.run(
                ["git", "-C", project_root, "worktree", "prune"],
                capture_output=True,
            )

    return {"cleaned": True, "skipped_worktree": skipped_worktree, "branch_kept": branch_kept}


def close_worker_pane(
    project_root: str, slug: str, session_root: str | None = None, *, force: bool = False
) -> bool:
    """Close a worker pane: kill tmux pane, remove worktree, update state."""
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    target = get_pane(session_root, slug)

    if not target:
        return True  # already cleaned up (e.g. by merge)

    result = _full_cleanup(
        project_root,
        session_root,
        slug,
        target,
        skip_worktree_if_dirty=not force,
    )
    if not result.get("skipped_worktree"):
        update_pane_state(session_root, slug, "closed")
        emit_event(session_root, "pane_closed", slug)
        remove_pane(session_root, slug)
    if result.get("branch_kept"):
        logger.info(
            "Branch %s was kept because it has unmerged commits. "
            "Merge or cherry-pick its changes, then delete with: git branch -D %s",
            target.get("branch_name", "?"),
            target.get("branch_name", "?"),
        )
    return True


def resume_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    agent: str | None = None,
    prompt: str | None = None,
    permission_mode: str = "acceptEdits",
) -> dict:
    """Resume a pane by re-launching an agent in its existing worktree.

    Works when the agent crashed, tmux pane died, or a dirty close left the
    worktree on disk. Reuses the same slug, branch, and worktree.
    """
    from dgov.status import _count_active_agent_workers

    session_root = os.path.abspath(session_root or project_root)
    target = get_pane(session_root, slug)

    if not target:
        return {"error": f"Pane not found: {slug}"}

    worktree_path = target.get("worktree_path", "")
    branch_name = target.get("branch_name", "")

    # Verify worktree still exists on disk
    if not worktree_path or not Path(worktree_path).exists():
        return {"error": f"Worktree no longer exists: {worktree_path}"}

    # Verify branch still exists
    branch_check = subprocess.run(
        ["git", "-C", project_root, "rev-parse", "--verify", branch_name],
        capture_output=True,
        text=True,
    )
    if branch_check.returncode != 0:
        return {"error": f"Branch no longer exists: {branch_name}"}

    # Kill old tmux pane if it still exists
    old_pane_id = target.get("pane_id", "")
    if old_pane_id and get_backend().is_alive(old_pane_id):
        get_backend().destroy(old_pane_id)

    # Resolve agent and prompt
    resume_agent = agent or target.get("agent", "claude")
    original_prompt = prompt or target.get("prompt", "")

    # Load registry for agent config
    registry = load_registry(project_root)
    agent_def = registry.get(resume_agent)
    if agent_def is None:
        return {"error": f"Unknown agent {resume_agent!r}. Available: {sorted(registry)}"}

    # Health check (config-driven)
    if agent_def.health_check:
        hc = subprocess.run(agent_def.health_check, shell=True, capture_output=True, text=True)
        if hc.returncode != 0 and agent_def.health_fix:
            subprocess.run(agent_def.health_fix, shell=True, capture_output=True, text=True)
            hc = subprocess.run(agent_def.health_check, shell=True, capture_output=True, text=True)
        if hc.returncode != 0:
            return {"error": f"Health check failed for {resume_agent}: {agent_def.health_check}"}

    # Concurrency guard (config-driven)
    if agent_def.max_concurrent is not None:
        active = _count_active_agent_workers(session_root, resume_agent)
        if active >= agent_def.max_concurrent:
            return {
                "error": f"Concurrency limit: {active} {resume_agent} workers "
                f"running (max {agent_def.max_concurrent})"
            }

    # Build resume prompt
    resume_context = (
        "\n\nYou are RESUMING a previous session in this worktree. "
        "Run 'git status' and 'git log --oneline -5' first to see what has "
        "already been done. Continue from where the previous agent left off. "
        "Do NOT redo work that is already committed."
    )
    full_prompt = original_prompt + resume_context

    # Build all_env from agent config (filtering invalid names)
    all_env: dict[str, str] = {}
    if agent_def.env:
        for key, val in agent_def.env.items():
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                all_env[key] = val

    # Create background worker pane
    startup_env = {
        "DISABLE_AUTO_UPDATE": "true",
        "DISABLE_UPDATE_PROMPT": "true",
    }
    get_backend().setup_pane_borders()
    pane_id = get_backend().create_worker_pane(cwd=worktree_path, env=startup_env, name=slug)
    time.sleep(0.25)

    _setup_and_launch_agent(
        pane_id=pane_id,
        slug=slug,
        project_root=project_root,
        session_root=session_root,
        worktree_path=worktree_path,
        branch_name=branch_name,
        agent_id=resume_agent,
        agent_def=agent_def,
        registry=registry,
        permission_mode=permission_mode,
        prompt=full_prompt,
        hook_prompt=original_prompt,
        all_env=all_env,
        owns_worktree=True,
        base_sha=target.get("base_sha", ""),
        clear_done_signal=True,
    )

    # Update state: new pane_id, back to active
    conn = _get_db(session_root)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM panes WHERE slug = ?", (slug,)).fetchone()
    if row:
        d = _row_to_dict(row)
        d["pane_id"] = pane_id
        d["state"] = "active"
        if agent:
            d["agent"] = resume_agent
        _insert_pane_dict(conn, d)
        conn.commit()

    emit_event(session_root, "pane_resumed", slug, agent=resume_agent)

    return {
        "resumed": True,
        "slug": slug,
        "agent": resume_agent,
        "pane_id": pane_id,
        "worktree": worktree_path,
    }
