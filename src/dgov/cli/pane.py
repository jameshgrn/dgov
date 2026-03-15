"""Pane management commands."""

from __future__ import annotations

import json
import os
import sys

import click

from dgov.cli import SESSION_ROOT_OPTION


def _fmt_duration(seconds: int) -> str:
    from dgov.dashboard import fmt_duration

    return fmt_duration(seconds)


@click.group()
def pane():
    """Manage worker panes."""


@pane.command("util")
@click.argument("command")
@click.option("--title", "-t", default=None, help="Pane title (defaults to command name)")
@click.option("--cwd", "-c", default=".", help="Working directory")
def pane_util(command, title, cwd):
    """Run a command in a utility pane (no worktree)."""
    from dgov.tmux import create_utility_pane

    title = title or command.split()[0]
    pane_id = create_utility_pane(command, f"[util] {title}", cwd=cwd)
    click.echo(json.dumps({"pane_id": pane_id, "command": command, "title": title}))


@pane.command("create")
@click.option("--agent", "-a", default=None, help="Agent CLI to launch (use 'auto' to classify)")
@click.option("--prompt", "-p", default=None, help="Task prompt for the agent")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option(
    "--permission-mode",
    "-m",
    default="bypassPermissions",
    help="Permission mode: plan, acceptEdits, bypassPermissions",
)
@click.option("--slug", "-s", default=None, help="Override auto-generated slug")
@click.option("--extra-flags", "-f", default="", help="Extra flags for the agent CLI")
@click.option(
    "--env",
    "-e",
    multiple=True,
    help="Environment variable as KEY=VALUE (repeatable)",
)
@click.option(
    "--preflight/--no-preflight", default=True, help="Run pre-flight checks (default: on)"
)
@click.option(
    "--fix/--no-fix", default=True, help="Auto-fix fixable preflight failures (default: on)"
)
@click.option(
    "--max-retries",
    default=None,
    type=int,
    help="Override agent max auto-retries for this pane (0=disable)",
)
@click.option(
    "--template",
    "-T",
    default=None,
    help="Use a prompt template by name",
)
@click.option(
    "--var",
    multiple=True,
    help="Template variable as key=value (repeatable)",
)
def pane_create(
    agent,
    prompt,
    project_root,
    session_root,
    permission_mode,
    slug,
    extra_flags,
    env,
    preflight,
    fix,
    max_retries,
    template,
    var,
):
    """Create a worker pane: worktree + tmux + agent."""
    from dgov.agents import get_default_agent, load_registry
    from dgov.lifecycle import create_worker_pane
    from dgov.strategy import classify_task

    registry = load_registry(project_root)
    skip_auto_structure = False

    if template:
        from dgov.templates import load_templates, render_template

        session_root_abs = os.path.abspath(session_root or project_root)
        templates = load_templates(session_root_abs)
        if template not in templates:
            click.echo(
                f"Unknown template: {template}. Available: {', '.join(templates)}", err=True
            )
            sys.exit(1)
        tpl = templates[template]

        template_vars = {}
        for item in var:
            if "=" not in item:
                click.echo(f"Invalid var (need key=value): {item}", err=True)
                sys.exit(1)
            k, v = item.split("=", 1)
            template_vars[k] = v

        try:
            prompt = render_template(tpl, template_vars)
        except ValueError as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)

        if agent is None:
            agent = tpl.default_agent or get_default_agent(registry)
        skip_auto_structure = True
    elif prompt is None:
        click.echo("Either --prompt or --template is required.", err=True)
        sys.exit(1)

    if prompt is not None and not prompt.strip():
        click.echo("Prompt cannot be empty.", err=True)
        sys.exit(1)

    if agent is None:
        agent = get_default_agent(registry)

    if agent == "auto":
        agent = classify_task(prompt)
        click.echo(json.dumps({"auto_classified": agent}), err=True)

    if agent not in registry:
        click.echo(f"Unknown agent: {agent}. Available: {', '.join(registry)}", err=True)
        sys.exit(1)

    if preflight:
        from dgov.preflight import fix_preflight, run_preflight

        report = run_preflight(
            project_root=project_root,
            agent=agent,
            session_root=session_root,
        )
        if not report.passed and fix:
            report = fix_preflight(report, project_root)
        if not report.passed:
            click.echo(json.dumps(report.to_dict(), indent=2), err=True)
            sys.exit(1)

    env_vars = {}
    for item in env:
        if "=" not in item:
            click.echo(f"Invalid env var (need KEY=VALUE): {item}", err=True)
            sys.exit(1)
        k, v = item.split("=", 1)
        env_vars[k] = v

    try:
        pane_obj = create_worker_pane(
            project_root=project_root,
            prompt=prompt,
            agent=agent,
            permission_mode=permission_mode,
            slug=slug,
            env_vars=env_vars if env_vars else None,
            extra_flags=extra_flags,
            session_root=session_root,
            skip_auto_structure=skip_auto_structure,
        )
    except ValueError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    # Store per-pane max_retries override in metadata
    if max_retries is not None:
        from dgov.persistence import set_pane_metadata

        session_root_abs = os.path.abspath(session_root or project_root)
        set_pane_metadata(session_root_abs, pane_obj.slug, max_retries=max_retries)

    result = {
        "slug": pane_obj.slug,
        "pane_id": pane_obj.pane_id,
        "agent": pane_obj.agent,
        "worktree": pane_obj.worktree_path,
        "branch": pane_obj.branch_name,
    }
    if max_retries is not None:
        result["max_retries"] = max_retries
    click.echo(json.dumps(result, indent=2))


