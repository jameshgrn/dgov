"""Core pane lifecycle: create, close, list, merge.

Each worker pane = git worktree + tmux pane + agent CLI.
State tracked in .dgov/state.json.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from dgov.agents import build_launch_command, load_registry
from dgov.backend import get_backend
from dgov.batch import (  # noqa: F401
    _compute_tiers,
    create_checkpoint,
    list_checkpoints,
    run_batch,
)
from dgov.experiment import (  # noqa: F401
    ExperimentLog,
    run_experiment,
    run_experiment_loop,
)
from dgov.merger import (  # noqa: F401
    _commit_worktree,
    _detect_conflicts,
    _lint_fix_merged_files,
    _pick_resolver_agent,
    _plumbing_merge,
    _resolve_conflicts_with_agent,
    _restore_protected_files,
    merge_worker_pane,
    merge_worker_pane_with_close,
)
from dgov.openrouter import (  # noqa: F401
    _qwen_4b_request,
    chat_completion,
)

# -- Internal imports from split modules --
from dgov.persistence import (  # noqa: F401
    _PROTECTED_FILES,
    _STATE_DIR,
    PANE_STATES,
    VALID_EVENTS,
    VALID_TRANSITIONS,
    IllegalTransitionError,
    WorkerPane,
    _add_pane,
    _all_panes,
    _emit_event,
    _get_db,
    _get_pane,
    _insert_pane_dict,
    _remove_pane,
    _row_to_dict,
    _set_pane_metadata,
    _update_pane_state,
    _validate_state,
)
from dgov.responder import (  # noqa: F401
    BUILT_IN_RULES,
    COOLDOWN_SECONDS,
    ResponseRule,
    auto_respond,
    check_cooldown,
    load_response_rules,
    match_response,
    record_cooldown,
    reset_cooldowns,
)
from dgov.retry import (  # noqa: F401
    RetryPolicy,
    get_retry_policy,
    maybe_auto_retry,
    retry_context,
)
from dgov.review_fix import (  # noqa: F401
    ReviewFinding,
    parse_review_findings,
    run_review_fix_pipeline,
)
from dgov.strategy import (  # noqa: F401
    _SLUG_RE,
    _generate_slug,
    _structure_pi_prompt,
    _validate_slug,
    classify_task,
)
from dgov.templates import (  # noqa: F401
    BUILT_IN_TEMPLATES,
    PromptTemplate,
    list_templates,
    load_templates,
    render_template,
)
from dgov.waiter import (  # noqa: F401
    _AGENT_COMMANDS,
    PaneTimeoutError,
    _agent_still_running,
    _detect_blocked,
    _has_new_commits,
    _is_done,
    _poll_once,
    _wrap_done_signal,
    interact_with_pane,
    nudge_pane,
    signal_pane,
    wait_all_worker_panes,
    wait_worker_pane,
)

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


def _remove_worktree(project_root: str, worktree_path: str, branch_name: str) -> None:
    subprocess.run(
        ["git", "-C", project_root, "worktree", "remove", "--force", worktree_path],
        capture_output=True,
    )
    subprocess.run(["git", "-C", project_root, "branch", "-D", branch_name], capture_output=True)
    subprocess.run(["git", "-C", project_root, "worktree", "prune"], capture_output=True)


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


# -- Freshness --


def _count_auto_responses(session_root: str, slug: str) -> int:
    """Count pane_auto_responded events for a slug from the event journal."""
    import json as _json

    events_path = Path(session_root) / _STATE_DIR / "events.jsonl"
    if not events_path.exists():
        return 0
    count = 0
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if ev.get("event") == "pane_auto_responded" and ev.get("pane") == slug:
                count += 1
    return count


def _compute_freshness(project_root: str, pane_record: dict) -> dict:
    """Compute freshness score for a pane relative to main.

    Returns {"freshness": "fresh"|"warn"|"stale", "commits_since_base": int,
             "overlapping_files": [...], "pane_age_hours": float}
    """
    base_sha = pane_record.get("base_sha", "")
    created_at = pane_record.get("created_at", 0)
    wt = pane_record.get("worktree_path", "")

    age_hours = (time.time() - created_at) / 3600 if created_at else 0

    # Commits on main since base
    commits_since = 0
    if base_sha:
        log_result = subprocess.run(
            ["git", "-C", project_root, "log", f"{base_sha}..HEAD", "--oneline"],
            capture_output=True,
            text=True,
        )
        if log_result.returncode == 0:
            commits_since = len([ln for ln in log_result.stdout.strip().splitlines() if ln])

    # Files changed on main since base
    main_files: set[str] = set()
    if base_sha:
        main_diff = subprocess.run(
            ["git", "-C", project_root, "diff", "--name-only", f"{base_sha}..HEAD"],
            capture_output=True,
            text=True,
        )
        if main_diff.returncode == 0:
            main_files = set(main_diff.stdout.strip().splitlines())

    # Files changed on worker branch
    worker_files: set[str] = set()
    if wt and Path(wt).exists() and base_sha:
        worker_diff = subprocess.run(
            ["git", "-C", wt, "diff", "--name-only", f"{base_sha}..HEAD"],
            capture_output=True,
            text=True,
        )
        if worker_diff.returncode == 0:
            worker_files = set(worker_diff.stdout.strip().splitlines())

    overlap = sorted(main_files & worker_files)

    # Classification
    if overlap and (commits_since > 5 or age_hours > 12):
        freshness = "stale"
    elif overlap or commits_since > 0 or age_hours > 4:
        freshness = "warn"
    else:
        freshness = "fresh"

    return {
        "freshness": freshness,
        "commits_since_base": commits_since,
        "overlapping_files": overlap,
        "pane_age_hours": round(age_hours, 1),
    }


# -- Concurrency guard --


def _count_active_agent_workers(session_root: str, agent: str) -> int:
    """Count how many workers for *agent* are currently alive."""
    panes = _all_panes(session_root)
    all_tmux = get_backend().bulk_info()
    count = 0
    for p in panes:
        if p.get("agent") == agent:
            pane_id = p.get("pane_id", "")
            if pane_id and pane_id in all_tmux:
                count += 1
    return count


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
    if agent_def:
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
        if agent_def and agent_def.health_check:
            hc = subprocess.run(agent_def.health_check, shell=True, capture_output=True, text=True)
            if hc.returncode != 0 and agent_def.health_fix:
                subprocess.run(agent_def.health_fix, shell=True, capture_output=True, text=True)
                hc = subprocess.run(
                    agent_def.health_check, shell=True, capture_output=True, text=True
                )
            if hc.returncode != 0:
                raise RuntimeError(f"Health check failed for {agent}: {agent_def.health_check}")

        # 2c. Generic concurrency guard (config-driven)
        if agent_def and agent_def.max_concurrent is not None:
            active = _count_active_agent_workers(session_root, agent)
            if active >= agent_def.max_concurrent:
                raise RuntimeError(
                    f"Concurrency limit: {active} {agent} workers already running "
                    f"(max {agent_def.max_concurrent}). "
                    f"Wait for one to finish or use a different agent."
                )

        startup_env = {
            "DISABLE_AUTO_UPDATE": "true",
            "DISABLE_UPDATE_PROMPT": "true",
        }

        # 3. Split tmux pane
        get_backend().setup_pane_borders()
        pane_id = get_backend().create_pane(cwd=worktree_path, env=startup_env)

        # Let the login shell finish startup before injecting commands.
        time.sleep(0.25)

        # 4. Lock pane title (prevent agent/tmux from overwriting)
        get_backend().set_pane_option(pane_id, "allow-rename", "off")
        get_backend().set_pane_option(pane_id, "automatic-rename", "off")
        title = _build_pane_title(slug, project_root)
        get_backend().set_title(pane_id, title)
        agent_color = agent_def.color if agent_def else None
        get_backend().style(pane_id, agent, color=agent_color)
        get_backend().set_pane_option(pane_id, "allow-set-title", "off")

        # 5. Tidy layout
        get_backend().select_layout("tiled")

        # 6. Clear CLAUDECODE recursion guard (inherited from parent claude session)
        get_backend().send_input(pane_id, "unset CLAUDECODE")

        # 6a. Start persistent logging via tmux pipe-pane
        logs_dir = Path(session_root) / _STATE_DIR / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_file = str(logs_dir / f"{slug}.log")
        get_backend().start_logging(pane_id, log_file)

        # 6b. Inject env vars
        for key, val in all_env.items():
            get_backend().send_input(pane_id, f"export {key}={val!r}")

        # 7. Trigger worktree_created hook
        hook_env = {
            "DGOV_ROOT": project_root,
            "DGOV_PANE_ID": pane_id,
            "DGOV_SLUG": slug,
            "DGOV_PROMPT": prompt,
            "DGOV_AGENT": agent,
            "DGOV_WORKTREE_PATH": worktree_path,
            "DGOV_BRANCH": branch_name,
            "DGOV_OWNS_WORKTREE": "1" if owns_worktree else "0",
        }
        hook_ran = _trigger_hook("worktree_created", project_root, hook_env)

        if agent == "pi" and not skip_auto_structure:
            prompt = _structure_pi_prompt(prompt)

        # 8. Rewrite absolute paths in prompt so agent edits worktree, not main repo
        rewritten_prompt = prompt.replace(project_root, worktree_path)

        # 8b. Fallback protected-file warning if hook didn't write CLAUDE.md
        if not hook_ran:
            protected_warning = (
                "\n\nIMPORTANT: Do NOT modify or overwrite these files: "
                + ", ".join(sorted(_PROTECTED_FILES))
                + ". Do NOT create new documentation files."
            )
            if protected_warning.strip() not in rewritten_prompt:
                rewritten_prompt += protected_warning

        # 9. Build done-signal path
        done_signal = str(Path(session_root) / _STATE_DIR / "done" / slug)
        Path(done_signal).parent.mkdir(parents=True, exist_ok=True)

        # 10. Launch agent (with done-signal wrapper)
        if agent_def:
            if agent_def.prompt_transport == "send-keys":
                base_cmd = build_launch_command(
                    agent,
                    None,
                    permission_mode,
                    project_root=worktree_path,
                    slug=slug,
                    extra_flags=extra_flags,
                    registry=registry,
                )
                wrapped_cmd = _wrap_done_signal(base_cmd, done_signal)
                get_backend().send_input(pane_id, wrapped_cmd)
                if agent_def.send_keys_ready_delay_ms > 0:
                    time.sleep(agent_def.send_keys_ready_delay_ms / 1000)
                for key in agent_def.send_keys_pre_prompt:
                    get_backend().send_keys(pane_id, [key])
                get_backend().send_prompt_via_buffer(pane_id, rewritten_prompt)
            else:
                launch_cmd = build_launch_command(
                    agent,
                    rewritten_prompt,
                    permission_mode,
                    project_root=worktree_path,
                    slug=slug,
                    extra_flags=extra_flags,
                    registry=registry,
                )
                wrapped_cmd = _wrap_done_signal(launch_cmd, done_signal)
                get_backend().send_input(pane_id, wrapped_cmd)

        # 10b. Set tmux pane title
        title = _build_pane_title(slug, project_root)
        get_backend().set_title(pane_id, title)
        get_backend().style(pane_id, agent, color=agent_color)
        get_backend().set_pane_option(pane_id, "allow-set-title", "off")

        # 11. Build pane record and save to state
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
        _add_pane(session_root, pane)

        _emit_event(
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
    done_path = Path(session_root) / _STATE_DIR / "done" / slug
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
                ["git", "-C", wt, "checkout", "."],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", project_root, "worktree", "remove", "--force", wt],
                capture_output=True,
            )
            if branch:
                subprocess.run(
                    ["git", "-C", project_root, "branch", "-D", branch],
                    capture_output=True,
                )
        if not skipped_worktree:
            subprocess.run(
                ["git", "-C", project_root, "worktree", "prune"],
                capture_output=True,
            )

    get_backend().select_layout("tiled")

    # 4. Remove from dgov state (after tmux kill and worktree removal)
    if not skipped_worktree:
        _remove_pane(session_root, slug)

    return {"cleaned": True, "skipped_worktree": skipped_worktree}


def close_worker_pane(
    project_root: str, slug: str, session_root: str | None = None, *, force: bool = False
) -> bool:
    """Close a worker pane: kill tmux pane, remove worktree, update state."""
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    target = _get_pane(session_root, slug)

    if not target:
        return False

    _update_pane_state(session_root, slug, "closed")
    _emit_event(session_root, "pane_closed", slug)
    _full_cleanup(
        project_root,
        session_root,
        slug,
        target,
        skip_worktree_if_dirty=not force,
    )
    return True


def list_worker_panes(project_root: str, session_root: str | None = None) -> list[dict]:
    """List worker panes with live status from tmux."""
    session_root = os.path.abspath(session_root or project_root)
    panes = _all_panes(session_root)
    all_tmux = get_backend().bulk_info()
    result = []
    for p in panes:
        pane_id = p.get("pane_id", "")
        slug = p["slug"]
        state = p.get("state") or "active"
        alive = pane_id in all_tmux if pane_id else False
        cmd = all_tmux.get(pane_id, {}).get("current_command", "") if alive else ""
        done = state != "active"
        if state == "active":
            done = _is_done(session_root, slug, pane_record=p)
            if done:
                # _is_done updated persistent state; reconcile local copy
                updated = _get_pane(session_root, slug)
                if updated:
                    state = updated.get("state", state)
        freshness = _compute_freshness(project_root, p)
        entry: dict = {
            "slug": slug,
            "agent": p.get("agent"),
            "pane_id": pane_id,
            "alive": alive,
            "done": done,
            "state": state,
            "current_command": cmd,
            "worktree_path": p.get("worktree_path"),
            "branch": p.get("branch_name"),
            "prompt": p.get("prompt", "")[:80],
            "duration_s": round(time.time() - (p.get("created_at") or time.time())),
            **freshness,
        }
        result.append(entry)

    # Deduplicate by slug: prefer alive entry, then latest (last in list)
    seen: dict[str, int] = {}
    for i, entry in enumerate(result):
        slug = entry["slug"]
        if slug not in seen:
            seen[slug] = i
        else:
            prev = result[seen[slug]]
            # Prefer alive over dead; if both same liveness, keep latest
            if entry["alive"] and not prev["alive"]:
                seen[slug] = i
            elif entry["alive"] == prev["alive"]:
                seen[slug] = i  # latest wins
    return [result[i] for i in sorted(seen.values())]


def prune_stale_panes(project_root: str, session_root: str | None = None) -> list[str]:
    """Remove state entries for panes that are dead and have no worktree.

    Also removes orphaned worktree directories in ``.dgov/worktrees/`` that
    have no matching pane entry in state (e.g. left behind after ``pane close``
    skipped a dirty worktree).
    """
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)
    panes = _all_panes(session_root)
    pruned = []

    # Pass 1: prune stale state entries (existing behaviour)
    for p in panes:
        pane_id = p.get("pane_id", "")
        slug = p["slug"]
        alive = get_backend().is_alive(pane_id) if pane_id else False
        wt = p.get("worktree_path", "")
        wt_exists = bool(wt) and Path(wt).exists()
        if not alive and not wt_exists:
            _remove_pane(session_root, slug)
            done_path = Path(session_root) / _STATE_DIR / "done" / slug
            done_path.unlink(missing_ok=True)
            pruned.append(slug)

    # Pass 2: remove orphaned worktree dirs with no matching pane entry
    worktrees_dir = Path(project_root) / _STATE_DIR / "worktrees"
    if worktrees_dir.is_dir():
        # Re-read state after pass 1 removals
        remaining_panes = _all_panes(session_root)
        known_worktrees = {p.get("worktree_path") for p in remaining_panes}
        for entry in worktrees_dir.iterdir():
            if not entry.is_dir():
                continue
            entry_str = str(entry)
            if entry_str in known_worktrees:
                continue
            # Orphan — no pane entry references this dir.
            # Only remove if there's no live tmux pane using it.
            branch_name = entry.name
            _remove_worktree(project_root, entry_str, branch_name)
            pruned.append(f"orphan:{branch_name}")

    return pruned


def capture_worker_output(
    project_root: str, slug: str, lines: int = 30, session_root: str | None = None
) -> str | None:
    """Capture the last N lines of a worker pane's output."""
    session_root = os.path.abspath(session_root or project_root)
    target = _get_pane(session_root, slug)

    if not target or not target.get("pane_id"):
        return None

    pane_id = target["pane_id"]
    if not get_backend().is_alive(pane_id):
        return None

    return get_backend().capture_output(pane_id, lines)


