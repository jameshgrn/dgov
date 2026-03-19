"""Task routing, slug generation, and prompt structuring."""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

# -- Task routing --


def classify_task(prompt: str, installed_agents: list[str] | None = None) -> str:
    """Classify a task prompt and recommend an agent.

    Uses OpenRouter (high-powered free models) for classification — this
    benefits from real intelligence. Falls back to Qwen 4B then "claude".
    """

    agents = installed_agents or ["pi", "claude"]
    use_multi = len(agents) > 2

    if use_multi:
        agent_list = ", ".join(f"'{a}'" for a in agents)
        system_msg = (
            f"Classify this task to one of these agents: {agent_list}.\n"
            "pi = mechanical: run a command, edit a specific line, "
            "add a comment, format files, simple find-and-replace.\n"
            "claude = analytical: debug why something fails, read and "
            "understand complex code, refactor architecture, fix flaky "
            "tests, multi-file reasoning, rework/redesign a system.\n"
            "codex = batch code changes, large-scale refactors, "
            "tasks that benefit from parallel execution.\n"
            "gemini = research, summarization, documentation tasks.\n"
            f"Reply with ONLY one of: {agent_list}. Nothing else."
        )
    else:
        system_msg = (
            "Classify this task as either 'pi' or 'claude'.\n"
            "pi = mechanical: run a command, edit a specific line, "
            "add a comment, format files, simple find-and-replace.\n"
            "claude = analytical: debug why something fails, read and "
            "understand complex code, refactor architecture, fix flaky "
            "tests, multi-file reasoning, rework/redesign a system, "
            "add a new feature with multiple moving parts, "
            "anything involving scheduler.py or panes.py.\n"
            "Reply with ONLY 'pi' or 'claude', nothing else."
        )

    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt[:300]},
    ]

    try:
        from dgov.openrouter import chat_completion

        result = chat_completion(messages, max_tokens=5, temperature=0)
        answer = result["choices"][0]["message"]["content"].strip().lower()
        # Match against known agents
        for agent in agents:
            if agent in answer:
                return agent
        return "claude"
    except RuntimeError:
        return "claude"


def extract_task_context(prompt: str) -> dict:
    """Extract relevant files and hints from a task prompt via keyword matching.

    Returns a dict with keys: primary_files, also_check, tests, hints.
    All values are lists. Empty lists if nothing matches.
    """
    p = prompt.lower()

    primary_files: list[str] = []
    also_check: list[str] = []
    tests: list[str] = []
    hints: list[str] = []

    # -- Merge / review --
    if any(kw in p for kw in ("merge", "review", "conflict", "verdict")):
        primary_files.extend(["src/dgov/merger.py", "src/dgov/inspection.py"])
        also_check.append("src/dgov/persistence.py")
        tests.extend(
            [
                "tests/test_merger_coverage.py",
                "tests/test_merger_conflicts.py",
                "tests/test_dgov_merger.py",
            ]
        )
        hints.append("Run post-merge lint and related tests after changes.")

    # -- Retry / escalation / recovery --
    if any(kw in p for kw in ("retry", "escalat", "recovery", "bounded retry")):
        primary_files.append("src/dgov/recovery.py")
        also_check.extend(["src/dgov/responder.py", "src/dgov/monitor.py"])
        tests.extend(
            [
                "tests/test_retry.py",
                "tests/test_bounded_retry.py",
                "tests/test_recovery_dogfood.py",
            ]
        )
        hints.append("Check monitor auto-retry hooks.")

    # -- Monitor daemon --
    if any(kw in p for kw in ("monitor", "daemon", "auto-merge", "auto-retry")):
        primary_files.append("src/dgov/monitor.py")
        also_check.extend(["src/dgov/monitor_hooks.py", "src/dgov/recovery.py"])
        tests.append("tests/test_monitor.py")
        hints.append("Monitor hooks are TOML-configured.")

    # -- Worker complete / done --
    if any(kw in p for kw in ("worker complete", "worker done", "done signal", "done strategy")):
        primary_files.extend(["src/dgov/cli/worker_cmd.py", "src/dgov/done.py"])
        also_check.append("src/dgov/waiter.py")
        tests.extend(["tests/test_done_strategy.py", "tests/test_dgov_panes.py"])

    # -- Lifecycle / pane create, close, resume --
    if any(kw in p for kw in ("pane create", "pane close", "pane resume", "cleanup", "lifecycle")):
        primary_files.append("src/dgov/lifecycle.py")
        also_check.extend(["src/dgov/done.py", "src/dgov/gitops.py"])
        tests.extend(["tests/test_lifecycle.py", "tests/test_dgov_panes.py"])

    # -- Agent routing / selection --
    if any(kw in p for kw in ("router", "agent routing", "agent selection", "resolve agent")):
        primary_files.extend(["src/dgov/router.py", "src/dgov/agents.py"])
        also_check.append("src/dgov/strategy.py")
        tests.extend(["tests/test_router.py", "tests/test_dgov_agents.py"])

    # -- Prompt templates --
    if any(kw in p for kw in ("template", "prompt template")):
        primary_files.extend(["src/dgov/templates.py", "src/dgov/strategy.py"])
        also_check.append("src/dgov/lifecycle.py")
        tests.append("tests/test_templates.py")

    # -- Dashboard / terrain TUI --
    if any(kw in p for kw in ("dashboard", "terrain", "tui", "spim")):
        primary_files.extend(["src/dgov/dashboard.py", "src/dgov/terrain.py"])
        also_check.append("src/dgov/terrain_pane.py")
        tests.extend(["tests/test_dashboard.py", "tests/test_terrain_events.py"])

    # -- DAG / batch / mission --
    if any(kw in p for kw in ("dag", "batch", "mission")):
        primary_files.extend(["src/dgov/dag.py", "src/dgov/batch.py", "src/dgov/mission.py"])
        also_check.extend(["src/dgov/dag_parser.py", "src/dgov/dag_graph.py"])
        tests.extend(["tests/test_dag.py", "tests/test_batch.py", "tests/test_mission.py"])

    # -- Persistence / state DB --
    if any(kw in p for kw in ("persistence", "state db", "event journal", "sqlite")):
        primary_files.append("src/dgov/persistence.py")
        also_check.append("src/dgov/status.py")
        tests.extend(["tests/test_dgov_state.py", "tests/test_persistence_pane.py"])

    # -- CLI commands --
    if any(kw in p for kw in ("cli command", "cli subcommand", "add_command", "cli init")):
        primary_files.append("src/dgov/cli/__init__.py")
        also_check.append("src/dgov/cli/pane.py")
        tests.extend(["tests/test_cli_admin.py", "tests/test_dgov_cli.py"])
        hints.append("Register top-level commands in cli/__init__.py after imports.")

    # -- Preflight / doctor --
    if any(kw in p for kw in ("preflight", "doctor", "health check")):
        primary_files.extend(["src/dgov/preflight.py", "src/dgov/cli/admin.py"])
        also_check.append("src/dgov/agents.py")
        tests.extend(["tests/test_dgov_preflight.py", "tests/test_init_doctor.py"])

    return {
        "primary_files": list(dict.fromkeys(primary_files)),
        "also_check": list(dict.fromkeys(also_check)),
        "tests": list(dict.fromkeys(tests)),
        "hints": list(dict.fromkeys(hints)),
    }


