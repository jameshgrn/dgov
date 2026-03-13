"""Core pane lifecycle: create, close, list, merge.

Each worker pane = git worktree + tmux pane + agent CLI.
State tracked in .dgov/state.json.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dgov import tmux
from dgov.agents import AGENT_REGISTRY, build_launch_command
from dgov.models import MergeResult

logger = logging.getLogger(__name__)

# -- Event log --

VALID_EVENTS = frozenset(
    {
        "pane_created",
        "pane_done",
        "pane_timed_out",
        "pane_merged",
        "pane_merge_failed",
        "pane_escalated",
        "pane_superseded",
        "pane_closed",
        "pane_retry_spawned",
        "checkpoint_created",
        "review_pass",
        "review_fail",
    }
)


def _emit_event(session_root: str, event: str, pane: str, **kwargs) -> None:
    """Append a structured event to .dgov/events.jsonl."""
    from datetime import datetime, timezone

    if event not in VALID_EVENTS:
        raise ValueError(f"Unknown event: {event!r}. Valid: {sorted(VALID_EVENTS)}")
    events_path = Path(session_root) / _STATE_DIR / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "pane": pane,
        **kwargs,
    }
    with open(events_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# -- Pane record --


# Canonical pane states — no others allowed
PANE_STATES = frozenset(
    {
        "active",
        "done",
        "reviewed_pass",
        "reviewed_fail",
        "merged",
        "merge_conflict",
        "timed_out",
        "escalated",
        "superseded",
        "closed",
        "abandoned",
    }
)


def _validate_state(state: str) -> str:
    """Validate and return a canonical pane state. Raises ValueError for unknown states."""
    if state not in PANE_STATES:
        raise ValueError(f"Unknown pane state: {state!r}. Valid: {sorted(PANE_STATES)}")
    return state


@dataclass
class WorkerPane:
    slug: str
    prompt: str
    pane_id: str
    agent: str
    project_root: str
    worktree_path: str
    branch_name: str
    created_at: float = field(default_factory=time.time)
    owns_worktree: bool = True
    base_sha: str = ""
    state: str = "active"

    def __post_init__(self) -> None:
        _validate_state(self.state)


# -- State file helpers --

_STATE_DIR = ".dgov"
_PROTECTED_FILES = {"CLAUDE.md", "CLAUDE.md.full", "THEORY.md", "ARCH-NOTES.md", ".napkin.md"}
_STATE_FILE = "state.json"
_MAX_CONCURRENT_PI_WORKERS = 2


def _build_pane_title(slug: str, project_root: str) -> str:
    """Build pane title for tmux pane border display.

    Format: ``slug@project_name-hash`` where *hash* is the first 4 hex
    chars of the MD5 digest of *project_root*.
    """
    import hashlib

    project_name = os.path.basename(project_root)
    hash_prefix = hashlib.md5(project_root.encode()).hexdigest()[:4]
    return f"{slug}@{project_name}-{hash_prefix}"


def _state_path(session_root: str) -> Path:
    return Path(session_root) / _STATE_DIR / _STATE_FILE


def _read_state(session_root: str) -> dict:
    path = _state_path(session_root)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"panes": []}


def _write_state(session_root: str, state: dict) -> None:
    path = _state_path(session_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.rename(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _add_pane(session_root: str, pane: WorkerPane) -> None:
    state = _read_state(session_root)
    # Upsert: remove any existing entry with the same slug before appending
    state["panes"] = [p for p in state["panes"] if p.get("slug") != pane.slug]
    state["panes"].append(asdict(pane))
    _write_state(session_root, state)


def _remove_pane(session_root: str, slug: str) -> None:
    state = _read_state(session_root)
    state["panes"] = [p for p in state["panes"] if p.get("slug") != slug]
    _write_state(session_root, state)


def _get_pane(session_root: str, slug: str) -> dict | None:
    state = _read_state(session_root)
    return next((p for p in state["panes"] if p.get("slug") == slug), None)


def _all_panes(session_root: str) -> list[dict]:
    return _read_state(session_root).get("panes", [])


def _update_pane_state(session_root: str, slug: str, new_state: str) -> None:
    """Update the state field of a pane record."""
    _validate_state(new_state)
    state = _read_state(session_root)
    for p in state["panes"]:
        if p.get("slug") == slug:
            p["state"] = new_state
            break
    _write_state(session_root, state)


def _count_active_pi_workers(session_root: str) -> int:
    """Count how many pi workers are currently alive."""
    panes = _all_panes(session_root)
    count = 0
    for p in panes:
        if p.get("agent") == "pi":
            pane_id = p.get("pane_id", "")
            if pane_id and tmux.pane_exists(pane_id):
                count += 1
    return count


# -- Qwen 4B helper (tunnel-aware) --

_QWEN_4B_URL = "http://localhost:8082/v1/chat/completions"
_QWEN_4B_TIMEOUT = 5


def _qwen_4b_request(messages: list[dict], max_tokens: int = 20, temperature: float = 0) -> dict:
    """Send a request to Qwen 4B, trying localhost first then SSH tunnel.

    Returns the parsed JSON response dict.
    Raises on failure (caller should catch).
    """
    import urllib.request

    body = json.dumps(
        {
            "model": "qwen",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    ).encode()

    # Try 1: direct localhost (tunnel is up locally)
    try:
        req = urllib.request.Request(
            _QWEN_4B_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_QWEN_4B_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception:
        logger.debug("Qwen 4B direct request failed, trying SSH fallback")

    # Try 2: SSH to river and curl from there
    json_str = json.dumps(
        {
            "model": "qwen",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    )
    curl_cmd = (
        f"curl -s --max-time {_QWEN_4B_TIMEOUT} -X POST "
        f"-H 'Content-Type: application/json' "
        f"-d @- 'http://localhost:8082/v1/chat/completions' <<'__JSON__'\n{json_str}\n__JSON__"
    )
    script = f"ssh river 'bash -l' <<'HEREDOC'\n{curl_cmd}\nHEREDOC"
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=_QWEN_4B_TIMEOUT + 30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH curl to river failed (exit {result.returncode})")
    return json.loads(result.stdout)


# -- Task routing --


def classify_task(prompt: str) -> str:
    """Classify a task prompt and recommend an agent via Qwen 4B.

    Returns "pi" for mechanical tasks (run commands, edit specific lines,
    format files) and "claude" for analytical tasks (debug flaky tests,
    refactor architecture, multi-step reasoning).

    Falls back to "claude" if the model is unreachable.
    """
    try:
        result = _qwen_4b_request(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Classify this task as either 'pi' or 'claude'.\n"
                        "pi = mechanical: run a command, edit a specific line, "
                        "add a comment, format files, simple find-and-replace.\n"
                        "claude = analytical: debug why something fails, read and "
                        "understand complex code, refactor architecture, fix flaky "
                        "tests, multi-file reasoning, rework/redesign a system, "
                        "add a new feature with multiple moving parts, "
                        "anything involving scheduler.py or panes.py.\n"
                        "Reply with ONLY 'pi' or 'claude', nothing else."
                    ),
                },
                {"role": "user", "content": prompt[:300]},
            ],
            max_tokens=5,
            temperature=0,
        )
        answer = result["choices"][0]["message"]["content"].strip().lower()
        if "pi" in answer:
            return "pi"
        return "claude"
    except Exception:
        return "claude"


# -- Slug generation --


def _generate_slug(prompt: str, max_words: int = 4) -> str:
    """Generate a descriptive kebab-case slug using Qwen 4B, with local fallback."""
    try:
        result = _qwen_4b_request(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Generate a short kebab-case slug (2-4 words, lowercase, "
                        "hyphens only) that describes this task. "
                        "Reply with ONLY the slug, nothing else."
                    ),
                },
                {"role": "user", "content": prompt[:200]},
            ],
            max_tokens=20,
            temperature=0.3,
        )
        raw = result["choices"][0]["message"]["content"].strip().lower()
        slug = re.sub(r"[^a-z0-9-]", "", raw).strip("-")
        if slug and len(slug) <= 50:
            return slug
    except Exception:
        logger.debug("LLM-based slug generation failed, using word extraction fallback")
    # Fallback: local word extraction
    words = re.sub(r"[^a-z0-9\s]", "", prompt.lower()).split()
    skip = {"the", "a", "an", "in", "on", "at", "to", "for", "of", "and", "or", "is", "it"}
    content = [w for w in words if w not in skip][:max_words]
    slug = "-".join(content) if content else f"task-{int(time.time())}"
    return slug[:50]


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


def _remove_worktree(project_root: str, worktree_path: str, branch_name: str) -> None:
    subprocess.run(
        ["git", "-C", project_root, "worktree", "remove", "--force", worktree_path],
        capture_output=True,
    )
    subprocess.run(["git", "-C", project_root, "branch", "-D", branch_name], capture_output=True)
    subprocess.run(["git", "-C", project_root, "worktree", "prune"], capture_output=True)


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
    subprocess.run(
        ["git", "-C", project_root, "reset", "--hard", "HEAD"],
        capture_output=True,
    )

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
    1. .dgov-hooks/ (version controlled, team hooks)
    2. .dgov/hooks/ (gitignored, local overrides)
    3. ~/.dgov/hooks/ (global user hooks)
    """
    hook_dirs = [
        Path(project_root) / ".dgov-hooks",
        Path(project_root) / ".dgov" / "hooks",
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
    owns_worktree = existing_worktree is None
    branch_name = slug
    worktree_path = (
        existing_worktree
        if existing_worktree
        else str(Path(project_root) / ".dgov" / "worktrees" / slug)
    )

    # 0. Capture base SHA (HEAD of project_root before worktree creation)
    base_sha_result = subprocess.run(
        ["git", "-C", project_root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    base_sha = base_sha_result.stdout.strip() if base_sha_result.returncode == 0 else ""

    # 1. Create git worktree (skip if using existing path)
    if owns_worktree:
        _create_worktree(project_root, worktree_path, branch_name)

    # 1b. Preflight health check for pi workers (35B + 4B ports)
    if agent == "pi":
        tunnel_up = False
        for port in (8080, 8081, 8082):
            health = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-o",
                    "/dev/null",
                    "-w",
                    "%{http_code}",
                    "--max-time",
                    "5",
                    f"http://localhost:{port}/health",
                ],
                capture_output=True,
                text=True,
            )
            if health.stdout.strip() == "200":
                tunnel_up = True
                break
        if not tunnel_up:
            if owns_worktree:
                _remove_worktree(project_root, worktree_path, branch_name)
            raise RuntimeError("SSH tunnel to river is down -- run the tunnel first")

    # 1c. GPU concurrency guard for pi workers
    if agent == "pi":
        active_pi = _count_active_pi_workers(session_root)
        if active_pi >= _MAX_CONCURRENT_PI_WORKERS:
            if owns_worktree:
                _remove_worktree(project_root, worktree_path, branch_name)
            raise RuntimeError(
                f"GPU concurrency limit: {active_pi} pi workers already running "
                f"(max {_MAX_CONCURRENT_PI_WORKERS}). "
                f"Wait for one to finish or use a different agent."
            )

    # 2. Split tmux pane
    tmux.setup_pane_borders()
    pane_id = tmux.split_pane(cwd=worktree_path)

    # 3. Lock pane title (prevent agent/tmux from overwriting)
    tmux._run(["set-option", "-p", "-t", pane_id, "allow-rename", "off"])
    tmux._run(["set-option", "-p", "-t", pane_id, "automatic-rename", "off"])
    title = _build_pane_title(slug, project_root)
    tmux.set_title(pane_id, title)

    # 4. Tidy layout
    tmux.select_layout("tiled")

    # 5. Clear CLAUDECODE recursion guard (inherited from parent claude session)
    tmux.send_command(pane_id, "unset CLAUDECODE")

    # 5b. Inject env vars
    if env_vars:
        for key, val in env_vars.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
                raise ValueError(f"Invalid environment variable name: {key!r}")
            tmux.send_command(pane_id, f"export {key}={val!r}")

    # 6. Trigger worktree_created hook
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

    # 6b. Fallback if hook missing/failed: CLAUDE.md.full exclude + protected-file warning
    if not hook_ran and owns_worktree:
        git_dir_result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
        )
        if git_dir_result.returncode == 0:
            git_dir = Path(git_dir_result.stdout.strip())
            if not git_dir.is_absolute():
                git_dir = Path(worktree_path) / git_dir
            exclude_file = git_dir / "info" / "exclude"
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude_file, "a") as f:
                f.write("\nCLAUDE.md.full\n")

    # 7. Rewrite absolute paths in prompt so agent edits worktree, not main repo
    rewritten_prompt = prompt.replace(project_root, worktree_path)

    # 7b. Fallback protected-file warning if hook didn't write CLAUDE.md
    if not hook_ran:
        protected_warning = (
            "\n\nIMPORTANT: Do NOT modify or overwrite these files: "
            + ", ".join(sorted(_PROTECTED_FILES))
            + ". Do NOT create new documentation files."
        )
        if protected_warning.strip() not in rewritten_prompt:
            rewritten_prompt += protected_warning

    # 8. Build done-signal path
    done_signal = str(Path(session_root) / _STATE_DIR / "done" / slug)
    Path(done_signal).parent.mkdir(parents=True, exist_ok=True)

    # 9. Launch agent (with done-signal wrapper)
    agent_def = AGENT_REGISTRY.get(agent)
    if agent_def:
        if agent_def.prompt_transport == "send-keys":
            base_cmd = build_launch_command(
                agent,
                None,
                permission_mode,
                project_root=worktree_path,
                slug=slug,
                extra_flags=extra_flags,
            )
            # Wrap with done signal
            wrapped_cmd = f"{base_cmd}; touch {shlex.quote(done_signal)}"
            tmux.send_command(pane_id, wrapped_cmd)
            if agent_def.send_keys_ready_delay_ms > 0:
                time.sleep(agent_def.send_keys_ready_delay_ms / 1000)
            for key in agent_def.send_keys_pre_prompt:
                tmux._run(["send-keys", "-t", pane_id, key])
            tmux.send_prompt_via_buffer(pane_id, rewritten_prompt)
        else:
            launch_cmd = build_launch_command(
                agent,
                rewritten_prompt,
                permission_mode,
                project_root=worktree_path,
                slug=slug,
                extra_flags=extra_flags,
            )
            # Wrap with done signal: when agent exits, touch the file
            wrapped_cmd = f"{launch_cmd}; touch {shlex.quote(done_signal)}"
            tmux.send_command(pane_id, wrapped_cmd)

    # 9b. Set tmux pane title
    title = _build_pane_title(slug, project_root)
    tmux.set_title(pane_id, title)

    # 10. Build pane record and save to state
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
        tmux.kill_pane(pane_id)
        if tmux.pane_exists(pane_id):
            time.sleep(0.2)
            tmux.kill_pane(pane_id)

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

    tmux.select_layout("tiled")

    # 4. Remove from dgov state (after tmux kill and worktree removal)
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