def review_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    full: bool = False,
) -> dict:
    """Preview a worker pane's changes before merging.

    Returns diff stat, protected file status, commit log, and safe-to-merge verdict.
    With ``full=True``, includes the complete diff.
    """
    session_root = os.path.abspath(session_root or project_root)
    target = _get_pane(session_root, slug)
    if not target:
        return {"error": f"Pane not found: {slug}"}

    wt = target.get("worktree_path", "")
    branch = target.get("branch_name", "")
    base_sha = target.get("base_sha", "")

    if not wt or not Path(wt).exists():
        return {"error": f"Worktree not found: {wt}"}
    if not base_sha:
        return {"error": "No base_sha recorded — cannot compute diff"}

    # Diff stat
    stat_result = subprocess.run(
        ["git", "-C", wt, "diff", "--stat", f"{base_sha}..HEAD"],
        capture_output=True,
        text=True,
    )
    stat = stat_result.stdout.strip() if stat_result.returncode == 0 else ""

    # Changed files (for protected check)
    names_result = subprocess.run(
        ["git", "-C", wt, "diff", "--name-only", f"{base_sha}..HEAD"],
        capture_output=True,
        text=True,
    )
    changed_files = (
        set(names_result.stdout.strip().splitlines()) if names_result.returncode == 0 else set()
    )
    protected_touched = sorted(changed_files & _PROTECTED_FILES)

    # Commit log
    log_result = subprocess.run(
        ["git", "-C", wt, "log", "--oneline", f"{base_sha}..HEAD"],
        capture_output=True,
        text=True,
    )
    commit_log = log_result.stdout.strip() if log_result.returncode == 0 else ""
    commit_count = len(commit_log.splitlines()) if commit_log else 0

    # Uncommitted changes
    porcelain = subprocess.run(
        ["git", "-C", wt, "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    # Filter out CLAUDE.md files — always modified by worktree hook, not by worker
    porcelain_lines = [
        ln
        for ln in porcelain.stdout.strip().splitlines()
        if not ln.lstrip(" MAD??").lstrip().startswith("CLAUDE.md")
    ]
    uncommitted = bool(porcelain_lines)

    # Verdict
    issues = []
    if protected_touched:
        issues.append(f"protected files touched: {protected_touched}")
    if uncommitted:
        issues.append("uncommitted changes (will be auto-committed on merge)")
    if commit_count == 0:
        issues.append("no commits — nothing to merge")

    verdict = "safe" if not issues else "review"

    if verdict == "safe":
        _emit_event(session_root, "review_pass", slug)
    else:
        _emit_event(session_root, "review_fail", slug, issues=issues)

    freshness = _compute_freshness(project_root, target)

    # Retry count from events
    from dgov.retry import _count_retries

    retry_count = _count_retries(session_root, slug)

    # Count auto-responses from event journal
    auto_respond_count = _count_auto_responses(session_root, slug)

    result = {
        "slug": slug,
        "branch": branch,
        "stat": stat,
        "protected_touched": protected_touched,
        "verdict": verdict,
        "commit_count": commit_count,
        "commit_log": commit_log,
        "uncommitted": uncommitted,
        "files_changed": len(changed_files),
        "retry_count": retry_count,
        "auto_responses": auto_respond_count,
        **freshness,
    }
    if issues:
        result["issues"] = issues
    if full:
        diff_result = subprocess.run(
            ["git", "-C", wt, "diff", f"{base_sha}..HEAD"],
            capture_output=True,
            text=True,
        )
        result["diff"] = diff_result.stdout if diff_result.returncode == 0 else ""

    return result


def diff_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    stat: bool = False,
    name_only: bool = False,
) -> dict:
    """Get the diff for a worker pane's branch vs its base_sha."""
    session_root = os.path.abspath(session_root or project_root)
    target = _get_pane(session_root, slug)
    if not target:
        return {"error": f"Pane not found: {slug}"}

    wt = target.get("worktree_path", "")
    base_sha = target.get("base_sha", "")
    if not wt or not Path(wt).exists():
        return {"error": f"Worktree not found: {wt}"}
    if not base_sha:
        return {"error": "No base_sha recorded"}

    cmd = ["git", "-C", wt, "diff", f"{base_sha}..HEAD"]
    if stat:
        cmd.append("--stat")
    elif name_only:
        cmd.append("--name-only")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"error": result.stderr.strip()}

    return {"slug": slug, "base_sha": base_sha, "diff": result.stdout}


