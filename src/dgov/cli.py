"""dgov CLI — programmatic pane management for the governor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from dgov.agents import detect_installed_agents

SESSION_ROOT_OPTION = click.option(
    "--session-root",
    "-S",
    default=None,
    help="Session root (where .dgov/ lives). Defaults to --project-root.",
)


def _check_governor_context() -> None:
    """Verify we're the governor: on main branch and not inside a worktree.

    Raises click.UsageError if either check fails.
    """
    if os.environ.get("DGOV_SKIP_GOVERNOR_CHECK") == "1":
        return

    try:
        git_dir = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if git_dir.returncode == 0 and "worktrees" in git_dir.stdout.strip():
            raise click.UsageError(
                "dgov is running inside a git worktree. "
                "The governor must run from the main repo, not a worker worktree."
            )
    except subprocess.TimeoutExpired:
        pass

    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if branch.returncode == 0:
            current = branch.stdout.strip()
            # Allow HEAD (detached/no commits yet) and main
            if current not in ("main", "HEAD"):
                raise click.UsageError(
                    f"Governor is on branch '{current}', but must stay on 'main'. "
                    f"Switch back with: git checkout main"
                )
    except subprocess.TimeoutExpired:
        pass


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """dgov: governor + worker pane orchestration."""
    # Skip the guard for info-only commands and the bare invocation
    if ctx.invoked_subcommand not in (
        None,
        "version",
        "agents",
        "blame",
        "checkpoint",
        "experiment",
        "template",
        "openrouter",
        "dashboard",
    ):
        _check_governor_context()

    if ctx.invoked_subcommand is not None:
        return

    # Bare `dgov` — launch or announce the governor session
    from dgov.tmux import style_dgov_session, style_governor_pane

    repo = Path.cwd().name
    session_name = f"dgov-{repo}"

    if os.environ.get("TMUX"):
        from dgov.art import print_banner

        style_dgov_session()
        # Style the current pane as governor
        pane_id = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if pane_id:
            style_governor_pane(pane_id)
        print_banner()
        click.echo(f"{repo} — governor ready")
    else:
        # Ensure the per-repo tmux session exists, then hand off
        exists = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
        )
        if exists.returncode != 0:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session_name],
                capture_output=True,
                check=True,
            )
        style_dgov_session(session_name)
        # Run dgov inside the session so the banner triggers via the TMUX branch
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "dgov", "Enter"],
            capture_output=True,
        )
        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])


@cli.group()
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


@pane.command("lazygit")
@click.option("--cwd", "-c", default=".", help="Working directory")
def pane_lazygit(cwd):
    """Launch lazygit in a utility pane."""
    from dgov.tmux import create_utility_pane

    pane_id = create_utility_pane("lazygit", "[util] lazygit", cwd=cwd)
    click.echo(json.dumps({"pane_id": pane_id, "command": "lazygit", "title": "lazygit"}))


@pane.command("yazi")
@click.option("--cwd", "-c", default=".", help="Working directory")
def pane_yazi(cwd):
    """Launch yazi in a utility pane."""
    from dgov.tmux import create_utility_pane

    pane_id = create_utility_pane("yazi", "[util] yazi", cwd=cwd)
    click.echo(json.dumps({"pane_id": pane_id, "command": "yazi", "title": "yazi"}))


@pane.command("htop")
@click.option("--cwd", "-c", default=".", help="Working directory")
def pane_htop(cwd):
    """Launch htop in a utility pane."""
    from dgov.tmux import create_utility_pane

    pane_id = create_utility_pane("htop", "[util] htop", cwd=cwd)
    click.echo(json.dumps({"pane_id": pane_id, "command": "htop", "title": "htop"}))


@pane.command("k9s")
@click.option("--cwd", "-c", default=".", help="Working directory")
def pane_k9s(cwd):
    """Launch k9s in a utility pane."""
    from dgov.tmux import create_utility_pane

    pane_id = create_utility_pane("k9s", "[util] k9s", cwd=cwd)
    click.echo(json.dumps({"pane_id": pane_id, "command": "k9s", "title": "k9s"}))


@pane.command("top")
@click.option("--cwd", "-c", default=".", help="Working directory")
def pane_top(cwd):
    """Launch btop in a utility pane."""
    from dgov.tmux import create_utility_pane

    pane_id = create_utility_pane("btop", "[util] btop", cwd=cwd)
    click.echo(json.dumps({"pane_id": pane_id, "command": "btop", "title": "btop"}))


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
    default="acceptEdits",
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
    from dgov.panes import create_worker_pane
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
        from dgov.persistence import _set_pane_metadata

        session_root_abs = os.path.abspath(session_root or project_root)
        _set_pane_metadata(session_root_abs, pane_obj.slug, max_retries=max_retries)

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
    from dgov.panes import close_worker_pane

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
    "--close/--no-close", default=True, help="Close worker pane after merge (default: on)"
)
@click.option(
    "--resolve",
    type=click.Choice(["agent", "manual"]),
    default="agent",
    help="Conflict resolution: agent (auto-resolve), manual (markers)",
)
def pane_merge(slug, project_root, session_root, close, resolve):
    """Merge a branch into main with configurable conflict resolution.

    Merge the worktree branch for the given pane. If --close is set,
    also close the worker pane after successful merge.
    """
    from dgov.merger import merge_worker_pane, merge_worker_pane_with_close

    if close:
        result = merge_worker_pane_with_close(
            project_root, slug, session_root=session_root, resolve=resolve
        )
    else:
        result = merge_worker_pane(project_root, slug, session_root=session_root, resolve=resolve)

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
    from dgov.panes import list_worker_panes
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
    from dgov.panes import list_worker_panes
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
    "--close/--no-close", default=True, help="Close worker panes after merge (default: on)"
)
@click.option(
    "--resolve",
    type=click.Choice(["agent", "manual"]),
    default="agent",
    help="Conflict resolution strategy",
)
def pane_merge_all(project_root, session_root, close, resolve):
    """Merge ALL done worker panes sequentially. Prints combined summary."""
    from dgov.merger import merge_worker_pane, merge_worker_pane_with_close
    from dgov.panes import list_worker_panes

    panes = list_worker_panes(project_root, session_root=session_root)
    done_panes = [p for p in panes if p["done"]]
    if not done_panes:
        click.echo(json.dumps({"merged": [], "skipped": "no done panes"}))
        return

    merge_fn = merge_worker_pane_with_close if close else merge_worker_pane

    merged_slugs = []
    closed_slugs = []
    failed_slugs = []
    total_files = 0
    warnings = []

    for p in done_panes:
        slug = p["slug"]
        result = merge_fn(project_root, slug, session_root=session_root, resolve=resolve)
        if "merged" in result:
            merged_slugs.append(slug)
            if close:
                closed_slugs.append(slug)
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
    if closed_slugs:
        summary["closed"] = closed_slugs
    if failed_slugs:
        summary["failed"] = failed_slugs
    if warnings:
        summary["warnings"] = warnings

    click.echo(json.dumps(summary, indent=2))
    if failed_slugs:
        sys.exit(1)


def _fmt_duration(seconds: int) -> str:
    """Format duration in human-readable format."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m}m"