@pane.command("close")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--force", "-f", is_flag=True, help="Remove worktree even if dirty")
def pane_close(slug, project_root, session_root, force):
    """Close a worker pane: kill tmux pane, remove worktree."""
    from dgov.lifecycle import close_worker_pane

    if close_worker_pane(project_root, slug, session_root=session_root, force=force):
        click.echo(json.dumps({"closed": slug}))
    else:
        click.echo(json.dumps({"error": f"Pane not found: {slug}"}), err=True)
        sys.exit(1)


@pane.command("merge")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option(
    "--resolve",
    type=click.Choice(["skip", "agent", "manual"]),
    default="skip",
    help="Conflict resolution: skip (error), agent (auto-resolve), manual (markers)",
)
@click.option(
    "--squash/--no-squash",
    default=True,
    help="Squash worker commits into one (default: squash)",
)
def pane_merge(slug, project_root, session_root, resolve, squash):
    """Merge a branch into main with configurable conflict resolution.

    Merge the worktree branch for the given pane.
    """
    from dgov.merger import merge_worker_pane

    result = merge_worker_pane(
        project_root, slug, session_root=session_root, resolve=resolve, squash=squash
    )

    click.echo(json.dumps(result, indent=2))

    if "error" in result:
        sys.exit(1)


@pane.command("wait")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--timeout", "-t", default=600, help="Max seconds to wait (0=forever)")
@click.option("--poll", "-i", default=3, help="Poll interval in seconds")
@click.option("--stable", "-s", default=15, help="Seconds of stable output before declaring done")
@click.option(
    "--auto-retry/--no-auto-retry",
    default=True,
    help="Auto-retry failed panes per agent retry policy (default: on)",
)
def pane_wait(slug, project_root, session_root, timeout, poll, stable, auto_retry):
    """Wait for a worker pane to finish.

    Three detection modes (checked each poll cycle, first wins):
    1. Done-signal file (agents that exit cleanly).
    2. New commits on the worker branch beyond base_sha.
    3. Output stabilization (TUI agents that stay open).
    """
    from dgov.status import list_worker_panes
    from dgov.waiter import PaneTimeoutError, wait_worker_pane

    panes = list_worker_panes(project_root, session_root=session_root)
    if not any(p.get("slug") == slug for p in panes):
        click.echo(json.dumps({"error": f"Pane not found: {slug}"}), err=True)
        sys.exit(1)

    try:
        result = wait_worker_pane(
            project_root,
            slug,
            session_root=session_root,
            timeout=timeout,
            poll=poll,
            stable=stable,
            auto_retry=auto_retry,
        )
        click.echo(json.dumps(result))
    except PaneTimeoutError as exc:
        timeout_result = {
            "error": f"Timeout after {exc.timeout}s",
            "slug": exc.slug,
            "agent": exc.agent,
        }
        if exc.agent == "pi":
            timeout_result["suggest_escalate"] = True
        click.echo(json.dumps(timeout_result), err=True)
        sys.exit(1)