def rebase_governor(project_root: str, onto: str | None = None) -> dict:
    """Rebase the current worktree onto a base branch.

    Args:
        project_root: Git repo (worktree) to rebase.
        onto: Explicit base branch. Auto-detects from upstream if None.

    Stashes dirty changes, rebases, and pops stash on success.
    On conflict: aborts rebase, pops stash, returns error.
    """
    project_root = os.path.abspath(project_root)

    # Detect base branch
    if onto:
        base = onto
    else:
        upstream = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--abbrev-ref", "@{upstream}"],
            capture_output=True,
            text=True,
        )
        if upstream.returncode == 0 and upstream.stdout.strip():
            base = upstream.stdout.strip().split("/", 1)[-1]  # origin/main -> main
        else:
            base = "main"

    # Stash if dirty
    status = subprocess.run(
        ["git", "-C", project_root, "status", "--porcelain"],
        capture_output=True,
        text=True,
    )
    dirty = bool(status.stdout.strip())
    stashed = False
    if dirty:
        stash = subprocess.run(
            ["git", "-C", project_root, "stash", "push", "-m", "dgov-rebase-auto"],
            capture_output=True,
            text=True,
        )
        stashed = stash.returncode == 0

    # Fetch to ensure we have latest refs
    subprocess.run(
        ["git", "-C", project_root, "fetch", "--quiet"],
        capture_output=True,
        timeout=30,
    )

    # Rebase
    rebase = subprocess.run(
        ["git", "-C", project_root, "rebase", base],
        capture_output=True,
        text=True,
    )

    if rebase.returncode != 0:
        # Abort rebase
        subprocess.run(
            ["git", "-C", project_root, "rebase", "--abort"],
            capture_output=True,
        )
        # Pop stash if we stashed
        if stashed:
            subprocess.run(
                ["git", "-C", project_root, "stash", "pop"],
                capture_output=True,
            )
        return {
            "rebased": False,
            "base": base,
            "stashed": stashed,
            "error": rebase.stderr.strip() or "Rebase failed with conflicts",
        }

    # Pop stash on success
    if stashed:
        pop = subprocess.run(
            ["git", "-C", project_root, "stash", "pop"],
            capture_output=True,
            text=True,
        )
        if pop.returncode != 0:
            return {
                "rebased": True,
                "base": base,
                "stashed": True,
                "warning": "Rebase succeeded but stash pop had conflicts",
            }

    return {"rebased": True, "base": base, "stashed": stashed}