@pane.command("list")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
def pane_list(project_root, session_root, as_json):
    """List all worker panes with live status."""
    from dgov.panes import list_worker_panes

    panes = list_worker_panes(project_root, session_root=session_root)

    if as_json:
        click.echo(json.dumps(panes, indent=2))
        return

    if not sys.stdout.isatty():
        click.echo("hint: use --json for machine-readable output", err=True)

    if not panes:
        click.echo("No panes.")
        return

    # Format as table
    header = (
        f"{'Slug':<20} {'Agent':<10} {'State':<10} {'Alive':<6} "
        f"{'Done':<5} {'Freshness':<8} {'Duration':<12} {'Prompt'}"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for p in panes:
        slug = (p.get("slug", "") or "")[:19]
        agent = p.get("agent", "unknown") or "unknown"
        state = p.get("state", "active") or "active"
        alive = "✓" if p.get("alive") else "✗"
        done = "✓" if p.get("done") else "✗"
        freshness = p.get("freshness", "unknown") or "unknown"
        duration_s = int(p.get("duration_s", 0))
        duration = _fmt_duration(duration_s)
        prompt = (p.get("prompt", "") or "")[:40]
        row = (
            f"{slug:<20} {agent:<10} {state:<10} {alive:<6} "
            f"{done:<5} {freshness:<8} {duration:<12} {prompt}"
        )
        click.echo(row)


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
    from dgov.panes import prune_stale_panes

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
    from dgov.panes import capture_worker_output

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
    from dgov.panes import review_worker_pane

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
    from dgov.panes import diff_worker_pane

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
    from dgov.panes import escalate_worker_pane

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
    from dgov.panes import retry_worker_pane

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
    from dgov.panes import resume_worker_pane

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
    import os

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


@pane.command("interact")
@click.argument("slug")
@click.argument("message")
@SESSION_ROOT_OPTION
def pane_interact(slug, message, session_root):
    """Send a message to a worker pane via tmux send-keys."""
    from dgov.waiter import interact_with_pane

    session_root = os.path.abspath(session_root or ".")
    if interact_with_pane(session_root, slug, message):
        click.echo(json.dumps({"sent": True, "slug": slug}))
    else:
        click.echo(json.dumps({"error": f"Pane not found or dead: {slug}"}), err=True)
        sys.exit(1)


@pane.command("respond")
@click.argument("slug")
@click.argument("message")
@SESSION_ROOT_OPTION
def pane_respond(slug, message, session_root):
    """Send a response to a worker pane (alias for interact)."""
    from dgov.waiter import interact_with_pane

    session_root = os.path.abspath(session_root or ".")
    if interact_with_pane(session_root, slug, message):
        click.echo(json.dumps({"sent": True, "slug": slug}))
    else:
        click.echo(json.dumps({"error": f"Pane not found or dead: {slug}"}), err=True)
        sys.exit(1)


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


@cli.command("preflight")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default=None, help="Agent to validate for")
@click.option("--fix", is_flag=True, help="Auto-fix fixable failures")
@click.option(
    "--touches",
    "-t",
    multiple=True,
    help="Files the task will touch (repeatable)",
)
@click.option("--branch", "-b", default=None, help="Expected branch name")
def preflight_cmd(project_root, session_root, agent, fix, touches, branch):
    """Run pre-flight checks before dispatch."""
    from dgov.agents import get_default_agent, load_registry

    if agent is None:
        agent = get_default_agent(load_registry(project_root))
    from dgov.preflight import fix_preflight, run_preflight

    report = run_preflight(
        project_root=project_root,
        agent=agent,
        touches=list(touches) if touches else None,
        expected_branch=branch,
        session_root=session_root,
    )
    if not report.passed and fix:
        report = fix_preflight(report, project_root)

    click.echo(json.dumps(report.to_dict(), indent=2))
    if not report.passed:
        sys.exit(1)