def _has_new_commits(project_root: str, branch_name: str, base_sha: str) -> bool:
    """Check if *branch_name* has commits newer than *base_sha*."""
    if not base_sha:
        return False
    result = subprocess.run(
        ["git", "-C", project_root, "log", branch_name, "--not", base_sha, "--oneline"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _is_done(session_root: str, slug: str, pane_record: dict | None = None) -> bool:
    """Check if a worker is done via any of three signals.

    1. Done-signal file exists (agent exited cleanly).
    2. Branch has new commits beyond base_sha (worker committed work).
    3. Pane is no longer alive (process died / was killed).

    Any one signal returning True means done. Also updates pane state to "done".
    """
    done_path = Path(session_root, _STATE_DIR, "done", slug)

    # Signal 1: done-signal file
    if done_path.exists():
        _update_pane_state(session_root, slug, "done")
        return True

    if pane_record is None:
        return False

    # Signal 2: new commits on the branch
    project_root = pane_record.get("project_root", "")
    branch_name = pane_record.get("branch_name", "")
    base_sha = pane_record.get("base_sha", "")
    if project_root and branch_name and base_sha:
        if _has_new_commits(project_root, branch_name, base_sha):
            _update_pane_state(session_root, slug, "done")
            _emit_event(session_root, "pane_done", slug)
            # Touch done-signal so we don't re-emit
            done_path.parent.mkdir(parents=True, exist_ok=True)
            done_path.touch()
            return True

    # Signal 3: pane no longer alive
    pane_id = pane_record.get("pane_id", "")
    if pane_id and not tmux.pane_exists(pane_id):
        _update_pane_state(session_root, slug, "done")
        _emit_event(session_root, "pane_done", slug)
        done_path.parent.mkdir(parents=True, exist_ok=True)
        done_path.touch()
        return True

    return False


def list_worker_panes(project_root: str, session_root: str | None = None) -> list[dict]:
    """List worker panes with live status from tmux."""
    session_root = os.path.abspath(session_root or project_root)
    panes = _all_panes(session_root)
    result = []
    for p in panes:
        pane_id = p.get("pane_id", "")
        slug = p["slug"]
        alive = tmux.pane_exists(pane_id) if pane_id else False
        cmd = ""
        if alive:
            try:
                cmd = tmux.current_command(pane_id)
            except RuntimeError:
                pass
        done = _is_done(session_root, slug, pane_record=p)
        freshness = _compute_freshness(project_root, p)
        entry: dict = {
            "slug": slug,
            "agent": p.get("agent"),
            "pane_id": pane_id,
            "alive": alive,
            "done": done,
            "state": p.get("state", "active"),
            "current_command": cmd,
            "worktree_path": p.get("worktree_path"),
            "branch": p.get("branch_name"),
            "prompt": p.get("prompt", "")[:80],
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
        alive = tmux.pane_exists(pane_id) if pane_id else False
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
    if not tmux.pane_exists(pane_id):
        return None

    return tmux.capture_pane(pane_id, lines)


# -- Wait helpers (public API) --


class PaneTimeoutError(Exception):
    """Raised when waiting for a pane exceeds the timeout."""

    def __init__(
        self,
        slug: str,
        timeout: int,
        agent: str = "unknown",
        pending_panes: list[dict] | None = None,
    ):
        self.slug = slug
        self.timeout = timeout
        self.agent = agent
        self.pending_panes = pending_panes or [{"slug": slug, "agent": agent}]
        super().__init__(f"Pane {slug!r} timed out after {timeout}s")


def _poll_once(
    session_root: str,
    project_root: str,
    slug: str,
    pane_record: dict | None,
    last_output: str | None,
    stable_since: float | None,
    stable: int,
) -> tuple[bool, str, str | None, float | None]:
    """Single poll cycle shared by wait_worker_pane and wait_all_worker_panes.

    Returns (is_done, method, last_output, stable_since).
    """
    if _is_done(session_root, slug, pane_record=pane_record):
        return True, "signal_or_commit", last_output, stable_since

    current_output = capture_worker_output(project_root, slug, lines=20, session_root=session_root)
    if current_output is not None:
        if current_output == last_output:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable:
                done_path = Path(session_root) / _STATE_DIR / "done" / slug
                done_path.parent.mkdir(parents=True, exist_ok=True)
                done_path.touch()
                return True, "stable", current_output, stable_since
        else:
            last_output = current_output
            stable_since = None

    return False, "", last_output, stable_since


def wait_worker_pane(
    project_root: str,
    slug: str,
    session_root: str | None = None,
    timeout: int = 600,
    poll: int = 3,
    stable: int = 15,
) -> dict:
    """Wait for a single worker pane to finish.

    Returns ``{"done": slug, "method": ...}`` on success.
    Raises ``PaneTimeoutError`` on timeout.
    """
    session_root = os.path.abspath(session_root or project_root)
    pane_record = _get_pane(session_root, slug)
    start = time.monotonic()
    last_output: str | None = None
    stable_since: float | None = None

    while True:
        done, method, last_output, stable_since = _poll_once(
            session_root,
            project_root,
            slug,
            pane_record,
            last_output,
            stable_since,
            stable,
        )
        if done:
            return {"done": slug, "method": method}

        elapsed = time.monotonic() - start
        if timeout > 0 and elapsed >= timeout:
            _update_pane_state(session_root, slug, "timed_out")
            _emit_event(session_root, "pane_timed_out", slug)
            agent = pane_record.get("agent", "unknown") if pane_record else "unknown"
            raise PaneTimeoutError(slug, timeout, agent)
        time.sleep(poll)


def wait_all_worker_panes(
    project_root: str,
    session_root: str | None = None,
    timeout: int = 600,
    poll: int = 3,
    stable: int = 15,
):
    """Wait for ALL worker panes to finish.

    Yields ``{"done": slug, "method": ...}`` as each pane completes.
    Raises ``PaneTimeoutError`` (with the first timed-out slug) on timeout.
    """
    session_root = os.path.abspath(session_root or project_root)
    panes = list_worker_panes(project_root, session_root=session_root)
    pending = {p["slug"] for p in panes if not p["done"]}
    if not pending:
        return

    start = time.monotonic()
    stable_trackers: dict[str, tuple[str | None, float | None]] = {
        s: (None, None) for s in pending
    }

    while pending:
        for slug in list(pending):
            rec = _get_pane(session_root, slug)
            last, since = stable_trackers.get(slug, (None, None))
            done, method, last, since = _poll_once(
                session_root,
                project_root,
                slug,
                rec,
                last,
                since,
                stable,
            )
            stable_trackers[slug] = (last, since)
            if done:
                pending.discard(slug)
                yield {"done": slug, "method": method}

        elapsed = time.monotonic() - start
        if timeout > 0 and elapsed >= timeout:
            pending_info = []
            for s in sorted(pending):
                rec = _get_pane(session_root, s)
                pending_info.append(
                    {
                        "slug": s,
                        "agent": rec.get("agent", "unknown") if rec else "unknown",
                    }
                )
            first = pending_info[0]
            raise PaneTimeoutError(
                first["slug"],
                timeout,
                first["agent"],
                pending_panes=pending_info,
            )
        if pending:
            time.sleep(poll)


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

    # Close the resolver pane
    close_worker_pane(project_root, resolver.slug, session_root=session_root)

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
        return True

    # Failed — abort merge
    subprocess.run(
        ["git", "-C", project_root, "merge", "--abort"],
        capture_output=True,
    )
    return False


def _commit_worktree(pane_record: dict) -> dict:
    """Auto-commit uncommitted changes in a worker's worktree.

    Stages all modified/new files except hook artifacts (CLAUDE.md, CLAUDE.md.full).
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
    session_root = os.path.abspath(session_root or project_root)
    target = _get_pane(session_root, slug)

    if not target:
        return {"error": f"Pane not found: {slug}"}

    branch_name = target.get("branch_name")
    pane_project_root = target.get("project_root") or project_root

    # Auto-commit uncommitted changes in worktree
    commit_result = _commit_worktree(target)

    # Pre-merge hook: restore protected files, etc.
    pre_merge_env = {
        "DGOV_PROJECT_ROOT": pane_project_root,
        "DGOV_WORKTREE_PATH": target.get("worktree_path", ""),
        "DGOV_BRANCH": branch_name or "",
        "DGOV_BASE_SHA": target.get("base_sha", ""),
        "DGOV_SLUG": slug,
        "DGOV_PROTECTED_FILES": " ".join(sorted(_PROTECTED_FILES)),
    }
    if not _trigger_hook("pre_merge", pane_project_root, pre_merge_env, timeout=30):
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

    # Plumbing merge — zero working-tree side effects on failure
    merge = _plumbing_merge(pane_project_root, branch_name)

    if merge.success:
        _update_pane_state(session_root, slug, "merged")
        _emit_event(session_root, "pane_merged", slug)
        _full_cleanup(pane_project_root, session_root, slug, target)

        # Post-merge hook: lint, verify protected files, etc.
        post_merge_env = {
            "DGOV_PROJECT_ROOT": pane_project_root,
            "DGOV_BASE_SHA": target.get("base_sha", ""),
            "DGOV_SLUG": slug,
            "DGOV_BRANCH": branch_name or "",
            "DGOV_CHANGED_FILES": "\n".join(changed_file_names),
            "DGOV_PROTECTED_FILES": " ".join(sorted(_PROTECTED_FILES)),
        }
        hook_ran = _trigger_hook("post_merge", pane_project_root, post_merge_env, timeout=30)

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
            lint_result = _lint_fix_merged_files(pane_project_root, changed_file_names)

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
    conflicts = _detect_conflicts(pane_project_root, branch_name)

    if conflicts:
        _update_pane_state(session_root, slug, "merge_conflict")
        if resolve == "agent":
            resolved = _resolve_conflicts_with_agent(
                pane_project_root, branch_name, target, session_root
            )
            if resolved:
                _update_pane_state(session_root, slug, "merged")
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
    _emit_event(session_root, "pane_merge_failed", slug, error=error_msg)
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
    session_root = os.path.abspath(session_root or project_root)
    result = merge_worker_pane(project_root, slug, session_root, resolve=resolve)

    if "error" in result:
        return result

    # Close the pane after successful merge. Note: merge_worker_pane already cleans up on success,
    # so close may fail silently because pane is no longer in state — that's expected.
    if not close_worker_pane(project_root, slug, session_root):
        logger.debug("Pane %s already cleaned up by merge worker", slug)

    return {"merged": slug, "branch": result["branch"]}


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
    uncommitted = bool(porcelain.stdout.strip())

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

    # Link records: update new pane with retried_from
    state = _read_state(session_root)
    for p in state["panes"]:
        if p.get("slug") == new_slug:
            p["retried_from"] = slug
        if p.get("slug") == slug:
            p["superseded_by"] = new_slug
            p["state"] = "superseded"
    _write_state(session_root, state)

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


def create_checkpoint(
    project_root: str,
    name: str,
    session_root: str | None = None,
) -> dict:
    """Create a checkpoint snapshot of current state."""
    from datetime import datetime, timezone

    session_root = os.path.abspath(session_root or project_root)

    # Get main SHA
    main_sha_result = subprocess.run(
        ["git", "-C", project_root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    main_sha = main_sha_result.stdout.strip() if main_sha_result.returncode == 0 else ""

    # Get all pane records
    panes = _all_panes(session_root)

    # Get branch heads for each pane
    branch_heads = {}
    for p in panes:
        branch = p.get("branch_name", "")
        wt = p.get("worktree_path", "")
        if branch and wt and Path(wt).exists():
            head_result = subprocess.run(
                ["git", "-C", wt, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
            )
            if head_result.returncode == 0:
                branch_heads[branch] = head_result.stdout.strip()

    checkpoint = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "name": name,
        "main_sha": main_sha,
        "panes": panes,
        "branch_heads": branch_heads,
    }

    # Write to .dgov/checkpoints/<name>.json
    checkpoint_dir = Path(session_root) / _STATE_DIR / "checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = checkpoint_dir / f"{name}.json"
    with open(checkpoint_path, "w") as f:
        json.dump(checkpoint, f, indent=2, default=str)
        f.write("\n")

    _emit_event(session_root, "checkpoint_created", f"checkpoint/{name}", main_sha=main_sha)

    return {"checkpoint": name, "main_sha": main_sha, "pane_count": len(panes)}


def list_checkpoints(session_root: str) -> list[dict]:
    """List all checkpoints."""
    checkpoint_dir = Path(session_root) / _STATE_DIR / "checkpoints"
    if not checkpoint_dir.exists():
        return []

    checkpoints = []
    for f in sorted(checkpoint_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            checkpoints.append(
                {
                    "name": data.get("name", f.stem),
                    "ts": data.get("ts", ""),
                    "pane_count": len(data.get("panes", [])),
                    "main_sha": data.get("main_sha", "")[:8],
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    return checkpoints


# ---------------------------------------------------------------------------
# Batch execution
# ---------------------------------------------------------------------------


def _compute_tiers(tasks: list[dict]) -> list[list[dict]]:
    """Group tasks into parallel tiers based on file touches.

    Tasks with disjoint `touches` go into the same tier.
    Tasks with overlapping touches are serialized into subsequent tiers.
    """
    tiers: list[list[dict]] = []
    remaining = list(tasks)

    while remaining:
        tier: list[dict] = []
        tier_touches: set[str] = set()
        next_remaining: list[dict] = []

        for task in remaining:
            task_touches = set(task.get("touches", []))
            has_overlap = False
            for tt in task_touches:
                for et in tier_touches:
                    if tt == et or tt.startswith(et) or et.startswith(tt):
                        has_overlap = True
                        break
                if has_overlap:
                    break

            if not has_overlap:
                tier.append(task)
                tier_touches.update(task_touches)
            else:
                next_remaining.append(task)

        if tier:
            tiers.append(tier)
        remaining = next_remaining

    return tiers


def run_batch(
    spec_path: str,
    session_root: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Execute a batch spec: create panes, wait, merge in tier order.

    Spec format (JSON file):
    {
        "project_root": "/path/to/repo",
        "tasks": [
            {"id": "task-1", "prompt": "...", "agent": "pi", "touches": ["src/foo.py"]},
            {"id": "task-2", "prompt": "...", "agent": "claude", "touches": ["tests/"]}
        ]
    }

    Tasks with disjoint `touches` run in parallel tiers.
    Overlapping touches are serialized.
    """
    with open(spec_path) as f:
        spec = json.load(f)

    project_root = spec["project_root"]
    tasks = spec["tasks"]
    session_root = os.path.abspath(session_root or project_root)

    tiers = _compute_tiers(tasks)

    if dry_run:
        return {
            "dry_run": True,
            "tiers": [[t["id"] for t in tier] for tier in tiers],
            "total_tasks": len(tasks),
        }

    results: dict = {"tiers": [], "merged": [], "failed": []}

    for tier_idx, tier in enumerate(tiers):
        tier_result: dict = {"tier": tier_idx, "tasks": []}

        # Create all panes in this tier
        slugs = []
        for task in tier:
            try:
                pane = create_worker_pane(
                    project_root=project_root,
                    prompt=task["prompt"],
                    agent=task.get("agent", "claude"),
                    permission_mode=task.get("permission_mode", "acceptEdits"),
                    slug=task["id"],
                    session_root=session_root,
                )
                slugs.append(pane.slug)
                tier_result["tasks"].append(
                    {"id": task["id"], "slug": pane.slug, "status": "created"}
                )
            except Exception as e:
                tier_result["tasks"].append(
                    {"id": task["id"], "status": "failed", "error": str(e)}
                )
                results["failed"].append(task["id"])

        # Wait for all panes in tier
        timeout = max(t.get("timeout", 600) for t in tier) if tier else 600
        start = time.monotonic()
        pending = set(slugs)

        while pending and (time.monotonic() - start < timeout):
            for slug in list(pending):
                rec = _get_pane(session_root, slug)
                if _is_done(session_root, slug, pane_record=rec):
                    pending.discard(slug)
            if pending:
                time.sleep(3)

        # Merge completed panes
        for slug in slugs:
            if slug in pending:
                tier_result["tasks"] = [
                    {**t, "status": "timed_out"} if t.get("slug") == slug else t
                    for t in tier_result["tasks"]
                ]
                results["failed"].append(slug)
                continue

            merge_result = merge_worker_pane(project_root, slug, session_root=session_root)
            if "merged" in merge_result:
                results["merged"].append(slug)
                tier_result["tasks"] = [
                    {**t, "status": "merged"} if t.get("slug") == slug else t
                    for t in tier_result["tasks"]
                ]
            else:
                results["failed"].append(slug)
                tier_result["tasks"] = [
                    {**t, "status": "merge_failed"} if t.get("slug") == slug else t
                    for t in tier_result["tasks"]
                ]

        results["tiers"].append(tier_result)

        # Abort remaining tiers if any failure
        if results["failed"]:
            results["aborted_remaining"] = True
            break

    return results
