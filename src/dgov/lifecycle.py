"""Pane lifecycle: create, close, resume, and cleanup."""

from __future__ import annotations

import fcntl
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

from dgov.agents import AgentDef, build_launch_command, load_registry
from dgov.backend import get_backend
from dgov.context_packet import ContextPacket, build_context_packet, render_start_here_section
from dgov.done import _wrap_done_signal, _wrap_exit_signal
from dgov.gitops import _remove_worktree
from dgov.persistence import (
    STATE_DIR,
    WorkerPane,
    _get_db,
    _retry_on_lock,
    add_pane,
    clear_preserved_artifacts,
    emit_event,
    get_child_panes,
    get_pane,
    mark_preserved_artifacts,
    remove_pane,
    update_pane_state,
)
from dgov.strategy import (
    _generate_slug,
    _structure_pi_prompt,
    _validate_slug,
)

logger = logging.getLogger(__name__)


def _refresh_codebase_md(project_root: str) -> None:
    """Regenerate CODEBASE.md if stale (older than HEAD commit timestamp).

    Called before every dispatch so workers always get a fresh codebase map.
    Skips regeneration if CODEBASE.md is newer than the last commit.
    """
    codebase = Path(project_root) / "CODEBASE.md"
    try:
        # Get HEAD commit timestamp
        result = subprocess.run(
            ["git", "-C", project_root, "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return
        head_ts = float(result.stdout.strip())

        # Skip if CODEBASE.md exists and is newer than HEAD
        if codebase.exists() and codebase.stat().st_mtime >= head_ts:
            return

        from dgov.cli.admin import regenerate_codebase_md

        regenerate_codebase_md(project_root)
        logger.debug("Refreshed CODEBASE.md (stale)")
    except Exception:
        logger.debug("CODEBASE.md refresh failed", exc_info=True)


@contextmanager
def _pane_lock(project_root: str):
    """File-based lock to serialize pane creation on the same repo."""
    lock_dir = Path(project_root) / ".dgov"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "pane.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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


def _process_snapshot() -> dict[int, tuple[int, int]]:
    """Return {pid: (ppid, pgid)} from the current process table."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid="],
        capture_output=True,
        text=True,
        check=True,
    )
    snapshot: dict[int, tuple[int, int]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            pid, ppid, pgid = (int(part) for part in parts)
        except ValueError:
            continue
        snapshot[pid] = (ppid, pgid)
    return snapshot


def _collect_descendant_pids(root_pid: int, snapshot: dict[int, tuple[int, int]]) -> set[int]:
    """Collect root_pid and all descendants from a pid/ppid snapshot."""
    children_by_parent: dict[int, list[int]] = {}
    for pid, (ppid, _pgid) in snapshot.items():
        children_by_parent.setdefault(ppid, []).append(pid)

    descendants: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        stack.extend(children_by_parent.get(pid, []))
    return descendants


def _terminate_pane_process_tree(root_pid: int, wait_timeout: float = 5.0) -> dict:
    """Terminate the pane process group and any descendant process groups.

    Required behavior:
    1. Send SIGTERM once per discovered process group
    2. Wait bounded time for processes to exit
    3. Return terminated=True when descendants are gone
    4. Only escalate to SIGKILL for process groups that still have live descendants after the wait
    5. After SIGKILL, re-check and return terminated=True only when nothing survives
    6. Catch killpg errors instead of raising

    Args:
        root_pid: PID to terminate (will kill all descendants).
        wait_timeout: Seconds to wait for processes to exit before returning.

    Returns:
        {"terminated": bool, "still_running": list[int] or []}.
    """
    import time

    try:
        snapshot = _process_snapshot()
    except (subprocess.SubprocessError, OSError):
        snapshot = {}

    descendant_pids = _collect_descendant_pids(root_pid, snapshot) if snapshot else {root_pid}
    pgids = {
        pgid
        for pid in descendant_pids
        if (entry := snapshot.get(pid)) is not None
        for pgid in [entry[1]]
        if pgid > 0
    }
    if not pgids:
        try:
            pgids = {os.getpgid(root_pid)}
        except (ProcessLookupError, PermissionError):
            return {"terminated": False, "still_running": []}

    # Step 1: Send SIGTERM once per discovered process group
    for pgid in sorted(pgids, reverse=True):
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue

    # Step 2: Wait bounded time for processes to exit
    deadline = time.time() + wait_timeout
    snapshot_after = _process_snapshot() if snapshot else {}

    while time.time() < deadline:
        still_alive_pids: set[int] = set()
        for pid in descendant_pids:
            try:
                os.kill(pid, 0)
                still_alive_pids.add(pid)
            except OSError:
                pass
        if not still_alive_pids:
            return {"terminated": True, "still_running": []}
        # Re-resolve descendants since PIDs may have changed
        try:
            snapshot_after = _process_snapshot()
        except (subprocess.SubprocessError, OSError):
            snapshot_after = {}
        descendant_pids = _collect_descendant_pids(root_pid, snapshot_after)
        time.sleep(0.25)

    # Step 3-4: SIGTERM didn't kill everything — check for still-living descendants
    still_alive: list[int] = []
    for pid in descendant_pids:
        try:
            os.kill(pid, 0)
            still_alive.append(pid)
        except OSError:
            pass

    if not still_alive:
        return {"terminated": True, "still_running": []}

    # Step 4: Only escalate to SIGKILL for process groups that have live descendants
    still_pgids = {
        pgid
        for pid in still_alive
        if (entry := snapshot_after.get(pid)) is not None
        for pgid in [entry[1]]
        if pgid > 0
    }
    # Fallback to root process group if snapshots fail or no pgids found
    if not still_pgids:
        try:
            still_pgids = {os.getpgid(root_pid)}
        except (ProcessLookupError, PermissionError):
            pass

    for pgid in sorted(still_pgids, reverse=True):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue

    # Step 5: Brief wait after SIGKILL, then re-check
    time.sleep(0.5)

    final_alive = []
    for pid in still_alive:
        try:
            os.kill(pid, 0)
            final_alive.append(pid)
        except OSError:
            pass

    if final_alive:
        return {"terminated": False, "still_running": final_alive}
    else:
        return {"terminated": True, "still_running": []}


# -- Git worktree helpers --


def _create_worktree(project_root: str, worktree_path: str, branch_name: str) -> None:
    # Reject stale worktree reuse on fresh create.
    if Path(worktree_path).is_dir():
        git_check = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
        )
        if git_check.returncode == 0:
            raise RuntimeError(
                f"Worktree already exists at {worktree_path!r}. "
                f"Use a new slug or explicit recovery path."
            )

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
            f"at path {worktree_path!r}: {e.stderr}"
        ) from e


def _find_unique_slug(project_root: str, session_root: str, base_slug: str) -> tuple[str, str]:
    """Find a unique slug by appending numeric suffix if needed.

    Checks for collisions with existing pane slugs, branch names, and
    worktree paths. Returns (unique_slug, worktree_path).
    """

    from dgov.persistence import all_panes, get_slug_history

    # Get existing panes and their branches/worktrees
    existing_panes = all_panes(session_root)
    existing_slugs = {p["slug"] for p in existing_panes}
    # Also include historical slugs (closed/removed panes)
    historical_slugs = get_slug_history(session_root)
    existing_slugs.update(historical_slugs)
    existing_branches = {p["branch_name"] for p in existing_panes if p.get("branch_name")}
    existing_worktrees = {p["worktree_path"] for p in existing_panes if p.get("worktree_path")}

    # Base path pattern
    worktrees_dir = Path(project_root) / ".dgov" / "worktrees"
    candidate_slug = base_slug
    counter = 1

    while True:
        # Check slug collision
        if candidate_slug in existing_slugs:
            candidate_slug = f"{base_slug}-{counter}"
            counter += 1
            continue

        # Check branch name collision
        branch_name = candidate_slug  # branch name matches slug
        if branch_name in existing_branches:
            candidate_slug = f"{base_slug}-{counter}"
            counter += 1
            continue
        git_branch = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--verify", branch_name],
            capture_output=True,
            text=True,
        )
        if git_branch.returncode == 0:
            candidate_slug = f"{base_slug}-{counter}"
            counter += 1
            continue

        # Check worktree path collision
        worktree_path = str(worktrees_dir / candidate_slug)
        if worktree_path in existing_worktrees or Path(worktree_path).exists():
            candidate_slug = f"{base_slug}-{counter}"
            counter += 1
            continue

        # All clear
        return candidate_slug, worktree_path


# -- Hook trigger --


def _write_worktree_instructions(
    worktree_path: str,
    slug: str,
    role: str,
    prompt: str | None = None,
    context_packet: ContextPacket | None = None,
) -> None:
    """Write role-appropriate instructions to .dgov/DGOV_WORKER_INSTRUCTIONS.md.

    Generated files contain only the role-specific preamble plus any
    start-here/codebase hints. Do not inherit the governor/repo CLAUDE.md body.
    Writes to worktree-local path .dgov/DGOV_WORKER_INSTRUCTIONS.md which is
    excluded via worktree's git exclude file so it cannot be staged by commits.
    """
    wt = Path(worktree_path)
    instructions_file = wt / ".dgov" / "DGOV_WORKER_INSTRUCTIONS.md"
    system_prompt_file = wt / ".dgov" / "DGOV_SYSTEM_PROMPT.md"

    # Ensure .dgov directory exists in worktree
    instructions_file.parent.mkdir(parents=True, exist_ok=True)

    # Codebase map reference
    codebase_hint = ""
    if (wt / "CODEBASE.md").exists():
        codebase_hint = "- Read CODEBASE.md for module map, task routing, and test mapping\n"

    # Extract routed task context if prompt provided
    packet = context_packet
    if packet is None and prompt:
        packet = build_context_packet(prompt)
    start_here_section = render_start_here_section(packet) if packet is not None else ""

    if role == "lt-gov":
        preamble = (
            f"# LT-GOV Instructions — {slug}\n\n"
            "You are a **lieutenant governor**. You orchestrate "
            "workers, you do NOT edit code.\n\n"
            "## Rules\n"
            "- Dispatch workers with: dgov pane create -a <agent> "
            f'-p "<task>" -r $DGOV_PROJECT_ROOT --parent {slug}\n'
            "- Wait: dgov pane wait <slug> -r $DGOV_PROJECT_ROOT\n"
            "- Review: dgov pane review <slug>\n"
            "- Close: dgov pane close <slug>\n"
            "- NEVER edit files directly\n"
            "- NEVER push to remote\n"
            "- Use logical agent names: qwen-9b, qwen-35b, qwen-122b\n"
        )
        # Inject Start here section before codebase hint when routed context exists
        if start_here_section:
            preamble += start_here_section + "\n"
        preamble += codebase_hint + "\n"
        preamble += f"## When done\nWrite status to .dgov/progress/{slug}.json and exit.\n"
    else:
        preamble = (
            f"# Worker Instructions — {slug}\n\n"
            "You are a **worker**. Complete the task, commit, "
            "and signal done.\n\n"
            "## Your tools\n"
            "You have these tools — USE THEM to do your work:\n"
            "- **Read** — read file contents before editing\n"
            "- **Edit** — make exact string replacements in files\n"
            "- **Write** — create new files (only when task requires it)\n"
            "- **Bash** — run shell commands (git, ruff, pytest, etc.)\n\n"
            "Do NOT just describe changes in text. "
            "You MUST call the Edit tool to modify files "
            "and the Bash tool to run commands.\n\n"
            "## Rules\n"
            "- BEFORE claiming a task is already done or needs no changes,\n"
            "  you MUST Read the target file and show its current content.\n"
            "  If you skip reading and guess, you WILL hallucinate.\n"
            "  Never trust your assumptions about file state.\n"
            "- ALWAYS Read source files FIRST — discover the real API.\n"
            "  The task prompt may have wrong function/class names.\n"
            "  Trust what you read in the code, not what the prompt says.\n"
            "- Edit ONLY the files specified in your task\n"
            "- Do NOT modify .gitignore, pyproject.toml\n"
            "- Do NOT create new files unless the task requires it\n"
            "- Do NOT push to remote\n"
            "- You are in a git worktree, not the main repo\n"
        )
        # Inject Start here section before codebase hint when routed context exists
        if start_here_section:
            preamble += start_here_section + "\n"
        preamble += codebase_hint + "\n"
        preamble += (
            "## Tooling\n"
            "- Lint: `uv run ruff check <file>` then `uv run ruff format <file>`\n"
            "- Test: `uv run pytest <test_file> -q -m unit`\n"
            "- Never run the full test suite — target specific test files\n\n"
            "## Testing rules\n"
            "- Every code change needs tests for new/changed behavior.\n"
            "- Test behavior, not implementation. Assert outputs and errors.\n"
            "- Use @pytest.mark.unit. Use tmp_path. Mock boundaries only.\n"
            "- Test edges: empty, None, zero, max size, error cases.\n"
            "- **When deleting or renaming code**: check .test-manifest.json\n"
            "  for affected test files, then grep those files for references\n"
            "  to deleted functions/classes/commands. Delete or update the tests.\n"
            "- **Run affected tests before committing**: if any fail, fix them.\n"
            "  Do NOT leave broken tests for the governor.\n\n"
            "## Commit checklist\n"
            "1. Run ruff check + format on changed files\n"
            "2. Check .test-manifest.json for test files related to your changes\n"
            "3. If you deleted/renamed any function, class, or CLI command:\n"
            "   grep the test files for references and fix/delete them\n"
            "4. Run affected tests: uv run pytest <test_files> -q -m unit\n"
            "5. Fix any test failures — do NOT commit with failing tests\n"
            "6. git add <all changed files including test files>\n"
            '7. git commit -m "<message>"\n'
            "8. Verify commit exists: git log --oneline $DGOV_BASE_SHA..HEAD\n"
            "9. Run `dgov worker complete` ONLY after step 8 succeeds\n"
            "10. If the task is already done or no changes are needed,\n"
            "    run `dgov worker complete -m 'already implemented'`\n"
        )

        system_prompt_content = preamble

        # Include the task prompt in the on-disk instructions file only.
        # Pi workers receive the task separately on the CLI, so duplicating it
        # in the injected system prompt wastes context window.
        if prompt:
            preamble += f"\n## Task\n\n{prompt}\n"
    if role == "lt-gov":
        system_prompt_content = preamble

    # Append CODEBASE.md content directly so workers/lt-govs have the full
    # module map, task routing table, and test mapping in their context window
    # without needing to follow a "read this file" hint.
    codebase_path = wt / "CODEBASE.md"
    codebase_content = ""
    if codebase_path.exists():
        try:
            codebase_content = codebase_path.read_text(encoding="utf-8")
        except OSError:
            pass

    content = preamble
    if codebase_content:
        content += f"\n## Codebase Map\n\n{codebase_content}\n"

    instructions_file.write_text(content, encoding="utf-8")
    system_prompt_file.write_text(system_prompt_content, encoding="utf-8")

    # Git-exclude generated instruction files so no `git add` can stage them
    _git_exclude_files(
        worktree_path,
        [".dgov/DGOV_WORKER_INSTRUCTIONS.md", ".dgov/DGOV_SYSTEM_PROMPT.md"],
    )


def _git_exclude_files(worktree_path: str, filenames: list[str]) -> None:
    """Add filenames to the worktree's git exclude (not .gitignore)."""
    result = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return
    git_dir = Path(result.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = Path(worktree_path) / git_dir
    exclude_file = git_dir / "info" / "exclude"
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_file.read_text() if exclude_file.exists() else ""
    existing_lines = set(existing.splitlines())
    to_add = [f for f in filenames if f not in existing_lines]
    if to_add:
        with exclude_file.open("a") as fh:
            for f in to_add:
                fh.write(f"{f}\n")


# -- Pane title --


def _state_icon(state: str | None) -> str:
    """Return the pane-title icon for a worker state."""
    return {
        "active": "~",
        "done": "ok",
        "merged": "+",
        "timed_out": "!",
        "failed": "X",
    }.get(state, "")


def _build_pane_title(
    agent: str,
    slug: str,
    project_root: str,
    *,
    state: str | None = None,
) -> str:
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
    extra_flags: str | None = None,
    owns_worktree: bool = True,
    base_sha: str | None = None,
    skip_auto_structure: bool = False,
    clear_done_signal: bool = False,
    role: str = "worker",
    context_packet: ContextPacket | None = None,
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

    # 4. Write role-appropriate CLAUDE.md into the worktree
    packet = context_packet or build_context_packet(prompt)
    _write_worktree_instructions(
        worktree_path,
        slug,
        role,
        prompt=prompt,
        context_packet=packet,
    )

    # 4b. Install pre-merge-commit hook to block branch integration in worktrees
    if owns_worktree:
        _install_worker_hooks(worktree_path)

    # 5. Auto-structure pi prompts
    if agent_id == "pi" and not skip_auto_structure:
        prompt = _structure_pi_prompt(
            prompt,
            list(packet.edit_files),
            commit_message=packet.commit_message,
        )

    # 6. Rewrite absolute paths so agent edits worktree, not main repo
    rewritten_prompt = re.sub(
        re.escape(project_root) + r"(?!/.dgov/worktrees/)", worktree_path, prompt
    )

    # 8. Build done-signal path — always clear stale signals from prior attempts
    done_signal = str(Path(session_root) / STATE_DIR / "done" / slug)
    Path(done_signal).parent.mkdir(parents=True, exist_ok=True)
    Path(done_signal).unlink(missing_ok=True)
    Path(done_signal + ".exit").unlink(missing_ok=True)

    # 9. Launch agent (with exit-only wrapper)
    # Workers always use headless mode; TUI interactive mode for LT-GOVs with real TUIs.
    # Codex "exec" is always one-shot (prompt on CLI), never TUI — force headless.
    is_cursor = agent_id in ("cursor", "cursor-auto") or agent_def.prompt_command == "cursor-agent"
    is_oneshot = agent_def.prompt_command == "codex"
    use_interactive = agent_def.interactive and role != "worker" and not is_oneshot

    # When forcing headless on a normally-interactive agent, add agent-specific flags
    if not use_interactive and agent_def.interactive and role == "worker":
        if agent_id == "claude":
            extra_flags = f"-p {extra_flags}".strip() if extra_flags else "-p"

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
        wrapped_cmd = _wrap_exit_signal(base_cmd, done_signal, worktree_path=worktree_path)
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
        wrapped_cmd = _wrap_exit_signal(base_cmd, done_signal, worktree_path=worktree_path)
        backend.send_shell_command(pane_id, wrapped_cmd)
        if agent_def.send_keys_ready_delay_ms > 0:
            time.sleep(agent_def.send_keys_ready_delay_ms / 1000)
        for key in agent_def.send_keys_pre_prompt:
            backend.send_keys(pane_id, [key])
        backend.send_prompt_via_buffer(pane_id, rewritten_prompt)
    else:
        # Headless workers with prompt embedded in launch command.
        # Use success-path wrapper so successful runs touch done file directly.
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
        wrapped_cmd = _wrap_done_signal(launch_cmd, done_signal, worktree_path=worktree_path)
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
    extra_flags: str | None = None,
    session_root: str | None = None,
    existing_worktree: str | None = None,
    skip_auto_structure: bool = False,
    role: str = "worker",
    parent_slug: str | None = None,
    context_packet: ContextPacket | None = None,
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
    base_slug = slug or _generate_slug(prompt)
    _validate_slug(base_slug)
    # Auto-detect role from routing tables when caller uses default "worker".
    if role == "worker":
        from dgov.router import resolve_role

        role = resolve_role(agent)
    # LT-GOVs don't edit code — run them on main, no worktree needed.
    if role == "lt-gov" and existing_worktree is None:
        existing_worktree = project_root
    owns_worktree = existing_worktree is None
    slug = base_slug
    worktree_path = os.path.abspath(existing_worktree) if existing_worktree else ""
    branch_name = "" if role == "lt-gov" else base_slug

    # 0. Validate env vars BEFORE any side effects
    all_env: dict[str, str] = {}

    # 0b. Auto-regenerate CODEBASE.md if stale (older than HEAD commit)
    _refresh_codebase_md(project_root)

    # 1. Capture base SHA (HEAD of project_root before worktree creation)
    base_sha_result = subprocess.run(
        ["git", "-C", project_root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    base_sha = base_sha_result.stdout.strip() if base_sha_result.returncode == 0 else None

    # Compute context packet early for overlay logic
    packet_for_overlay = context_packet or build_context_packet(prompt)

    # From here on, side effects need cleanup on failure
    pane_id: str | None = None
    try:
        # 2-5. Create the pane atomically under the repo lock.
        # This prevents a race where the monitor sees a newly-created worktree
        # before the pane is persisted and prunes it as an orphan.
        with _pane_lock(project_root):
            if owns_worktree:
                slug, worktree_path = _find_unique_slug(project_root, session_root, base_slug)
                branch_name = slug
                _create_worktree(project_root, worktree_path, branch_name)

                # Symlink .venv from main repo so workers skip uv sync
                _main_venv = Path(project_root) / ".venv"
                _wt_venv = Path(worktree_path) / ".venv"
                if _main_venv.is_dir() and not _wt_venv.exists():
                    _wt_venv.symlink_to(_main_venv)

                # Overlay dirty tracked files from governor repo into the new worktree
                _overlay_dirty_files(project_root, worktree_path, packet_for_overlay)

            # Re-resolve logical agent name -> physical backend after lock
            # River-first then OpenRouter fallback preserved by router order
            logical_agent = agent  # preserve for span metadata
            from dgov.router import resolve_agent as _resolve_agent

            final_agent, routed_from = _resolve_agent(agent, session_root, project_root)
            if routed_from:
                logger.info("Routed %s -> %s", routed_from, final_agent)
            agent = final_agent

            registry = load_registry(project_root)
            agent_def = registry.get(agent)
            if agent_def is None:
                raise ValueError(f"Unknown agent {agent!r}. Available: {sorted(registry)}")
            all_env.update(agent_def.env)
            if env_vars:
                all_env.update(env_vars)

            if role == "lt-gov":
                all_env["DGOV_SKIP_GOVERNOR_CHECK"] = "1"
                all_env["DGOV_PROJECT_ROOT"] = project_root

            for key in all_env:
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                    raise ValueError(f"Invalid environment variable name: {key!r}")

            # Health check (config-driven) with 10s timeout
            if agent_def.health_check:
                hc = subprocess.run(
                    agent_def.health_check, shell=True, capture_output=True, text=True, timeout=10
                )
                if hc.returncode != 0 and agent_def.health_fix:
                    subprocess.run(
                        agent_def.health_fix,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    hc = subprocess.run(
                        agent_def.health_check,
                        shell=True,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                if hc.returncode != 0:
                    raise RuntimeError(
                        f"Health check failed for {agent}: {agent_def.health_check}"
                    )

            # Concurrency guard (config-driven)
            if agent_def.max_concurrent is not None:
                # For logical routed names, resolve_agent() already chose an
                # available backend under this same lock, so repeating the
                # per-backend check here is both redundant and racy.
                if routed_from is None:
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

            # Disable bracketed paste to prevent garbled pane output
            get_backend().send_shell_command(pane_id, "printf '\\e[?2004l'")
            time.sleep(0.2)

            # 4. Setup and launch agent
            if agent_def.prompt_command == "pi":
                extra_flags = (
                    f"{extra_flags} --no-extensions".strip() if extra_flags else "--no-extensions"
                )
            pi_ext = _pi_extension_flags(project_root) if agent_def.prompt_command == "pi" else ""
            if pi_ext:
                extra_flags = f"{extra_flags} {pi_ext}".strip() if extra_flags else pi_ext
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
                context_packet=packet_for_overlay,
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
                file_claims=packet_for_overlay.file_claims,
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

            # Dispatch span (immediate open+close — dispatch is synchronous)
            try:
                from dgov.spans import (
                    SpanKind,
                    SpanOutcome,
                    close_span,
                    open_span,
                    store_prompt,
                )

                phash = store_prompt(session_root, prompt)
                sid = open_span(
                    session_root,
                    slug,
                    SpanKind.DISPATCH,
                    agent=logical_agent,
                    from_agent=agent,
                    prompt_hash=phash,
                    base_sha=base_sha,
                )
                close_span(session_root, sid, SpanOutcome.SUCCESS)
            except Exception:
                logger.debug("dispatch span failed for %s", slug, exc_info=True)

            return pane

    except BaseException:
        if pane_id:
            get_backend().destroy(pane_id)
        if owns_worktree and Path(worktree_path).exists():
            _remove_worktree(project_root, worktree_path, branch_name)
        raise


def _overlay_dirty_files(
    project_root: str,
    worktree_path: str,
    context_packet: ContextPacket | None,
) -> None:
    """Overlay dirty tracked files from governor repo into a newly created worktree.

    Only considers tracked dirty files (modifications and deletions), ignores
    untracked ?? entries. Copies current file contents for modified files and
    removes the file in worktree for tracked deletions. Scope is determined by
    context_packet.file_claims when present, otherwise context_packet.edit_files.

    No-op when owns_worktree=False or existing_worktree path provided.
    """
    if not context_packet:
        return

    # Determine editable scope
    edit_scope = (
        context_packet.file_claims if context_packet.file_claims else context_packet.edit_files
    )
    if not edit_scope:
        return

    project_root_path = Path(project_root).resolve()
    worktree_root = Path(worktree_path).resolve()

    # Get dirty tracked files from governor repo: M (modified) and D (deleted)
    result = subprocess.run(
        ["git", "-C", str(project_root_path), "status", "--porcelain=v1"],
        capture_output=True,
        text=True,
        check=True,
    )

    for line in result.stdout.splitlines():
        if not line.strip():
            continue

        # Git porcelain format: XX YYY filename
        # XX: status index, working tree; YYY: upstream (renamed)
        status_prefix = line[:3]
        working_status = status_prefix[1]
        filepath = line[3:].strip()

        if not filepath:
            continue

        # Only handle modifications (M) and deletions (D) in working tree
        if working_status not in ("M", "D"):
            continue

        # Check if file overlaps the task's editable scope
        norm_path = filepath.strip().lstrip("./").rstrip("/")
        matches_scope = any(
            norm_path == relpath.strip().lstrip("./").rstrip("/")
            or norm_path.startswith(relpath.strip().lstrip("./").rstrip("/") + "/")
            for relpath in edit_scope
            if relpath
        )
        if not matches_scope:
            continue

        worktree_file = worktree_root / filepath
        project_file = project_root_path / filepath

        if working_status == "D":
            # Tracked deletion: remove file in worktree if it exists
            if worktree_file.exists():
                worktree_file.unlink()
        else:
            # Modified: copy current contents from governor repo
            if project_file.exists():
                worktree_file.parent.mkdir(parents=True, exist_ok=True)
                worktree_file.write_bytes(project_file.read_bytes())


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

    Returns {"cleaned": bool, "skipped_worktree": bool, "branch_kept": bool,
             "worktree_removal_failed": bool or None, "crash_log": str}.
    """
    # 1. Read log file content before deletion to preserve crash logs
    crash_log = ""
    log_path = Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"
    if log_path.exists():
        try:
            raw = log_path.read_text(errors="replace")
            lines = raw.splitlines()
            if len(lines) > 500:
                lines = lines[-500:]
            crash_log = "\n".join(lines)
        except OSError:
            pass
    done_path = Path(session_root) / STATE_DIR / "done" / slug
    done_path.unlink(missing_ok=True)
    exit_path = Path(session_root) / STATE_DIR / "done" / (slug + ".exit")
    exit_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)

    # 2. Kill process group with bounded wait, then tmux pane
    pane_id = pane_record.get("pane_id")
    if pane_id:
        try:
            from dgov.tmux import _run as tmux_run

            pid_str = tmux_run(
                ["display-message", "-t", pane_id, "-p", "#{pane_pid}"],
                silent=True,
            )
            if pid_str.strip():
                terminate_result = _terminate_pane_process_tree(int(pid_str.strip()))
                still = terminate_result.get("still_running", [])
                if not terminate_result.get("terminated") and still:
                    logger.warning(
                        "Pane %s: %d process(es) survived termination; continuing with cleanup.",
                        slug,
                        len(still),
                    )
        except (RuntimeError, ValueError):
            pass  # pane already dead or bad PID, skip PGID kill
        get_backend().destroy(pane_id)

    # 3. Remove worktree + branch
    skipped_worktree = False
    branch_kept = False
    worktree_removal_failed = None
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
            remove_result = subprocess.run(
                ["git", "-C", project_root, "worktree", "remove", "--force", wt],
                capture_output=True,
                text=True,
            )
            if remove_result.returncode != 0:
                logger.error(
                    "Failed to remove worktree %s: %s",
                    wt,
                    remove_result.stderr.strip(),
                )
                worktree_removal_failed = True

            if branch and not worktree_removal_failed:
                pane_state = pane_record.get("state", "")
                _force_delete_states = {"merged", "superseded", "timed_out", "abandoned"}
                delete_flag = "-D" if pane_state in _force_delete_states else "-d"
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

            if not skipped_worktree and not worktree_removal_failed:
                subprocess.run(
                    ["git", "-C", project_root, "worktree", "prune"],
                    capture_output=True,
                )
        elif worktree_removal_failed:
            # Still prune to clean stale metadata even if remove failed
            subprocess.run(
                ["git", "-C", project_root, "worktree", "prune"],
                capture_output=True,
            )

    # 4. Copy pi session transcript (all workers run through pi harness)
    worktree_path = pane_record.get("worktree_path", "")
    if worktree_path:
        # Transform worktree path to pi session dir format:
        # /Users/jakegearon/projects/dgov/.dgov/worktrees/my-task
        # → --Users-jakegearon-projects-dgov-.dgov-worktrees-my-task--
        session_dir_name = f"--{worktree_path.lstrip('/').replace('/', '-')}--"
        pi_sessions_root = Path.home() / ".pi" / "agent" / "sessions"
        session_dir = pi_sessions_root / session_dir_name

        if session_dir.exists():
            # Find all .jsonl files and get the newest one
            jsonl_files = list(session_dir.glob("*.jsonl"))
            if jsonl_files:
                newest_session = max(jsonl_files, key=lambda p: p.stat().st_mtime)
                # Ensure .dgov/logs exists in project root
                logs_dir = Path(project_root) / ".dgov" / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                # Copy to .dgov/logs/<slug>.transcript.jsonl
                dest_path = logs_dir / f"{slug}.transcript.jsonl"
                shutil.copy2(newest_session, dest_path)

                # Store raw transcript in DB + ingest structured traces
                sr = session_root or project_root
                try:
                    from dgov.spans import ingest_transcript, store_transcript

                    raw = dest_path.read_text()
                    store_transcript(sr, slug, raw)
                    ingest_transcript(sr, slug, str(dest_path))
                except Exception:
                    logger.debug("transcript ingest failed for %s", slug, exc_info=True)

    return {
        "cleaned": not worktree_removal_failed or skipped_worktree,
        "skipped_worktree": skipped_worktree,
        "branch_kept": branch_kept,
        "worktree_removal_failed": worktree_removal_failed,
        "crash_log": crash_log,
    }


def close_worker_pane(
    project_root: str, slug: str, session_root: str | None = None, *, force: bool = False
) -> bool:
    """Close a worker pane: kill tmux pane, remove worktree, update state.

    For LT-GOV or parent panes, cascades to close all child panes first.

    When worktree is preserved, the pane record remains inspectable and keeps
    its current state.

    When worktree is removed, pane transitions to 'closed' and record is deleted.
    """
    session_root = os.path.abspath(session_root) if session_root else project_root
    target = get_pane(session_root, slug)

    if not target:
        # Check if this slug was ever in the DB (archived or event history)
        from dgov.persistence import read_events

        events = read_events(session_root, slug=slug)
        if not events:
            return False  # truly nonexistent slug
        return True  # was cleaned up by merge or close

    # Open close span
    close_span_id = None
    try:
        from dgov.spans import SpanKind, open_span

        close_span_id = open_span(session_root, slug, SpanKind.CLOSE)
    except Exception:
        logger.debug("close span open failed for %s", slug, exc_info=True)

    # Use persisted pane project_root for cleanup, normalize with abspath
    pane_project_root = target.get("project_root") or project_root
    clean_project_root = os.path.abspath(pane_project_root)

    # Cascade: close all child panes before closing the parent
    children = get_child_panes(session_root, slug)
    for child in children:
        child_slug = child["slug"]
        logger.info("Cascade-closing child pane %s (parent: %s)", child_slug, slug)
        # Use each child's persisted project_root for recursive close
        child_project_root = os.path.abspath(child.get("project_root", project_root))
        close_worker_pane(child_project_root, child_slug, session_root, force=force)

    # Auto-enable force for terminal-state panes
    if target.get("state") in (
        "merged",
        "closed",
        "done",
        "failed",
        "merge_conflict",
        "superseded",
        "timed_out",
        "abandoned",
    ):
        force = True

    result = _full_cleanup(
        clean_project_root,
        session_root,
        slug,
        target,
        skip_worktree_if_dirty=not force,
    )

    # Extract crash_log from cleanup result for archival
    crash_log = result.get("crash_log", "")

    # Ingest transcript into tool_traces table
    transcript_captured = 0
    transcript_path = Path(clean_project_root) / ".dgov" / "logs" / f"{slug}.transcript.jsonl"
    if transcript_path.exists():
        try:
            from dgov.spans import ingest_transcript

            ingest_transcript(session_root, slug, str(transcript_path))
            transcript_captured = 1
        except Exception:
            logger.debug("transcript ingest failed for %s", slug, exc_info=True)

    pane_state = str(target.get("state", ""))
    cleanup_must_close = pane_state in {"superseded", "escalated"}

    # Preserve evidence when worktree removal fails or is skipped (dirty pane),
    # except for superseded/escalated panes where the replacement pane is now
    # canonical and lingering records become coordination noise.
    if result.get("skipped_worktree") and not cleanup_must_close:
        mark_preserved_artifacts(
            session_root,
            slug,
            reason="dirty_worktree",
            recoverable=True,
            state=pane_state,
        )
        logger.info("Pane %s preserved for inspection in state %s", slug, pane_state)
    elif result.get("worktree_removal_failed") and not cleanup_must_close:
        # Check if the path is a real git worktree or just a leftover directory.
        # The orphan pruner race can leave empty dirs that git doesn't recognize.
        wt = target.get("worktree_path", "")
        is_valid_worktree = False
        if wt and Path(wt).exists():
            check = subprocess.run(
                ["git", "-C", wt, "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
            )
            # Valid worktree has a .git file pointing to main repo's .git/worktrees/
            is_valid_worktree = check.returncode == 0 and "worktrees" in check.stdout

        if not is_valid_worktree:
            # Leftover directory or already gone — clean up everything
            if wt and Path(wt).is_dir():
                import shutil

                shutil.rmtree(wt, ignore_errors=True)
            logger.info("Worktree invalid/gone for %s — cleaning up pane record", slug)
            update_pane_state(session_root, slug, "closed")
            emit_event(session_root, "pane_closed", slug)
            remove_pane(session_root, slug, crash_log=crash_log)
        else:
            mark_preserved_artifacts(
                session_root,
                slug,
                reason="cleanup_failed",
                recoverable=False,
                state=pane_state,
            )
            logger.warning(
                "Worktree removal failed for %s — pane state preserved for inspection",
                slug,
            )
    else:
        # Normal close - worktree removed, or retry/escalation cleanup must be
        # deterministic even when filesystem cleanup was imperfect.
        update_pane_state(session_root, slug, "closed")
        emit_event(session_root, "pane_closed", slug)
        remove_pane(session_root, slug, crash_log=crash_log)

    # Close the close span
    if close_span_id is not None:
        try:
            from dgov.spans import SpanOutcome, close_span

            close_span(
                session_root,
                close_span_id,
                SpanOutcome.SUCCESS,
                transcript_captured=transcript_captured,
            )
        except Exception:
            logger.debug("close span close failed for %s", slug, exc_info=True)

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

    clear_preserved_artifacts(session_root, slug)

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

    # Disable bracketed paste to prevent garbled pane output
    get_backend().send_shell_command(pane_id, "printf '\\e[?2004l'")
    time.sleep(0.2)

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
        base_sha=target.get("base_sha"),
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
