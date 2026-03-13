"""dgov CLI — programmatic pane management for the governor."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from dgov.agents import AGENT_REGISTRY, detect_installed_agents

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
    if ctx.invoked_subcommand not in (None, "version", "agents", "checkpoint"):
        _check_governor_context()

    if ctx.invoked_subcommand is not None:
        return

    # Bare `dgov` — launch or announce the governor session
    from dgov.tmux import style_dgov_session, style_governor_pane

    repo = Path.cwd().name
    session_name = f"dgov-{repo}"

    if os.environ.get("TMUX"):
        style_dgov_session()
        # Style the current pane as governor
        pane_id = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_id}"],
            capture_output=True,
            text=True,
        ).stdout.strip()
        if pane_id:
            style_governor_pane(pane_id)
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
        os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])


@cli.group()
def pane():
    """Manage worker panes."""


@pane.command("create")
@click.option(
    "--agent", "-a", default="claude", help="Agent CLI to launch (use 'auto' to classify)"
)
@click.option("--prompt", "-p", required=True, help="Task prompt for the agent")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Git repo root for the worktree",
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
):
    """Create a worker pane: worktree + tmux + agent."""
    from dgov.panes import classify_task, create_worker_pane

    if agent == "auto":
        agent = classify_task(prompt)
        click.echo(json.dumps({"auto_classified": agent}), err=True)

    if agent not in AGENT_REGISTRY:
        click.echo(f"Unknown agent: {agent}. Available: {', '.join(AGENT_REGISTRY)}", err=True)
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

    pane_obj = create_worker_pane(
        project_root=project_root,
        prompt=prompt,
        agent=agent,
        permission_mode=permission_mode,
        slug=slug,
        env_vars=env_vars if env_vars else None,
        extra_flags=extra_flags,
        session_root=session_root,
    )
    result = {
        "slug": pane_obj.slug,
        "pane_id": pane_obj.pane_id,
        "agent": pane_obj.agent,
        "worktree": pane_obj.worktree_path,
        "branch": pane_obj.branch_name,
    }
    click.echo(json.dumps(result, indent=2))


@pane.command("close")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Git repo root",
)
@SESSION_ROOT_OPTION
def pane_close(slug, project_root, session_root):
    """Close a worker pane: kill tmux pane, remove worktree."""
    from dgov.panes import close_worker_pane

    if close_worker_pane(project_root, slug, session_root=session_root):
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
    help="Git repo root",
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
    from dgov.panes import merge_worker_pane, merge_worker_pane_with_close

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
    help="Git repo root",
)
@SESSION_ROOT_OPTION
@click.option("--timeout", "-t", default=600, help="Max seconds to wait (0=forever)")
@click.option("--poll", "-i", default=3, help="Poll interval in seconds")
@click.option("--stable", "-s", default=15, help="Seconds of stable output before declaring done")
def pane_wait(slug, project_root, session_root, timeout, poll, stable):
    """Wait for a worker pane to finish.

    Three detection modes (checked each poll cycle, first wins):
    1. Done-signal file (agents that exit cleanly).
    2. New commits on the worker branch beyond base_sha.
    3. Output stabilization (TUI agents that stay open).
    """
    import time as _time

    from dgov.panes import (
        _STATE_DIR,
        _get_pane,
        _is_done,
        capture_worker_output,
    )

    session_root = os.path.abspath(session_root or project_root)
    pane_record = _get_pane(session_root, slug)
    start = _time.monotonic()
    last_output = None
    stable_since: float | None = None

    while True:
        # Unified check: done-signal, commit-based, or pane-dead
        if _is_done(session_root, slug, pane_record=pane_record):
            click.echo(json.dumps({"done": slug, "method": "signal_or_commit"}))
            return

        # Fallback: output stabilization (TUI agents)
        current_output = capture_worker_output(
            project_root, slug, lines=20, session_root=session_root
        )
        if current_output is not None:
            if current_output == last_output:
                if stable_since is None:
                    stable_since = _time.monotonic()
                elif _time.monotonic() - stable_since >= stable:
                    # Touch done-signal so _is_done / merge-all can see it
                    done_path = Path(session_root) / _STATE_DIR / "done" / slug
                    done_path.parent.mkdir(parents=True, exist_ok=True)
                    done_path.touch()
                    click.echo(json.dumps({"done": slug, "method": "stable"}))
                    return
            else:
                last_output = current_output
                stable_since = None

        elapsed = _time.monotonic() - start
        if timeout > 0 and elapsed >= timeout:
            from dgov.panes import _emit_event, _update_pane_state

            _update_pane_state(session_root, slug, "timed_out")
            _emit_event(session_root, "pane_timed_out", slug)
            agent = pane_record.get("agent", "unknown") if pane_record else "unknown"
            timeout_result = {
                "error": f"Timeout after {timeout}s",
                "slug": slug,
                "agent": agent,
            }
            if agent == "pi":
                timeout_result["suggest_escalate"] = True
            click.echo(json.dumps(timeout_result), err=True)
            sys.exit(1)
        _time.sleep(poll)


@pane.command("wait-all")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Git repo root",
)
@SESSION_ROOT_OPTION
@click.option("--timeout", "-t", default=600, help="Max seconds to wait (0=forever)")
@click.option("--poll", "-i", default=3, help="Poll interval in seconds")
@click.option("--stable", "-s", default=15, help="Seconds of stable output before declaring done")
def pane_wait_all(project_root, session_root, timeout, poll, stable):
    """Wait for ALL worker panes to finish. Prints each as it completes."""
    import time as _time

    from dgov.panes import (
        _STATE_DIR,
        _get_pane,
        _is_done,
        capture_worker_output,
        list_worker_panes,
    )

    session_root = os.path.abspath(session_root or project_root)
    panes = list_worker_panes(project_root, session_root=session_root)
    pending = {p["slug"] for p in panes if not p["done"]}
    if not pending:
        click.echo(json.dumps({"done": "all", "count": 0}))
        return

    start = _time.monotonic()
    stable_trackers: dict[str, tuple[str | None, float | None]] = {
        s: (None, None) for s in pending
    }

    while pending:
        for slug in list(pending):
            rec = _get_pane(session_root, slug)
            if _is_done(session_root, slug, pane_record=rec):
                click.echo(json.dumps({"done": slug, "method": "signal_or_commit"}))
                pending.discard(slug)
                continue

            output = capture_worker_output(project_root, slug, lines=20, session_root=session_root)
            last, since = stable_trackers.get(slug, (None, None))
            if output is not None:
                if output == last:
                    if since is None:
                        since = _time.monotonic()
                    elif _time.monotonic() - since >= stable:
                        # Touch done-signal so _is_done / merge-all can see it
                        done_path = Path(session_root) / _STATE_DIR / "done" / slug
                        done_path.parent.mkdir(parents=True, exist_ok=True)
                        done_path.touch()
                        click.echo(json.dumps({"done": slug, "method": "stable"}))
                        pending.discard(slug)
                        continue
                else:
                    since = None
                stable_trackers[slug] = (output, since)

        elapsed = _time.monotonic() - start
        if timeout > 0 and elapsed >= timeout:
            for slug in pending:
                rec = _get_pane(session_root, slug)
                agent = rec.get("agent", "unknown") if rec else "unknown"
                timeout_result = {
                    "error": f"Timeout after {timeout}s",
                    "slug": slug,
                    "agent": agent,
                }
                if agent == "pi":
                    timeout_result["suggest_escalate"] = True
                click.echo(json.dumps(timeout_result), err=True)
            sys.exit(1)
        if pending:
            _time.sleep(poll)

    click.echo(json.dumps({"done": "all", "count": len(panes)}))


@pane.command("merge-all")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Git repo root",
)
@SESSION_ROOT_OPTION
@click.option(
    "--resolve",
    type=click.Choice(["agent", "manual"]),
    default="agent",
    help="Conflict resolution strategy",
)
def pane_merge_all(project_root, session_root, resolve):
    """Merge ALL done worker panes sequentially. Prints combined summary."""
    from dgov.panes import list_worker_panes, merge_worker_pane

    panes = list_worker_panes(project_root, session_root=session_root)
    done_panes = [p for p in panes if p["done"]]
    if not done_panes:
        click.echo(json.dumps({"merged": [], "skipped": "no done panes"}))
        return

    merged_slugs = []
    failed_slugs = []
    total_files = 0
    warnings = []

    for p in done_panes:
        slug = p["slug"]
        result = merge_worker_pane(project_root, slug, session_root=session_root, resolve=resolve)
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
    help="Git repo root",
)
@SESSION_ROOT_OPTION
def pane_list(project_root, session_root):
    """List all worker panes with live status."""
    from dgov.panes import list_worker_panes

    panes = list_worker_panes(project_root, session_root=session_root)
    click.echo(json.dumps(panes, indent=2))


@pane.command("prune")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Git repo root",
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
    """Classify a task prompt and recommend pi or claude."""
    from dgov.panes import classify_task

    agent = classify_task(prompt)
    click.echo(json.dumps({"recommended_agent": agent, "prompt_preview": prompt[:80]}))


@pane.command("capture")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Git repo root",
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
    help="Git repo root",
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
    help="Git repo root",
)
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default="claude", help="Agent to escalate to")
@click.option(
    "--permission-mode",
    "-m",
    default="acceptEdits",
    help="Permission mode for the new agent",
)
def pane_escalate(slug, project_root, session_root, agent, permission_mode):
    """Escalate a worker pane to a different agent (e.g. pi -> claude)."""
    from dgov.panes import escalate_worker_pane

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


@cli.command("preflight")
@click.option(
    "--project-root",
    "-r",
    default=".",
    help="Git repo root",
)
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default="claude", help="Agent to validate for")
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
    help="Git repo root",
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
    help="Git repo root (worktree to rebase)",
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


@cli.command("agents")
def list_agents():
    """List available agents and which are installed."""
    installed = set(detect_installed_agents())
    agents = []
    for agent_id, defn in AGENT_REGISTRY.items():
        agents.append(
            {
                "id": agent_id,
                "name": defn.name,
                "installed": agent_id in installed,
                "transport": defn.prompt_transport,
            }
        )
    click.echo(json.dumps(agents, indent=2))


@cli.command("version")
def version_cmd():
    """Show dgov version."""
    from dgov import __version__

    result = {"dgov": __version__}
    click.echo(json.dumps(result, indent=2))


@cli.group()
def checkpoint():
    """Manage state checkpoints."""


@checkpoint.command("create")
@click.argument("name")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
def checkpoint_create(name, project_root, session_root):
    """Create a named checkpoint of current state."""
    from dgov.panes import create_checkpoint

    result = create_checkpoint(project_root, name, session_root=session_root)
    click.echo(json.dumps(result, indent=2))


@checkpoint.command("list")
@click.option("--project-root", "-r", default=".", help="Git repo root")
@SESSION_ROOT_OPTION
def checkpoint_list(project_root, session_root):
    """List all checkpoints."""
    from dgov.panes import list_checkpoints

    session_root = os.path.abspath(session_root or project_root)
    result = list_checkpoints(session_root)
    click.echo(json.dumps(result, indent=2))


@cli.command("batch")
@click.argument("spec_path", type=click.Path(exists=True))
@SESSION_ROOT_OPTION
@click.option("--dry-run", is_flag=True, help="Show computed tiers without executing")
def batch(spec_path, session_root, dry_run):
    """Execute a batch spec with DAG-ordered parallelism."""
    from dgov.panes import run_batch

    result = run_batch(spec_path, session_root=session_root, dry_run=dry_run)
    click.echo(json.dumps(result, indent=2))
    if result.get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    cli()