def _structure_pi_prompt(raw_prompt: str, files: list[str] | None = None) -> str:
    """Wrap a raw task description into pi's numbered-step format.

    Takes a freeform prompt and returns a structured prompt with:
    1. Read instructions for mentioned files
    2. The original task description
    3. Lint step
    4. Explicit git add + git commit steps

    If files are provided, they're used for the read/add steps.
    If not, extract file paths from the prompt text.
    """
    if files is None:
        # Extract file paths from prompt
        # Patterns: src/..., tests/..., or anything with an extension
        matches = re.findall(r"\b(?:src/|tests/)[\w\-\./]+|[\w\-\./]+\.\w+", raw_prompt)
        files = []
        seen = set()
        for f in matches:
            f = f.strip("./")
            if f and f not in seen and ("/" in f or "." in f):
                # Avoid matching things like "3.5" or "1.2.3"
                if not re.match(r"^\d+(\.\d+)+$", f):
                    files.append(f)
                    seen.add(f)

    steps = []
    step_num = 1

    # 1. Read steps
    if files:
        for f in files:
            steps.append(f"{step_num}. Read {f}")
            step_num += 1

    # 2. Original task
    steps.append(f"{step_num}. {raw_prompt.strip()}")
    step_num += 1

    # 3. Lint step
    if files:
        py_files = [f for f in files if f.endswith(".py")]
        if py_files:
            steps.append(f"{step_num}. Run: uv run ruff check {' '.join(py_files)}")
            step_num += 1

    # 4. git add
    if files:
        steps.append(f"{step_num}. git add {' '.join(files)}")
        step_num += 1

    # 5. git commit
    # Infer commit message from first line of prompt
    first_line = raw_prompt.strip().split("\n")[0]
    commit_msg = first_line[:50].strip().rstrip(".")
    if not commit_msg:
        commit_msg = "Worker changes"

    # Use double quotes for the commit message in the step text
    steps.append(f'{step_num}. git commit -m "{commit_msg}"')

    return "\n".join(steps)


# -- Slug validation --

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")


def _validate_slug(slug: str) -> str:
    """Validate slug for safe use in file paths and shell commands."""
    if not _SLUG_RE.match(slug):
        raise ValueError(
            f"Invalid slug: {slug!r}. "
            "Must be 1-50 chars, lowercase alphanumeric and hyphens, "
            "starting with alphanumeric."
        )
    return slug


# -- Slug generation --


def _generate_slug(prompt: str, max_words: int = 4) -> str:
    """Generate a descriptive kebab-case slug from prompt words."""
    # Strip absolute path segments (e.g. /Users/jake/...) and keep only the tail
    prompt_tail = re.sub(r"/\S+/", " ", prompt)
    words = re.sub(r"[^a-z0-9\s]", " ", prompt_tail.lower()).split()
    noise = {
        "the",
        "a",
        "an",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "and",
        "or",
        "is",
        "it",
        "read",
        "run",
        "git",
        "add",
        "commit",
        "pytest",
        "ruff",
        "uv",
        "file",
        "files",
        "path",
        "paths",
    }
    content = []
    for w in words:
        if w in noise:
            continue
        if w.isdigit():
            continue
        content.append(w)
        if len(content) >= max_words:
            break
    slug = "-".join(content) if content else f"task-{int(time.time())}"
    slug = slug[:50]
    # Ensure generated slug passes validation (strip leading/trailing hyphens)
    slug = slug.strip("-") or f"task-{int(time.time())}"
    return slug