@pane.command("wait-all")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--timeout", "-t", default=600, help="Max seconds to wait (0=forever)")
@click.option("--poll", "-i", default=3, help="Poll interval in seconds")
@click.option("--stable", "-s", default=15, help="Seconds of stable output before declaring done")
def pane_wait_all(project_root, session_root, timeout, poll, stable):
    """Wait for ALL worker panes to finish. Prints each as it completes."""
    from dgov.status import list_worker_panes
    from dgov.waiter import PaneTimeoutError, wait_all_worker_panes

    session_root_abs = os.path.abspath(session_root or project_root)
    panes = list_worker_panes(project_root, session_root=session_root_abs)
    pending = {p["slug"] for p in panes if not p["done"]}
    if not pending:
        click.echo(json.dumps({"done": "all", "count": 0}))
        return

    try:
        count = 0
        for result in wait_all_worker_panes(
            project_root,
            session_root=session_root,
            timeout=timeout,
            poll=poll,
            stable=stable,
        ):
            click.echo(json.dumps(result))
            count += 1
        click.echo(json.dumps({"done": "all", "count": count}))
    except PaneTimeoutError as exc:
        for p in exc.pending_panes:
            timeout_result = {
                "error": f"Timeout after {exc.timeout}s",
                "slug": p["slug"],
                "agent": p["agent"],
            }
            if p["agent"] == "pi":
                timeout_result["suggest_escalate"] = True
            click.echo(json.dumps(timeout_result), err=True)
        sys.exit(1)


@pane.command("merge-all")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option(
    "--resolve",
    type=click.Choice(["skip", "agent", "manual"]),
    default="skip",
    help="Conflict resolution strategy",
)
@click.option(
    "--squash/--no-squash",
    default=True,
    help="Squash worker commits into one (default: squash)",
)
def pane_merge_all(project_root, session_root, resolve, squash):
    """Merge ALL done worker panes sequentially. Prints combined summary."""
    from dgov.merger import merge_worker_pane
    from dgov.status import list_worker_panes

    panes = list_worker_panes(project_root, session_root=session_root)
    done_panes = [p for p in panes if p["done"]]
    if not done_panes:
        click.echo(json.dumps({"merged": [], "skipped": "no done panes"}))
        return

    merge_fn = merge_worker_pane

    merged_slugs = []
    failed_slugs = []
    total_files = 0
    warnings = []

    for p in done_panes:
        slug = p["slug"]
        result = merge_fn(
            project_root, slug, session_root=session_root, resolve=resolve, squash=squash
        )
        if "merged" in result:
            merged_slugs.append(slug)
            total_files += result.get("files_changed", 0)
            if result.get("warning"):
                warnings.append(f"{slug}: {result['warning']}")
        else:
            failed_slugs.append(slug)
            err = result.get("error") or result.get("hint", "unknown")
            warnings.append(f"{slug}: {err}")

    summary = {
        "merged_count": len(merged_slugs),
        "failed_count": len(failed_slugs),
        "total_files_changed": total_files,
        "merged": merged_slugs,
    }
    if failed_slugs:
        summary["failed"] = failed_slugs
    if warnings:
        summary["warnings"] = warnings

    click.echo(json.dumps(summary, indent=2))
    if failed_slugs:
        sys.exit(1)