@cli.command("status")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
def status(project_root, session_root):
    """Get full dgov status as JSON."""
    from dgov.state import get_status

    click.echo(json.dumps(get_status(project_root, session_root=session_root), indent=2))


@cli.command("rebase")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@click.option(
    "--onto",
    default=None,
    help="Explicit base branch to rebase onto (default: auto-detect upstream or main)",
)
def rebase(project_root, onto):
    """Rebase the governor worktree onto its base branch.

    Stashes dirty changes, rebases onto upstream (or main), and pops stash.
    On conflict: aborts rebase and restores working tree.
    """
    from dgov.panes import rebase_governor

    result = rebase_governor(project_root, onto=onto)
    click.echo(json.dumps(result, indent=2))
    if not result.get("rebased"):
        sys.exit(1)


@cli.command("blame")
@click.argument("file_path")
@click.option("--project-root", "-r", default=".", help="Project root")
@click.option("--session-root", "-R", default=None, help="Session root")
@click.option("--all", "-a", "show_all", is_flag=True, default=False, help="Show full history")
@click.option("--agent", default=None, help="Filter by agent")
@click.option("--line-level", is_flag=True, default=False, help="Show line-level blame")
@click.option("--lines", "-L", default=None, help="Line range for line-level blame (e.g. 10-20)")
def blame(file_path, project_root, session_root, show_all, agent, line_level, lines):
    """Show which agent/pane last touched a file."""
    if lines or line_level:
        from dgov.blame import blame_lines

        start_line = None
        end_line = None
        if lines:
            parts = lines.split("-", 1)
            try:
                start_line = int(parts[0])
                if len(parts) > 1:
                    end_line = int(parts[1])
                else:
                    end_line = start_line
            except ValueError:
                click.echo(f"Invalid line range: {lines} (expected N or N-M)", err=True)
                sys.exit(1)

        result = blame_lines(
            project_root=project_root,
            file_path=file_path,
            session_root=session_root,
            start_line=start_line,
            end_line=end_line,
            agent_filter=agent,
        )
    else:
        from dgov.blame import blame_file

        result = blame_file(
            project_root=project_root,
            file_path=file_path,
            session_root=session_root,
            last_only=not show_all,
            agent_filter=agent,
        )
    click.echo(json.dumps(result, indent=2))