def escalate_worker_pane(
    project_root: str,
    slug: str,
    target_agent: str = "claude",
    session_root: str | None = None,
    permission_mode: str = "bypassPermissions",
) -> dict:
    """Escalate a worker pane to a different agent.

    Closes the existing pane and relaunches with ``target_agent``
    using the same prompt. Returns the new pane info.
    """
    session_root = os.path.abspath(session_root or project_root)
    target = _get_pane(session_root, slug)

    if not target:
        return {"error": f"Pane not found: {slug}"}

    original_prompt = target.get("prompt", "")
    if not original_prompt:
        return {"error": f"No prompt recorded for {slug}"}

    original_agent = target.get("agent", "unknown")

    # Create the new pane first, then close the old one
    new_slug = f"{slug}-esc"
    try:
        new_pane = create_worker_pane(
            project_root=project_root,
            prompt=original_prompt,
            agent=target_agent,
            permission_mode=permission_mode,
            slug=new_slug,
            session_root=session_root,
        )
    except Exception as e:
        return {"error": str(e)}

    # Mark old pane as escalated then close
    _update_pane_state(session_root, slug, "escalated")
    _emit_event(session_root, "pane_escalated", slug, new_slug=new_slug, target_agent=target_agent)
    close_worker_pane(project_root, slug, session_root=session_root)

    return {
        "escalated": True,
        "original_slug": slug,
        "original_agent": original_agent,
        "new_slug": new_pane.slug,
        "agent": target_agent,
        "pane_id": new_pane.pane_id,
        "worktree": new_pane.worktree_path,
    }