@pane.command("list")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show last output line")
def pane_list(project_root, session_root, as_json, verbose):
    """List all worker panes with live status."""
    from dgov.status import list_worker_panes

    panes = list_worker_panes(project_root, session_root=session_root)

    if as_json or os.environ.get("DGOV_JSON"):
        click.echo(json.dumps(panes, indent=2))
        return

    if not panes:
        click.echo("No panes.")
        return

    # Format as table
    header = (
        f"{'Slug':<20} {'Agent':<10} {'State':<10} {'Activity':<12} "
        f"{'Duration':<12} {'Prompt/Status'}"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for p in panes:
        slug = (p.get("slug", "") or "")[:19]
        agent = p.get("agent", "unknown") or "unknown"
        state = p.get("state", "active") or "active"
        activity = p.get("activity", "unknown") or "unknown"
        duration_s = int(p.get("duration_s", 0))
        duration = _fmt_duration(duration_s)
        last_output = (p.get("last_output", "") or "").strip()
        prompt = (p.get("prompt", "") or "")[:40]
        status_col = last_output if last_output else prompt
        row = f"{slug:<20} {agent:<10} {state:<10} {activity:<12} {duration:<12} {status_col}"
        click.echo(row)
        if verbose and last_output:
            click.echo(f"  └ {last_output}")


@pane.command("prune")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
def pane_prune(project_root, session_root):
    """Remove stale pane entries (dead pane + no worktree)."""
    from dgov.status import prune_stale_panes

    pruned = prune_stale_panes(project_root, session_root=session_root)
    click.echo(json.dumps({"pruned": pruned}))


@pane.command("classify")
@click.argument("prompt")
def pane_classify(prompt):
    """Classify a task and recommend an agent (OpenRouter or local Qwen 4B)."""
    from dgov.strategy import classify_task

    agent = classify_task(prompt)
    click.echo(json.dumps({"recommended_agent": agent, "prompt_preview": prompt[:80]}))


@pane.command("capture")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--lines", "-n", default=30, help="Number of lines to capture")
def pane_capture(slug, project_root, session_root, lines):
    """Capture the last N lines of a worker pane's output."""
    from dgov.status import capture_worker_output

    output = capture_worker_output(project_root, slug, lines, session_root=session_root)
    if output is None:
        click.echo(json.dumps({"error": f"Pane not found or dead: {slug}"}), err=True)
        sys.exit(1)
    click.echo(output)


@pane.command("review")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--full", is_flag=True, help="Show complete diff (not just stat)")
def pane_review(slug, project_root, session_root, full):
    """Preview a worker pane's changes before merging."""
    from dgov.inspection import review_worker_pane

    result = review_worker_pane(project_root, slug, session_root=session_root, full=full)
    click.echo(json.dumps(result, indent=2))
    if "error" in result:
        sys.exit(1)


@pane.command("diff")
@click.argument("slug")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
@click.option("--stat", is_flag=True, help="Show diffstat only")
@click.option("--name-only", is_flag=True, help="Show changed file names only")
def pane_diff(slug, project_root, session_root, stat, name_only):
    """Show diff for a worker pane's branch vs base."""
    from dgov.inspection import diff_worker_pane

    result = diff_worker_pane(
        project_root, slug, session_root=session_root, stat=stat, name_only=name_only
    )
    click.echo(json.dumps(result, indent=2))
    if "error" in result:
        sys.exit(1)


@pane.command("escalate")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default=None, help="Agent to escalate to")
@click.option(
    "--permission-mode",
    "-m",
    default="acceptEdits",
    help="Permission mode for the new agent",
)
def pane_escalate(slug, project_root, session_root, agent, permission_mode):
    """Re-dispatch to a stronger agent."""
    from dgov.agents import get_default_agent, load_registry
    from dgov.recovery import escalate_worker_pane

    if agent is None:
        agent = get_default_agent(load_registry(project_root))

    result = escalate_worker_pane(
        project_root,
        slug,
        target_agent=agent,
        session_root=session_root,
        permission_mode=permission_mode,
    )
    click.echo(json.dumps(result, indent=2))
    if "error" in result:
        sys.exit(1)


@pane.command("retry")
@click.argument("slug")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default=None, help="Override agent for retry")
@click.option("--prompt", "-p", default=None, help="Override prompt for retry")
@click.option("--permission-mode", "-m", default="acceptEdits", help="Permission mode")
def pane_retry(slug, project_root, session_root, agent, prompt, permission_mode):
    """Retry a failed pane with a new attempt."""
    from dgov.recovery import retry_worker_pane

    result = retry_worker_pane(
        project_root,
        slug,
        session_root=session_root,
        agent=agent,
        prompt=prompt,
        permission_mode=permission_mode,
    )
    click.echo(json.dumps(result, indent=2))
    if "error" in result:
        sys.exit(1)


