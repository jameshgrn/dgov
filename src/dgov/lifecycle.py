"""Pane lifecycle: create, close, resume, and cleanup."""

from __future__ import annotations

import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from dgov.agents import AgentDef, build_launch_command, load_registry
from dgov.backend import get_backend
from dgov.done import _wrap_done_signal
from dgov.gitops import _remove_worktree
from dgov.persistence import (
    PROTECTED_FILES,
    STATE_DIR,
    WorkerPane,
    _get_db,
    _retry_on_lock,
    add_pane,
    emit_event,
    get_child_panes,
    get_pane,
    remove_pane,
    update_pane_state,
)
from dgov.strategy import _generate_slug, _structure_pi_prompt, _validate_slug

logger = logging.getLogger(__name__)


def ensure_dgov_gitignored(project_root: str) -> None:
    """Add .dgov/ to .gitignore if not already present."""
    gitignore = Path(project_root) / ".gitignore"
    marker = ".dgov/"
    if gitignore.is_file():
        content = gitignore.read_text(encoding="utf-8")
        if marker not in content.splitlines():
            with open(gitignore, "a", encoding="utf-8") as f:
                if not content.endswith("\n"):
                    f.write("\n")
                f.write(f"{marker}\n")
    else:
        gitignore.write_text(f"{marker}\n", encoding="utf-8")


# -- Git worktree helpers --


def _create_worktree(project_root: str, worktree_path: str, branch_name: str) -> None:
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


def _state_icon(state: str) -> str:
    """Return the pane-title icon for a worker state."""
    return {
        "active": "~",
        "done": "ok",
        "merged": "+",
        "timed_out": "!",
        "failed": "X",
    }.get(state, "")


def _build_pane_title(agent: str, slug: str, project_root: str, *, state: str = "") -> str:
    """Build pane title for tmux pane border display.

    Format: ``[agent] slug`` or ``[agent] slug icon`` when *state* has a title icon.
    """
    title = f"[{agent}] {slug}"
    icon = _state_icon(state)
    return f"{title} {icon}" if icon else title


# -- Worker hook installation --

_PRE_MERGE_COMMIT_HOOK = """\
#!/usr/bin/env bash
echo "ERROR: Workers must not integrate branches (merge, pull, rebase)." >&2
echo "Commit your changes and let the governor handle integration." >&2
exit 1
"""