def retry_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    agent: str | None = None,
    prompt: str | None = None,
    permission_mode: str = "acceptEdits",
) -> dict:
    """Retry a pane by creating a new one linked to the original.

    Reads original pane record (prompt, agent, base_sha), computes a new
    slug ``<original-base>-<attempt+1>``, creates a new worktree + branch +
    pane via the normal create path, then cross-links the old and new records.
    """
    session_root = os.path.abspath(session_root or project_root)
    target = _get_pane(session_root, slug)
    if not target:
        return {"error": f"Pane not found: {slug}"}

    original_prompt = prompt or target.get("prompt", "")
    original_agent = agent or target.get("agent", "claude")

    # Compute attempt number from slug pattern
    base_slug = re.sub(r"-\d+$", "", slug)  # strip trailing -N
    attempt = 1
    existing = _all_panes(session_root)
    for p in existing:
        m = re.match(rf"^{re.escape(base_slug)}-(\d+)$", p.get("slug", ""))
        if m:
            attempt = max(attempt, int(m.group(1)))
    attempt += 1
    new_slug = f"{base_slug}-{attempt}"

    # Create new pane
    try:
        new_pane = create_worker_pane(
            project_root=project_root,
            prompt=original_prompt,
            agent=original_agent,
            permission_mode=permission_mode,
            slug=new_slug,
            session_root=session_root,
        )
    except Exception as e:
        return {"error": str(e)}

    # Link records via SQLite metadata
    _set_pane_metadata(session_root, new_slug, retried_from=slug)
    _set_pane_metadata(session_root, slug, superseded_by=new_slug)
    _update_pane_state(session_root, slug, "superseded", force=True)

    # Emit events
    _emit_event(session_root, "pane_retry_spawned", slug, new_slug=new_slug, attempt=attempt)
    _emit_event(session_root, "pane_retry_spawned", new_slug, retried_from=slug, attempt=attempt)
    _emit_event(session_root, "pane_superseded", slug, superseded_by=new_slug)

    return {
        "retried": True,
        "original_slug": slug,
        "new_slug": new_pane.slug,
        "agent": original_agent,
        "attempt": attempt,
        "pane_id": new_pane.pane_id,
    }


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
    session_root = os.path.abspath(session_root or project_root)
    target = _get_pane(session_root, slug)

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

    # Health check (config-driven)
    if agent_def and agent_def.health_check:
        hc = subprocess.run(agent_def.health_check, shell=True, capture_output=True, text=True)
        if hc.returncode != 0 and agent_def.health_fix:
            subprocess.run(agent_def.health_fix, shell=True, capture_output=True, text=True)
            hc = subprocess.run(agent_def.health_check, shell=True, capture_output=True, text=True)
        if hc.returncode != 0:
            return {"error": f"Health check failed for {resume_agent}: {agent_def.health_check}"}

    # Concurrency guard (config-driven)
    if agent_def and agent_def.max_concurrent is not None:
        active = _count_active_agent_workers(session_root, resume_agent)
        if active >= agent_def.max_concurrent:
            return {
                "error": f"Concurrency limit: {active} {resume_agent} workers "
                f"running (max {agent_def.max_concurrent})"
            }

    resume_context = (
        "\n\nYou are RESUMING a previous session in this worktree. "
        "Run 'git status' and 'git log --oneline -5' first to see what has "
        "already been done. Continue from where the previous agent left off. "
        "Do NOT redo work that is already committed."
    )
    full_prompt = original_prompt + resume_context

    if resume_agent == "pi":
        full_prompt = _structure_pi_prompt(full_prompt)

    # Rewrite paths
    rewritten_prompt = full_prompt.replace(project_root, worktree_path)

    startup_env = {
        "DISABLE_AUTO_UPDATE": "true",
        "DISABLE_UPDATE_PROMPT": "true",
    }

    # Create new tmux pane
    get_backend().setup_pane_borders()
    pane_id = get_backend().create_pane(cwd=worktree_path, env=startup_env)

    # Let the login shell finish startup before injecting commands.
    time.sleep(0.25)

    get_backend().set_pane_option(pane_id, "allow-rename", "off")
    get_backend().set_pane_option(pane_id, "automatic-rename", "off")
    title = _build_pane_title(slug, project_root)
    get_backend().set_title(pane_id, title)
    agent_color = agent_def.color if agent_def else None
    get_backend().style(pane_id, resume_agent, color=agent_color)
    get_backend().set_pane_option(pane_id, "allow-set-title", "off")
    get_backend().select_layout("tiled")

    # Clear recursion guard + inject env
    get_backend().send_input(pane_id, "unset CLAUDECODE")

    # Inject agent config env vars
    if agent_def and agent_def.env:
        for key, val in agent_def.env.items():
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                get_backend().send_input(pane_id, f"export {key}={val!r}")

    # Start persistent logging via tmux pipe-pane
    logs_dir = Path(session_root) / _STATE_DIR / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(logs_dir / f"{slug}.log")
    get_backend().start_logging(pane_id, log_file)

    # Trigger worktree_created hook
    hook_env = {
        "DGOV_ROOT": project_root,
        "DGOV_PANE_ID": pane_id,
        "DGOV_SLUG": slug,
        "DGOV_PROMPT": original_prompt,
        "DGOV_AGENT": resume_agent,
        "DGOV_WORKTREE_PATH": worktree_path,
        "DGOV_BRANCH": branch_name,
        "DGOV_OWNS_WORKTREE": "1",
    }
    hook_ran = _trigger_hook("worktree_created", project_root, hook_env)

    if not hook_ran:
        protected_warning = (
            "\n\nIMPORTANT: Do NOT modify or overwrite these files: "
            + ", ".join(sorted(_PROTECTED_FILES))
            + ". Do NOT create new documentation files."
        )
        if protected_warning.strip() not in rewritten_prompt:
            rewritten_prompt += protected_warning

    # Build done signal
    done_signal = str(Path(session_root) / _STATE_DIR / "done" / slug)
    Path(done_signal).parent.mkdir(parents=True, exist_ok=True)
    # Clear old done signal if it exists
    Path(done_signal).unlink(missing_ok=True)

    # Launch agent
    if agent_def:
        if agent_def.prompt_transport == "send-keys":
            base_cmd = build_launch_command(
                resume_agent,
                None,
                permission_mode,
                project_root=worktree_path,
                slug=slug,
                extra_flags="",
                registry=registry,
            )
            wrapped_cmd = _wrap_done_signal(base_cmd, done_signal)
            get_backend().send_input(pane_id, wrapped_cmd)
            if agent_def.send_keys_ready_delay_ms > 0:
                time.sleep(agent_def.send_keys_ready_delay_ms / 1000)
            for key in agent_def.send_keys_pre_prompt:
                get_backend().send_keys(pane_id, [key])
            get_backend().send_prompt_via_buffer(pane_id, rewritten_prompt)
        else:
            launch_cmd = build_launch_command(
                resume_agent,
                rewritten_prompt,
                permission_mode,
                project_root=worktree_path,
                slug=slug,
                extra_flags="",
                registry=registry,
            )
            wrapped_cmd = _wrap_done_signal(launch_cmd, done_signal)
            get_backend().send_input(pane_id, wrapped_cmd)

    # Update state: new pane_id, back to active
    conn = _get_db(session_root)
    try:
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
    finally:
        conn.close()

    _emit_event(session_root, "pane_resumed", slug, agent=resume_agent)

    return {
        "resumed": True,
        "slug": slug,
        "agent": resume_agent,
        "pane_id": pane_id,
        "worktree": worktree_path,
    }