@cli.command("agents")
@click.option("--project-root", "-r", default=".", help="Project root for registry loading")
def list_agents(project_root):
    """List available agents and which are installed."""
    from dgov.agents import load_registry

    registry = load_registry(project_root)
    installed = set(detect_installed_agents(registry))
    agents = []
    for agent_id, defn in registry.items():
        entry = {
            "id": agent_id,
            "name": defn.name,
            "installed": agent_id in installed,
            "transport": defn.prompt_transport,
            "source": defn.source,
        }
        if defn.health_check:
            hc = subprocess.run(defn.health_check, shell=True, capture_output=True, text=True)
            entry["healthy"] = hc.returncode == 0
        agents.append(entry)
    click.echo(json.dumps(agents, indent=2))


@cli.command("version")
def version_cmd():
    """Show dgov version."""
    from dgov import __version__

    result = {"dgov": __version__}
    click.echo(json.dumps(result, indent=2))


@cli.command("dashboard")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Project root",
)
@SESSION_ROOT_OPTION
@click.option("--refresh", default=2, type=float, help="Refresh interval in seconds")
def dashboard(project_root, session_root, refresh):
    """Launch live terminal dashboard."""
    from dgov.dashboard import run_dashboard

    run_dashboard(
        project_root=project_root,
        session_root=session_root,
        refresh_interval=refresh,
    )


@cli.group()
def template():
    """Manage prompt templates."""


@template.command("list")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
def template_list(project_root, session_root):
    """List all available templates (built-in + user)."""
    from dgov.templates import list_templates

    session_root_abs = os.path.abspath(session_root or project_root)
    click.echo(json.dumps(list_templates(session_root_abs), indent=2))


@template.command("show")
@click.argument("name")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
def template_show(name, project_root, session_root):
    """Show template details and required variables."""
    from dgov.templates import load_templates

    session_root_abs = os.path.abspath(session_root or project_root)
    templates = load_templates(session_root_abs)
    if name not in templates:
        click.echo(f"Unknown template: {name}. Available: {', '.join(templates)}", err=True)
        sys.exit(1)
    tpl = templates[name]
    click.echo(
        json.dumps(
            {
                "name": tpl.name,
                "description": tpl.description,
                "template": tpl.template,
                "required_vars": tpl.required_vars,
                "default_agent": tpl.default_agent,
            },
            indent=2,
        )
    )