@pane.command("resume")
@click.argument("slug")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default=None, help="Override agent")
@click.option("--prompt", "-p", default=None, help="Override prompt")
@click.option("--permission-mode", "-m", default="acceptEdits", help="Permission mode")
def pane_resume(slug, project_root, session_root, agent, prompt, permission_mode):
    """Re-launch agent in an existing worktree."""
    from dgov.lifecycle import resume_worker_pane

    result = resume_worker_pane(
        project_root=project_root,
        slug=slug,
        session_root=session_root,
        agent=agent,
        prompt=prompt,
        permission_mode=permission_mode,
    )
    click.echo(json.dumps(result, indent=2))
    if "error" in result:
        sys.exit(1)


@pane.command("logs")
@click.argument("slug")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
@click.option("--tail", "-n", default=None, type=int, help="Show last N lines")
def pane_logs(slug, project_root, session_root, tail):
    """Show persistent log for a pane."""
    session_root = os.path.abspath(session_root or project_root)
    log_file = os.path.join(session_root, ".dgov", "logs", f"{slug}.log")
    if not os.path.exists(log_file):
        click.echo(json.dumps({"error": f"No log file found: {log_file}"}), err=True)
        sys.exit(1)
    with open(log_file) as f:
        lines = f.readlines()
    if tail:
        lines = lines[-tail:]
    click.echo("".join(lines), nl=False)


@pane.command("respond")
@click.argument("slug")
@click.argument("message")
@SESSION_ROOT_OPTION
def pane_respond(slug, message, session_root):
    """Send a message to a worker pane via tmux send-keys."""
    from dgov.waiter import interact_with_pane

    session_root = os.path.abspath(session_root or ".")
    if interact_with_pane(session_root, slug, message):
        click.echo(json.dumps({"sent": True, "slug": slug}))
    else:
        click.echo(json.dumps({"error": f"Pane not found or dead: {slug}"}), err=True)
        sys.exit(1)


@pane.command("message")
@click.argument("slug")
@click.argument("text")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
def pane_message(slug, text, project_root, session_root):
    """Send a message to a running worker pane."""
    from dgov.backend import get_backend
    from dgov.persistence import get_pane

    session_root = os.path.abspath(session_root or project_root)
    pane = get_pane(session_root, slug)
    if not pane:
        click.echo(json.dumps({"error": f"Pane not found: {slug}"}))
        sys.exit(1)
    pane_id = pane.get("pane_id")
    if not pane_id or not get_backend().is_alive(pane_id):
        click.echo(json.dumps({"error": f"Pane {slug} is not running"}))
        sys.exit(1)
    get_backend().send_input(pane_id, text)
    click.echo(json.dumps({"sent": True, "slug": slug, "message": text[:100]}))


@pane.command("nudge")
@click.argument("slug")
@SESSION_ROOT_OPTION
@click.option("--wait", "-w", default=10, help="Seconds to wait for response")
def pane_nudge(slug, session_root, wait):
    """Nudge a worker: ask if done, parse YES/NO response."""
    from dgov.waiter import nudge_pane

    session_root = os.path.abspath(session_root or ".")
    result = nudge_pane(session_root, slug, wait_seconds=wait)
    click.echo(json.dumps(result))
    if result.get("response") == "error":
        sys.exit(1)


@pane.command("signal")
@click.argument("slug")
@click.argument("signal_type", type=click.Choice(["done", "failed"]))
@SESSION_ROOT_OPTION
def pane_signal(slug, signal_type, session_root):
    """Manually signal a pane as done or failed."""
    from dgov.waiter import signal_pane

    session_root = os.path.abspath(session_root or ".")
    if signal_pane(session_root, slug, signal_type):
        click.echo(json.dumps({"signaled": signal_type, "slug": slug}))
    else:
        click.echo(json.dumps({"error": f"Pane not found: {slug}"}), err=True)
        sys.exit(1)