def _install_worker_hooks(worktree_path: str) -> None:
    """Install git hooks that prevent workers from integrating branches."""
    hooks_dir = Path(worktree_path) / ".dgov-worker-hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hooks_dir / "pre-merge-commit"
    hook_file.write_text(_PRE_MERGE_COMMIT_HOOK, encoding="utf-8")
    hook_file.chmod(0o755)
    subprocess.run(
        ["git", "-C", worktree_path, "config", "core.hooksPath", str(hooks_dir)],
        capture_output=True,
        check=True,
    )


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
    role: str = "worker",
) -> None:
    """Lock pane, inject env, trigger hook, rewrite prompt, launch agent."""

    backend = get_backend()

    # 1. Lock pane title, apply colour, disable renaming, start logging (single tmux call)
    title = _build_pane_title(agent_id, slug, project_root, state="active")
    logs_dir = Path(session_root) / STATE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(logs_dir / f"{slug}.log")
    backend.configure_worker_pane(
        pane_id, title, agent_id, color=agent_def.color, log_file=log_file
    )

    # 3. Build and send all env setup as a single compound shell command
    env_lines: list[str] = []
    # Only strip API keys for the claude agent (Claude Code should use OAuth).
    # Pi-routed variants (pi-claude, pi-codex, etc.) need API keys to reach
    # their upstream providers.
    if agent_id == "claude":
        for var in ("CLAUDECODE", "ANTHROPIC_API_KEY", "CLAUDE_CODE_API_KEY"):
            env_lines.append(f"unset {var}")
    for key, val in all_env.items():
        env_lines.append(f"export {key}={shlex.quote(val)}")
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
        env_lines.append(f"export {key}={shlex.quote(val)}")
    if env_lines:
        backend.send_shell_command(pane_id, " && ".join(env_lines))
        from dgov.tmux import wait_for_shell_ready

        if not wait_for_shell_ready(pane_id, timeout=2.0):
            logger.warning("Shell prompt not detected after env setup for %s", slug)

    # 4. Trigger worktree_created hook
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

    # 4b. Install pre-merge-commit hook to block branch integration in worktrees
    if owns_worktree:
        _install_worker_hooks(worktree_path)

    # 5. Auto-structure pi prompts
    if agent_id == "pi" and not skip_auto_structure:
        prompt = _structure_pi_prompt(prompt)

    # 6. Rewrite absolute paths so agent edits worktree, not main repo
    rewritten_prompt = re.sub(
        re.escape(project_root) + r"(?!/.dgov/worktrees/)", worktree_path, prompt
    )

    # 7. Fallback protected-file warning if hook didn't write CLAUDE.md
    if not hook_ran:
        protected_warning = (
            "\n\nIMPORTANT: Do NOT modify or overwrite these files: "
            + ", ".join(sorted(PROTECTED_FILES))
            + ". Do NOT create new documentation files."
        )
        if protected_warning.strip() not in rewritten_prompt:
            rewritten_prompt += protected_warning

    # 8. Build done-signal path — always clear stale signals from prior attempts
    done_signal = str(Path(session_root) / STATE_DIR / "done" / slug)
    Path(done_signal).parent.mkdir(parents=True, exist_ok=True)
    Path(done_signal).unlink(missing_ok=True)
    Path(done_signal + ".exit").unlink(missing_ok=True)

    # 9. Launch agent (with done-signal wrapper)
    # Workers always use headless mode; only LT-GOVs get TUI interactive mode.
    is_cursor = agent_id in ("cursor", "cursor-auto") or agent_def.prompt_command == "cursor-agent"
    use_interactive = agent_def.interactive and role != "worker"

    # When forcing headless on a normally-interactive agent, add agent-specific flags
    if not use_interactive and agent_def.interactive and role == "worker":
        if agent_id == "claude":
            extra_flags = f"-p {extra_flags}".strip()

    if use_interactive:
        # Interactive TUI mode: launch without prompt, send prompt via tmux after ready.
        base_cmd = build_launch_command(
            agent_id,
            rewritten_prompt,
            permission_mode,
            project_root=worktree_path,
            slug=slug,
            extra_flags=extra_flags,
            registry=registry,
        )
        wrapped_cmd = _wrap_done_signal(base_cmd, done_signal)
        backend.send_shell_command(pane_id, wrapped_cmd)
        ready_delay = agent_def.send_keys_ready_delay_ms or 2000
        time.sleep(ready_delay / 1000)

        # 9a. Cursor: accept workspace trust BEFORE sending prompt
        if is_cursor:
            backend.send_keys(pane_id, ["a"])
            time.sleep(2)

        backend.send_prompt_via_buffer(pane_id, rewritten_prompt)
    elif agent_def.prompt_transport == "send-keys":
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
        backend.send_shell_command(pane_id, wrapped_cmd)
        if agent_def.send_keys_ready_delay_ms > 0:
            time.sleep(agent_def.send_keys_ready_delay_ms / 1000)
        for key in agent_def.send_keys_pre_prompt:
            backend.send_keys(pane_id, [key])
        backend.send_prompt_via_buffer(pane_id, rewritten_prompt)
    else:
        force_headless = not use_interactive and agent_def.interactive
        launch_cmd = build_launch_command(
            agent_id,
            rewritten_prompt,
            permission_mode,
            project_root=worktree_path,
            slug=slug,
            extra_flags=extra_flags,
            registry=registry,
            force_headless=force_headless,
        )
        wrapped_cmd = _wrap_done_signal(launch_cmd, done_signal)
        backend.send_shell_command(pane_id, wrapped_cmd)

        # Cursor headless: accept workspace trust dialog (send twice for reliability)
        if is_cursor:
            time.sleep(5)
            backend.send_keys(pane_id, ["a"])
            time.sleep(1)
            backend.send_keys(pane_id, ["a"])


# -- Public API --