@template.command("create")
@click.argument("name")
def template_create(name):
    """Create a new template file in .dgov/templates/."""
    session_root = os.path.abspath(".")
    templates_dir = Path(session_root) / ".dgov" / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    out_path = templates_dir / f"{name}.toml"
    if out_path.exists():
        click.echo(f"Template already exists: {out_path}", err=True)
        sys.exit(1)
    content = (
        f'name = "{name}"\n'
        'description = ""\n'
        'template = "Do {{thing}} in {{file}}. Commit."\n'
        'required_vars = ["thing", "file"]\n'
        'default_agent = "pi"\n'
    )
    out_path.write_text(content)
    click.echo(json.dumps({"created": str(out_path)}))


@cli.group()
def checkpoint():
    """Manage state checkpoints."""


@checkpoint.command("create")
@click.argument("name")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
def checkpoint_create(name, project_root, session_root):
    """Create a named checkpoint of current state."""
    from dgov.batch import create_checkpoint

    result = create_checkpoint(project_root, name, session_root=session_root)
    click.echo(json.dumps(result, indent=2))


@checkpoint.command("list")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
def checkpoint_list(project_root, session_root):
    """List all checkpoints."""
    from dgov.batch import list_checkpoints

    session_root = os.path.abspath(session_root or project_root)
    result = list_checkpoints(session_root)
    click.echo(json.dumps(result, indent=2))


@cli.command("batch")
@click.argument("spec_path", type=click.Path(exists=True))
@SESSION_ROOT_OPTION
@click.option("--dry-run", is_flag=True, help="Show computed tiers without executing")
def batch(spec_path, session_root, dry_run):
    """Execute a batch spec with DAG-ordered parallelism."""
    from dgov.batch import run_batch

    result = run_batch(spec_path, session_root=session_root, dry_run=dry_run)
    click.echo(json.dumps(result, indent=2))
    if result.get("failed"):
        sys.exit(1)


@cli.group()
def experiment():
    """Manage experiment loops."""


@experiment.command("start")
@click.option(
    "--program", "-p", required=True, type=click.Path(exists=True), help="Program file (markdown)"
)
@click.option("--metric", "-m", required=True, help="Metric name to optimize")
@click.option("--budget", "-b", default=5, help="Max experiments to run")
@click.option("--agent", "-a", default=None, help="Agent to use")
@click.option("--direction", "-d", type=click.Choice(["minimize", "maximize"]), default="minimize")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
@click.option("--timeout", "-t", default=600, help="Timeout per experiment in seconds")
@click.option("--dry-run", is_flag=True, help="Show plan without executing")
def experiment_start(
    program, metric, budget, agent, direction, project_root, session_root, timeout, dry_run
):
    """Run an experiment loop."""
    from dgov.agents import get_default_agent, load_registry
    from dgov.experiment import run_experiment_loop

    if agent is None:
        agent = get_default_agent(load_registry(project_root))

    session_root_abs = os.path.abspath(session_root or project_root)

    if dry_run:
        program_name = Path(program).stem
        click.echo(
            json.dumps(
                {
                    "dry_run": True,
                    "program": program,
                    "program_name": program_name,
                    "metric": metric,
                    "budget": budget,
                    "agent": agent,
                    "direction": direction,
                },
                indent=2,
            )
        )
        return

    for result in run_experiment_loop(
        project_root=project_root,
        program_path=program,
        metric_name=metric,
        budget=budget,
        agent=agent,
        direction=direction,
        session_root=session_root_abs,
        timeout=timeout,
    ):
        if isinstance(result, dict):
            click.echo(json.dumps(result))


@experiment.command("log")
@click.option("--program", "-p", required=True, help="Program name (stem of the program file)")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
def experiment_log(program, project_root, session_root):
    """Show the experiment log as JSON."""
    from dgov.experiment import ExperimentLog

    session_root_abs = os.path.abspath(session_root or project_root)
    log = ExperimentLog(session_root_abs, program)
    if not log.path.exists():
        click.echo(
            json.dumps({"warning": f"No experiment log found for program: {program}"}), err=True
        )
    entries = log.read_log()
    click.echo(json.dumps(entries, indent=2))


@experiment.command("summary")
@click.option("--program", "-p", required=True, help="Program name (stem of the program file)")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
@click.option("--direction", "-d", type=click.Choice(["minimize", "maximize"]), default="minimize")
def experiment_summary(program, project_root, session_root, direction):
    """Show summary stats for an experiment program."""
    from dgov.experiment import ExperimentLog

    session_root_abs = os.path.abspath(session_root or project_root)
    log = ExperimentLog(session_root_abs, program)
    if not log.path.exists():
        click.echo(
            json.dumps({"warning": f"No experiment log found for program: {program}"}), err=True
        )
    click.echo(json.dumps(log.summary(direction), indent=2))


@cli.command("review-fix")
@click.option(
    "--targets", "-t", required=True, multiple=True, help="File/directory paths to review"
)
@click.option("--review-agent", default=None, help="Agent for review phase")
@click.option("--fix-agent", default=None, help="Agent for fix phase")
@click.option(
    "--auto-approve", is_flag=True, default=False, help="Proceed to fix phase automatically"
)
@click.option(
    "--severity",
    type=click.Choice(["critical", "medium", "low"]),
    default="medium",
    help="Severity threshold (critical=only critical, medium=critical+medium, low=all)",
)
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
@click.option("--timeout", default=600, help="Timeout per phase in seconds")
def review_fix(
    targets, review_agent, fix_agent, auto_approve, severity, project_root, session_root, timeout
):
    """Run review-then-fix pipeline: review targets, collect findings, optionally fix."""
    from dgov.agents import get_default_agent, load_registry
    from dgov.review_fix import run_review_fix_pipeline

    if review_agent is None or fix_agent is None:
        default = get_default_agent(load_registry(project_root))
        review_agent = review_agent or default
        fix_agent = fix_agent or default

    result = run_review_fix_pipeline(
        project_root=project_root,
        targets=list(targets),
        review_agent=review_agent,
        fix_agent=fix_agent,
        session_root=session_root,
        auto_approve=auto_approve,
        severity_threshold=severity,
        timeout=timeout,
    )
    click.echo(json.dumps(result, indent=2))
    if result.get("failed_count", 0) > 0:
        sys.exit(1)


@cli.group()
def openrouter():
    """Manage OpenRouter LLM integration."""


@openrouter.command("status")
def openrouter_status():
    """Show API key status, default model, and connectivity."""
    from dgov.openrouter import check_status

    click.echo(json.dumps(check_status(), indent=2))


@openrouter.command("models")
def openrouter_models():
    """List available free models on OpenRouter."""
    from dgov.openrouter import list_free_models

    try:
        models = list_free_models()
        click.echo(json.dumps(models, indent=2))
    except Exception as exc:
        click.echo(json.dumps({"error": str(exc)}), err=True)
        sys.exit(1)


@openrouter.command("test")
@click.option("--prompt", "-p", default="Say hello in one word.", help="Test prompt")
@click.option("--model", "-m", default=None, help="Model to use")
def openrouter_test(prompt, model):
    """Send a test prompt and show the response."""
    from dgov.openrouter import chat_completion

    try:
        result = chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=50,
            temperature=0,
        )
        answer = result["choices"][0]["message"]["content"].strip()
        click.echo(json.dumps({"response": answer, "model": result.get("model", "unknown")}))
    except Exception as exc:
        click.echo(json.dumps({"error": str(exc)}), err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()