def _pi_extension_flags(project_root: str) -> str:
    """Return --extension flags for dgov pi extensions if they exist."""
    import importlib.resources

    ext_dir = Path(str(importlib.resources.files("dgov") / "pi-extensions"))
    if not ext_dir.is_dir():
        return ""
    flags = []
    for ext_file in sorted(ext_dir.glob("*.ts")):
        flags.append(f"--extension {ext_file}")
    return " ".join(flags)


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
    role: str = "worker",
    parent_slug: str = "",
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
    ensure_dgov_gitignored(project_root)
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

            # Symlink .venv from main repo so workers skip uv sync
            _main_venv = Path(project_root) / ".venv"
            _wt_venv = Path(worktree_path) / ".venv"
            if _main_venv.is_dir() and not _wt_venv.exists():
                _wt_venv.symlink_to(_main_venv)

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
        pane_id = get_backend().create_worker_pane(
            cwd=worktree_path, env=startup_env, name=slug, agent=agent
        )

        # Wait for shell to initialize before sending commands.
        # Without this, send-keys arrives before zsh loads .zshrc,
        # causing commands to echo raw then replay — garbling source scripts.
        from dgov.tmux import wait_for_shell_ready

        if not wait_for_shell_ready(pane_id, timeout=3.0):
            logger.warning("Shell ready timeout for %s — proceeding anyway", slug)

        # 4. Setup and launch agent
        pi_ext = _pi_extension_flags(project_root) if agent_def.prompt_command == "pi" else ""
        if pi_ext:
            extra_flags = f"{extra_flags} {pi_ext}".strip()
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
            role=role,
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
            role=role,
            parent_slug=parent_slug,
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
    # 1. Delete done signal, exit signal, and log file
    done_path = Path(session_root) / STATE_DIR / "done" / slug
    done_path.unlink(missing_ok=True)
    exit_path = Path(session_root) / STATE_DIR / "done" / (slug + ".exit")
    exit_path.unlink(missing_ok=True)
    log_path = Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"
    log_path.unlink(missing_ok=True)

    # 2. Kill tmux pane (kill-pane is synchronous — no retry needed)
    pane_id = pane_record.get("pane_id")
    if pane_id:
        get_backend().destroy(pane_id)

    # 3. Remove worktree + branch
    skipped_worktree = False
    branch_kept = False
    if remove_worktree and pane_record.get("owns_worktree", False):
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
                pane_state = pane_record.get("state", "")
                delete_flag = "-D" if pane_state == "merged" else "-d"
                br_result = subprocess.run(
                    ["git", "-C", project_root, "branch", delete_flag, branch],
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
    """Close a worker pane: kill tmux pane, remove worktree, update state.

    For LT-GOV or parent panes, cascades to close all child panes first.
    """
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    target = get_pane(session_root, slug)

    if not target:
        return True  # already cleaned up (e.g. by merge)

    # Cascade: close all child panes before closing the parent
    children = get_child_panes(session_root, slug)
    for child in children:
        child_slug = child["slug"]
        logger.info("Cascade-closing child pane %s (parent: %s)", child_slug, slug)
        close_worker_pane(project_root, child_slug, session_root, force=force)

    # Auto-enable force for merged/closed panes
    if target.get("state") in ("merged", "closed", "done", "failed"):
        force = True

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
    permission_mode: str = "bypassPermissions",
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
    pane_id = get_backend().create_worker_pane(
        cwd=worktree_path, env=startup_env, name=slug, agent=resume_agent
    )

    from dgov.tmux import wait_for_shell_ready

    if not wait_for_shell_ready(pane_id, timeout=3.0):
        logger.warning("Shell ready timeout for %s (resume) — proceeding anyway", slug)

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
        role=target.get("role", "worker"),
    )

    # Update state: new pane_id, back to active (targeted UPDATE, not full-row replace)
    def _do_resume_update():
        conn = _get_db(session_root)
        if agent:
            conn.execute(
                "UPDATE panes SET pane_id = ?, state = ?, agent = ? WHERE slug = ?",
                (pane_id, "active", resume_agent, slug),
            )
        else:
            conn.execute(
                "UPDATE panes SET pane_id = ?, state = ? WHERE slug = ?",
                (pane_id, "active", slug),
            )
        conn.commit()

    _retry_on_lock(_do_resume_update)

    emit_event(session_root, "pane_resumed", slug, agent=resume_agent)

    return {
        "resumed": True,
        "slug": slug,
        "agent": resume_agent,
        "pane_id": pane_id,
        "worktree": worktree_path,
    }
