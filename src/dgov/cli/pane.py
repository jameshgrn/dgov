"""Pane management commands."""

from __future__ import annotations

import json
import os
import os as _os
import sys
from dataclasses import asdict

import click

from dgov.cli import SESSION_ROOT_OPTION, want_json
from dgov.context_packet import build_context_packet
from dgov.persistence import PaneState


def _autocorrect_roots(
    project_root: str, session_root: str | None = None
) -> tuple[str, str | None]:
    dgov_pr = _os.environ.get("DGOV_PROJECT_ROOT")
    if dgov_pr and "/.dgov/worktrees/" in _os.path.abspath(project_root):
        project_root = dgov_pr
    if dgov_pr and session_root and "/.dgov/worktrees/" in _os.path.abspath(session_root):
        session_root = dgov_pr
    return project_root, session_root


def _fmt_duration(seconds: int) -> str:
    from dgov.dashboard import fmt_duration

    return fmt_duration(seconds)


def _declared_touches(task: dict) -> list[str]:
    touches = task.get("touches", [])
    return list(touches) if isinstance(touches, list | tuple) else []


def _review_summary(review: object, slug: str) -> dict[str, str | int]:
    return {
        "review": getattr(review, "verdict", "unknown"),
        "commits": getattr(review, "commit_count", 0),
        "slug": slug,
    }


def _finalize_slugs(
    project_root: str,
    session_root: str | None,
    slugs: list[str],
    *,
    resolve: str,
    squash: bool,
    rebase: bool,
    land: bool,
) -> list:
    """Run the canonical post-dispatch pipeline for a set of slugs."""
    from dgov.executor import run_finalize_panes
    from dgov.merger import ConflictResolveStrategy

    return run_finalize_panes(
        project_root,
        slugs,
        session_root=session_root,
        resolve=ConflictResolveStrategy(resolve),
        squash=squash,
        rebase=rebase,
        close=land,
    )


@click.group()
def pane():
    """Manage worker panes."""


@pane.command("util")
@click.argument("command")
@click.option("--title", "-t", default=None, help="Pane title (defaults to command name)")
@click.option("--cwd", "-c", default=".", help="Working directory")
def pane_util(command, title, cwd):
    """Run a command in a utility pane (no worktree).

    \b
    Examples:
      dgov pane util "ls -la" --title "file-list"
      dgov pane util "top" --cwd /tmp
      cat script.sh | dgov pane util "bash -s" --title "run-script"
    """
    from dgov.tmux import create_utility_pane

    title = title or command.split()[0]
    pane_id = create_utility_pane(command, f"[util] {title}", cwd=cwd)
    click.echo(json.dumps({"pane_id": pane_id, "command": command, "title": title}))


@pane.command("create")
@click.option("--agent", "-a", default=None, help="Agent CLI to launch (use 'auto' to classify)")
@click.option("--prompt", "-p", default=None, help="Task prompt for the agent")
@click.option(
    "--prompt-file",
    "-F",
    type=click.Path(exists=True),
    default=None,
    help="Read prompt from file instead of -p",
)
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option(
    "--permission-mode",
    "-m",
    default="bypassPermissions",
    help="Permission mode: plan, acceptEdits, bypassPermissions",
)
@click.option("--slug", "-s", default=None, help="Override auto-generated slug")
@click.option("--extra-flags", "-x", default="", help="Extra flags for the agent CLI")
@click.option(
    "--env",
    "-e",
    multiple=True,
    help="Environment variable as KEY=VALUE (repeatable)",
)
@click.option(
    "--touches",
    "-t",
    multiple=True,
    help="Files the task will touch (repeatable)",
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
@click.option("--role", default="worker", help="Pane role: worker or lt-gov")
@click.option("--parent", default=None, help="Parent pane slug (for LT-GOV-created workers)")
@click.option("--stdin", "use_stdin", is_flag=True, default=False, help="Read prompt from stdin")
def pane_create(
    agent,
    prompt,
    prompt_file,
    project_root,
    session_root,
    permission_mode,
    slug,
    extra_flags,
    env,
    touches,
    preflight,
    fix,
    max_retries,
    template,
    var,
    role,
    parent,
    use_stdin=False,
):
    """Create a worker pane: worktree + tmux + agent.

    \b
    Examples:
      dgov pane create -a qwen-35b -s fix-parser -r . -p "Fix the parser bug"
      dgov pane create -a qwen-9b -s lint-fix -r . -p "Run ruff fix"
      cat prompt.txt | dgov pane create -a qwen-35b -s my-task -r . --stdin
      dgov pane create -a qwen-35b -T default -r . --var key=value
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.agents import get_default_agent, load_registry
    from dgov.executor import run_dispatch_only
    from dgov.strategy import classify_task

    registry = load_registry(project_root)
    skip_auto_structure = False

    if prompt_file:
        from pathlib import Path

        prompt = Path(prompt_file).read_text().strip()

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

    if use_stdin:
        prompt = sys.stdin.read().strip()
        if not prompt:
            raise click.ClickException("No prompt received on stdin")
    elif not prompt:
        raise click.ClickException("Prompt required: use -p, --prompt-file, or -T/--template")

    if agent is None:
        agent = get_default_agent(registry)

    if agent == "auto":
        agent = classify_task(prompt, session_root=session_root)
        click.echo(json.dumps({"auto_classified": agent}), err=True)

    if agent not in registry:
        from dgov.router import is_routable

        if not is_routable(agent, project_root):
            from dgov.router import _load_routing_tables, available_names

            # Show logical routing names + non-routable registry agents (not physical backends)
            routable = set(available_names())
            routing_tables = _load_routing_tables(project_root)
            non_routable: set[str] = set()
            for k in registry:
                if k not in routable and not any(k in b for b in routing_tables.values()):
                    non_routable.add(k)
            all_names = sorted(routable | non_routable)
            click.echo(f"Unknown agent: {agent}. Available: {', '.join(all_names)}", err=True)
            sys.exit(1)

    if preflight:
        from dgov.executor import run_dispatch_preflight
        from dgov.preflight import fix_preflight
        from dgov.router import is_routable
        from dgov.router import resolve_agent as _resolve

        if is_routable(agent, project_root):
            try:
                preflight_agent, _ = _resolve(agent, session_root or project_root, project_root)
            except RuntimeError:
                preflight_agent = "pi"
        else:
            preflight_agent = agent if agent in registry else "pi"
        packet = build_context_packet(prompt, file_claims=list(touches) if touches else None)
        report = run_dispatch_preflight(
            project_root,
            preflight_agent,
            session_root=session_root,
            packet=packet,
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

    # LT-GOV: direct dispatch (no plan pipeline — they orchestrate, not execute)
    if role == "lt-gov":
        try:
            packet = build_context_packet(prompt, file_claims=list(touches) if touches else None)
            pane_obj = run_dispatch_only(
                project_root=project_root,
                prompt=prompt,
                agent=agent,
                session_root=session_root,
                permission_mode=permission_mode,
                slug=slug,
                env_vars=env_vars if env_vars else None,
                extra_flags=extra_flags,
                skip_auto_structure=skip_auto_structure,
                role=role,
                parent_slug=parent,
                context_packet=packet,
            )
        except (ValueError, RuntimeError) as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)

        result = {
            "slug": pane_obj.slug,
            "pane_id": pane_obj.pane_id,
            "agent": pane_obj.agent,
            "worktree": pane_obj.worktree_path,
            "branch": pane_obj.branch_name,
        }
        click.echo(json.dumps(result, indent=2))
        return

    # Worker: dispatch via plan pipeline (canonical path)
    from dgov.plan import build_adhoc_plan, run_plan, write_adhoc_plan
    from dgov.strategy import _generate_slug

    effective_slug = slug or _generate_slug(prompt)
    session_root_abs = os.path.abspath(session_root or project_root)

    try:
        plan = build_adhoc_plan(
            slug=effective_slug,
            prompt=prompt,
            agent=agent,
            project_root=project_root,
            session_root=session_root_abs,
            permission_mode=permission_mode,
            touches=tuple(touches) if touches else (),
            max_retries=max_retries or 1,
            timeout_s=600,
            role=role,
            template=template,
            template_vars=dict(kv.split("=", 1) for kv in var) if var else None,
        )
        plan_path = write_adhoc_plan(plan, session_root_abs)
        dag_result = run_plan(plan_path)
    except (ValueError, RuntimeError) as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    result = {
        "slug": effective_slug,
        "plan": plan_path,
        "dag_run_id": dag_result.run_id,
        "status": dag_result.status,
    }
    click.echo(json.dumps(result, indent=2))


@pane.command("batch")
@click.argument("toml_file", type=click.Path(exists=True))
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
def pane_batch(toml_file, project_root, session_root):
    """Dispatch multiple workers from a TOML file.

    TOML format:

    \b
    [tasks.fix-parser]
    agent = "pi"
    prompt = "Fix the parser bug in..."
    mode = "bypassPermissions"

    \b
    Examples:
      dgov pane batch tasks.toml -r .
      cat batch.toml | xargs -0 dgov pane batch -r .
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)
    from dgov.batch import run_batch

    summary = run_batch(
        toml_file,
        session_root=session_root,
        dry_run=False,
        project_root=project_root,
    )
    click.echo(json.dumps(summary, indent=2))
    if summary.get("failed"):
        sys.exit(1)


@pane.command("close")
@click.argument("slug", nargs=-1, required=True)
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--force", "-f", is_flag=True, help="Remove worktree even if dirty")
@click.option(
    "--dry-run", is_flag=True, default=False, help="Preview what would be closed without executing"
)
def pane_close(slug, project_root, session_root, force, dry_run):
    """Close a worker pane: kill tmux pane, remove worktree.

    \b
    Examples:
      dgov pane close fix-parser -r .
      dgov pane close fix-parser add-tests -r . --force
      dgov pane close stuck-pane -r . --dry-run
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    if dry_run:
        from dgov.persistence import get_pane as _get_pane

        session_root_abs = os.path.abspath(session_root or project_root)
        results = []
        for s in slug:
            rec = _get_pane(session_root_abs, s)
            if rec:
                results.append(
                    {
                        "slug": s,
                        "would_close": True,
                        "state": rec.get("state", "unknown"),
                        "agent": rec.get("agent", "unknown"),
                    }
                )
            else:
                results.append({"slug": s, "would_close": False, "reason": "not found"})
        click.echo(json.dumps({"dry_run": True, "panes": results}, indent=2))
        return

    from dgov.executor import run_close_only

    for s in slug:
        result = run_close_only(project_root, s, session_root=session_root, force=force)
        if result.closed:
            click.echo(json.dumps({"closed": s}))
        else:
            click.echo(
                json.dumps(
                    {
                        "error": f"Pane not found: {s}",
                        "hint": "Run 'dgov pane list -r .' to see active panes",
                    }
                ),
                err=True,
            )
            sys.exit(1)


@pane.command("merge")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
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
@click.option(
    "--rebase",
    is_flag=True,
    default=False,
    help="Rebase merge (linear history, original commits)",
)
@click.option(
    "--strict-claims",
    is_flag=True,
    default=False,
    help="Block merge if worker touched undeclared files",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview merge without executing")
def pane_merge(slug, project_root, session_root, resolve, squash, rebase, strict_claims, dry_run):
    """Merge a branch into main with configurable conflict resolution.

        Merge the worktree branch for the given pane.

    \b
    Examples:
      dgov pane merge fix-parser -r .
      dgov pane merge fix-parser -r . --rebase --strict-claims
      dgov pane merge fix-parser -r . --resolve agent --dry-run
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.executor import run_merge_only

    if rebase and not squash:
        click.echo("Cannot use --rebase with --no-squash", err=True)
        sys.exit(1)

    if dry_run:
        from dgov.executor import run_review_only

        review = run_review_only(
            project_root,
            slug,
            session_root=session_root,
            emit_events=False,
        ).review
        result = review.to_dict()
        result["dry_run"] = True
        click.echo(json.dumps(result, indent=2))
        return

    result = run_merge_only(
        project_root,
        slug,
        session_root=session_root,
        resolve=resolve,
        squash=squash,
        rebase=rebase,
        strict_claims=strict_claims,
    ).merge_result

    merge_dict = asdict(result)
    click.echo(json.dumps(merge_dict, indent=2))

    if merge_dict.get("error"):
        sys.exit(1)


@pane.command("land")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
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
    help="Squash worker commits",
)
@click.option(
    "--rebase",
    is_flag=True,
    default=False,
    help="Rebase merge (linear history, original commits)",
)
@click.option("--dry-run", is_flag=True, default=False, help="Preview land without executing")
def pane_land(slug, project_root, session_root, resolve, squash, rebase, dry_run):
    """Review, merge, and close a worker pane in one step.

    \b
    Examples:
      dgov pane land fix-parser -r .
      dgov pane land fix-parser -r . --rebase --resolve agent
      dgov pane land fix-parser -r . --dry-run
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    if rebase and not squash:
        click.echo("Cannot use --rebase with --no-squash", err=True)
        sys.exit(1)

    if dry_run:
        from dataclasses import asdict

        from dgov.executor import run_review_only

        review = run_review_only(
            project_root,
            slug,
            session_root=session_root,
            emit_events=False,
        ).review
        result = review.to_dict()
        result["dry_run"] = True
        result["would_merge"] = review.verdict == "safe"
        click.echo(json.dumps(result, indent=2))
        return

    results = _finalize_slugs(
        project_root,
        session_root,
        [slug],
        resolve=resolve,
        squash=squash,
        rebase=rebase,
        land=True,
    )
    if not results:
        click.echo(json.dumps({"error": f"Pane not found: {slug}"}), err=True)
        sys.exit(1)

    final = results[0]
    from dataclasses import asdict

    click.echo(json.dumps(_review_summary(final.review, slug)))

    if final.error:
        click.echo(json.dumps({"error": final.error}), err=True)
        sys.exit(1)
    if final.cleanup_error:
        click.echo(json.dumps({"error": final.cleanup_error}), err=True)
        sys.exit(1)

    merge_dict = asdict(final.merge_result) if final.merge_result else {}
    click.echo(json.dumps(merge_dict, indent=2))


@pane.command("wait")
@click.argument("slug", nargs=-1, required=True)
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
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

    \b
    Examples:
      dgov pane wait fix-parser -r .
      dgov pane wait fix-parser add-tests -r . --timeout 300
      dgov pane wait stuck-task -r . -t 60 -s 30
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.executor import run_wait_only
    from dgov.status import list_worker_panes

    panes = list_worker_panes(project_root, session_root=session_root)
    known = {p.get("slug") for p in panes}
    exit_code = 0

    for s in slug:
        if s not in known:
            click.echo(
                json.dumps(
                    {
                        "error": f"Pane not found: {s}",
                        "hint": "Run 'dgov pane list -r .' to see active panes",
                    }
                ),
                err=True,
            )
            exit_code = 1
            continue
        wait_result = run_wait_only(
            project_root,
            s,
            session_root=session_root,
            timeout=timeout,
            poll=poll,
            stable=stable,
            max_retries=0,
            auto_retry=auto_retry,
        )
        if wait_result.state == "completed":
            click.echo(json.dumps(wait_result.wait_result or {"done": True, "slug": s}))
        else:
            timeout_result = {"error": wait_result.error or "Worker failed", "slug": s}
            if wait_result.suggest_escalate:
                timeout_result["suggest_escalate"] = True
            click.echo(json.dumps(timeout_result), err=True)
            exit_code = 1

    if exit_code:
        sys.exit(exit_code)


@pane.command("list")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON")
@click.option("--verbose", "-v", is_flag=True, default=False, help="Show last output line")
def pane_list(project_root, session_root, as_json, verbose):
    """List all worker panes with live status.

    \b
    Examples:
      dgov pane list -r .
      dgov pane list -r . --json
      dgov pane list -r . --verbose
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.status import list_worker_panes

    panes = list_worker_panes(project_root, session_root=session_root)

    if as_json or want_json():
        click.echo(json.dumps(panes, indent=2))
        return

    if not panes:
        click.echo("No panes.")
        return

    # Format as table
    header = f"{'Slug':<20} {'Agent':<8} {'State':<10} {'Phase':<12} {'Duration':<8} {'Summary'}"
    click.echo(header)
    click.echo("-" * len(header))
    for p in panes:
        slug = (p.get("slug", "") or "")[:19]
        agent = (p.get("agent", "unknown") or "unknown")[:7]
        state = p.get("state", "active") or "active"
        phase = p.get("phase", p.get("activity", "?")) or "?"
        duration_s = int(p.get("duration_s", 0))
        duration = _fmt_duration(duration_s)
        # Prefer prompt (task purpose) over log tail (often terminal noise)
        summary = (p.get("prompt", "") or "").strip()[:60]
        if not summary:
            summary = (p.get("summary", "") or "").strip()[:60]
        row = f"{slug:<20} {agent:<8} {state:<10} {phase:<12} {duration:<8} {summary}"
        click.echo(row)
        if verbose and p.get("last_output"):
            click.echo(f"  └ {p['last_output'].strip()}")


@pane.command("gc")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option(
    "--older-than-hours",
    default=24.0,
    type=float,
    show_default=True,
    help="Only collect preserved or terminal panes older than this many hours",
)
@click.option(
    "--state",
    "states",
    multiple=True,
    help="Only collect panes in these states (repeatable)",
)
def pane_gc(project_root, session_root, older_than_hours, states):
    """Garbage-collect: prune stale entries, then clean old preserved/terminal panes.

    \b
    Examples:
      dgov pane gc -r .
      dgov pane gc -r . --older-than-hours 48
      dgov pane gc -r . --state done --state failed --older-than-hours 12
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.status import gc_retained_panes, prune_stale_panes

    pruned = prune_stale_panes(project_root, session_root=session_root)
    result = gc_retained_panes(
        project_root,
        session_root=session_root,
        older_than_s=older_than_hours * 3600.0,
        states=tuple(states),
    )
    result["pruned"] = pruned
    click.echo(json.dumps(result))


@pane.command("review")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--full", is_flag=True, help="Show complete diff (not just stat)")
@click.option("--diff", "show_diff", is_flag=True, help="Include full diff in review output")
def pane_review(slug, project_root, session_root, full, show_diff):
    """Preview a worker pane's changes before merging.

    \b
    Examples:
      dgov pane review fix-parser -r .
      dgov pane review fix-parser -r . --full
      dgov pane review fix-parser -r . --diff
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.executor import run_review_only

    result = run_review_only(
        project_root,
        slug,
        session_root=session_root,
        full=full,
        emit_events=False,
        require_safe=False,
        require_commits=False,
    ).review
    if show_diff and result.error is None:
        from dgov.inspection import diff_worker_pane

        diff_result = diff_worker_pane(project_root, slug, session_root=session_root)
        result.diff = diff_result.get("diff", "")
    click.echo(json.dumps(result.to_dict(), indent=2))
    if result.error is not None:
        sys.exit(1)


@pane.command("diff")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Git repo root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--stat", is_flag=True, help="Show diffstat only")
@click.option("--name-only", is_flag=True, help="Show changed file names only")
def pane_diff(slug, project_root, session_root, stat, name_only):
    """Show diff for a worker pane's branch vs base.

    \b
    Examples:
      dgov pane diff fix-parser -r .
      dgov pane diff fix-parser -r . --stat
      dgov pane diff fix-parser -r . --name-only
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

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
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default=None, help="Agent to escalate to")
@click.option(
    "--permission-mode",
    "-m",
    default="bypassPermissions",
    help="Permission mode for the new agent",
)
def pane_escalate(slug, project_root, session_root, agent, permission_mode):
    """Re-dispatch to a stronger agent.

    \b
    Examples:
      dgov pane escalate stuck-task -r . -a qwen-35b
      dgov pane escalate stuck-task -r . --agent qwen-122b
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.agents import get_default_agent, load_registry
    from dgov.executor import run_escalate_only

    if agent is None:
        agent = get_default_agent(load_registry(project_root))

    result = run_escalate_only(
        project_root,
        slug,
        session_root=session_root,
        target_agent=agent,
        permission_mode=permission_mode,
    )
    output = {}
    if result.error:
        output["error"] = result.error
    else:
        output["new_slug"] = result.new_slug
        output["target_agent"] = result.target_agent
    click.echo(json.dumps(output, indent=2))
    if "error" in output:
        sys.exit(1)


@pane.command("retry")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Git repo root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default=None, help="Override agent for retry")
@click.option("--prompt", "-p", default=None, help="Override prompt for retry")
@click.option("--permission-mode", "-m", default="bypassPermissions", help="Permission mode")
@click.option("--close", is_flag=True, help="Close the original pane before retrying")
def pane_retry(slug, project_root, session_root, agent, prompt, permission_mode, close):
    """Retry a failed pane with a new attempt.

    \b
    Examples:
      dgov pane retry failed-task -r .
      dgov pane retry failed-task -r . --agent qwen-35b
      dgov pane retry failed-task -r . --close
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.executor import run_retry_only

    result = run_retry_only(
        project_root,
        slug,
        session_root=session_root,
        agent=agent,
    )
    output = {}
    if result.error:
        output["error"] = result.error
    else:
        output["new_slug"] = result.new_slug
    click.echo(json.dumps(output, indent=2))
    if "error" in output:
        sys.exit(1)


@pane.command("retry-or-escalate")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Git repo root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--max-retries", "-n", default=2, help="Retries before escalating (default: 2)")
@click.option("--permission-mode", "-m", default="bypassPermissions", help="Permission mode")
def pane_retry_or_escalate(slug, project_root, session_root, max_retries, permission_mode):
    """Retry a failed pane, auto-escalating after N retries at the same tier.

    \b
    Examples:
      dgov pane retry-or-escalate stuck-task -r .
      dgov pane retry-or-escalate stuck-task -r . --max-retries 3
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.executor import run_retry_or_escalate

    result = run_retry_or_escalate(
        project_root,
        slug,
        session_root=session_root,
    )
    # Result is RetryResult | EscalateResult (union type)
    output = {}
    if result.error:
        output["error"] = result.error
    elif hasattr(result, "new_slug") and result.new_slug:
        output["new_slug"] = result.new_slug
        if hasattr(result, "target_agent") and result.target_agent:
            output["target_agent"] = result.target_agent
    click.echo(json.dumps(output, indent=2))
    if "error" in output:
        sys.exit(1)


@pane.command("output")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--tail", "-n", default=50, help="Number of lines from end")
@click.option("--follow", "-f", is_flag=True, help="Stream output continuously (like tail -f)")
def pane_output(slug, project_root, session_root, tail, follow):
    """Show worker output (auto-routes: log for headless workers, capture for TUI).

    \b
    Examples:
      dgov pane output fix-parser -r .
      dgov pane output fix-parser -r . --tail 100
      dgov pane output fix-parser -r . --follow
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.persistence import get_pane
    from dgov.status import _clean_worker_output_text, capture_worker_output, tail_worker_log

    session_root = os.path.abspath(session_root or project_root)
    pane_record = get_pane(session_root, slug)
    is_headless = not pane_record or pane_record.get("role", "worker") == "worker"

    if is_headless:
        # Headless workers: log file has clean output, capture may show empty shell
        text = tail_worker_log(session_root, slug, lines=tail)
        if text is None:
            text = capture_worker_output(project_root, slug, lines=tail, session_root=session_root)
    else:
        # TUI mode (lt-gov): live screen capture is cleaner than ANSI-laden logs
        text = capture_worker_output(project_root, slug, lines=tail, session_root=session_root)
        if text is None:
            text = tail_worker_log(session_root, slug, lines=tail)

    if text is None:
        click.echo(json.dumps({"error": f"No output for: {slug}"}), err=True)
        sys.exit(1)
    click.echo(text)

    if follow:
        import time
        from pathlib import Path

        from dgov.persistence import STATE_DIR

        log_path = Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"
        if not log_path.exists():
            click.echo("(no log file to follow)")
            return
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)  # seek to end
                while True:
                    line = f.readline()
                    if line:
                        cleaned = _clean_worker_output_text(line.decode("utf-8", errors="replace"))
                        if cleaned:
                            click.echo(cleaned)
                    else:
                        time.sleep(0.5)
        except KeyboardInterrupt:
            pass


_TAIL_TERMINAL_STATES = frozenset(
    {
        s.value
        for s in (
            PaneState.DONE,
            PaneState.FAILED,
            PaneState.MERGED,
            PaneState.CLOSED,
            PaneState.ABANDONED,
            PaneState.TIMED_OUT,
            PaneState.ESCALATED,
            PaneState.SUPERSEDED,
        )
    }
)


@pane.command("tail")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("-n", "--lines", default=50, help="Number of lines to show initially")
def pane_tail(slug, project_root, session_root, lines):
    """Stream worker output in real-time, auto-stop on completion.

    \b
    Examples:
      dgov pane tail fix-parser -r .
      dgov pane tail fix-parser -r . --lines 100
    """
    import time
    from pathlib import Path

    from dgov.persistence import STATE_DIR, get_pane
    from dgov.status import _clean_worker_output_text, capture_worker_output, tail_worker_log

    project_root, session_root = _autocorrect_roots(project_root, session_root)
    session_root = os.path.abspath(session_root or project_root)

    pane_record = get_pane(session_root, slug)
    if not pane_record:
        click.secho(f"Pane not found: {slug}", fg="red", err=True)
        sys.exit(1)

    # Check if already terminal before streaming
    state = pane_record.get("state", "")
    if state in _TAIL_TERMINAL_STATES:
        # Show final output snapshot
        text = tail_worker_log(session_root, slug, lines=lines)
        if text is None:
            text = capture_worker_output(
                project_root, slug, lines=lines, session_root=session_root
            )
        if text:
            click.echo(text)
        color = "green" if state == PaneState.MERGED else "yellow"
        click.secho(f"--- {slug} {state} ---", fg=color)
        return

    # Show initial output
    is_headless = pane_record.get("role", "worker") == "worker"
    if is_headless:
        text = tail_worker_log(session_root, slug, lines=lines)
        if text is None:
            text = capture_worker_output(
                project_root, slug, lines=lines, session_root=session_root
            )
    else:
        text = capture_worker_output(project_root, slug, lines=lines, session_root=session_root)
        if text is None:
            text = tail_worker_log(session_root, slug, lines=lines)
    if text:
        click.echo(text)

    # Follow loop
    log_path = Path(session_root) / STATE_DIR / "logs" / f"{slug}.log"
    try:
        if log_path.exists():
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                while True:
                    line = f.readline()
                    if line:
                        cleaned = _clean_worker_output_text(line.decode("utf-8", errors="replace"))
                        if cleaned:
                            click.echo(cleaned)
                    else:
                        time.sleep(0.5)
                        rec = get_pane(session_root, slug)
                        st = rec.get("state", "") if rec else "closed"
                        if st in _TAIL_TERMINAL_STATES:
                            # Drain remaining lines before exiting
                            while True:
                                remaining = f.readline()
                                if not remaining:
                                    break
                                cleaned = _clean_worker_output_text(
                                    remaining.decode("utf-8", errors="replace")
                                )
                                if cleaned:
                                    click.echo(cleaned)
                            click.secho(
                                f"--- {slug} {st} ---",
                                fg="green" if st == PaneState.MERGED.value else "yellow",
                            )
                            return
        else:
            # No log file — poll capture output
            last_text = ""
            while True:
                time.sleep(1)
                rec = get_pane(session_root, slug)
                st = rec.get("state", "") if rec else "closed"
                if st in _TAIL_TERMINAL_STATES:
                    click.secho(
                        f"--- {slug} {st} ---",
                        fg="green" if st == PaneState.MERGED.value else "yellow",
                    )
                    return
                new_text = capture_worker_output(
                    project_root, slug, lines=lines, session_root=session_root
                )
                if new_text and new_text != last_text:
                    click.echo(new_text)
                    last_text = new_text
    except KeyboardInterrupt:
        pass


@pane.command("message")
@click.argument("slug")
@click.argument("text")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
def pane_message(slug, text, project_root, session_root):
    """Send a message to a running worker pane.

    \b
    Examples:
      dgov pane message fix-parser "Add tests for edge case" -r .
      dgov pane message stuck-task "Please check the parser module" -r .
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.backend import get_backend
    from dgov.persistence import get_pane
    from dgov.waiter import interact_with_pane

    session_root = os.path.abspath(session_root or project_root)
    if not interact_with_pane(session_root, slug, text):
        target = get_pane(session_root, slug)
        if not target:
            click.echo(json.dumps({"error": f"Pane not found: {slug}"}))
        else:
            pane_id = target.get("pane_id")
            if not pane_id:
                click.echo(json.dumps({"error": f"Pane {slug} has no pane_id"}))
            elif not get_backend().is_alive(pane_id):
                click.echo(json.dumps({"error": f"Pane {slug} is not running"}))
            else:
                click.echo(json.dumps({"error": f"Pane {slug} agent not attached"}))
        sys.exit(1)
    click.echo(json.dumps({"sent": True, "slug": slug, "message": text[:100]}))


@pane.command("merge-request")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
def pane_merge_request(slug, project_root, session_root):
    """Submit a merge request to the queue (used by LT-GOVs).

    \b
    Examples:
      dgov pane merge-request fix-parser -r .
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.executor import run_enqueue_merge
    from dgov.persistence import get_pane

    session_root_abs = os.path.abspath(session_root or project_root)
    target = get_pane(session_root_abs, slug)
    if not target:
        click.echo(json.dumps({"error": f"Pane not found: {slug}"}), err=True)
        sys.exit(1)

    requester = os.environ.get("DGOV_SLUG", "governor")
    result = run_enqueue_merge(session_root_abs, slug, requester)
    click.echo(json.dumps(result))


@pane.command("signal")
@click.argument("slug")
@click.argument("signal_type", type=click.Choice(["done", "failed"]))
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
def pane_signal(slug, signal_type, project_root, session_root):
    """Manually signal a pane as done or failed.

    \b
    Examples:
      dgov pane signal fix-parser done -r .
      dgov pane signal stuck-task failed -r .
    """
    from dgov.persistence import get_pane
    from dgov.waiter import signal_pane

    project_root, session_root = _autocorrect_roots(project_root, session_root)
    session_root = os.path.abspath(session_root or project_root)

    # Idempotent: if already in the target state, no-op
    target = get_pane(session_root, slug)
    if target and target.get("state") == signal_type:
        click.echo(json.dumps({"already": signal_type, "slug": slug}))
        return

    if signal_pane(session_root, slug, signal_type):
        click.echo(json.dumps({"signaled": signal_type, "slug": slug}))
    else:
        if target and signal_type == "done":
            error = f"Pane {slug} has no completion commit; cannot signal done."
        else:
            error = f"Pane not found: {slug}"
        click.echo(
            json.dumps({"error": error, "hint": "Run 'dgov pane list -r .' to see active panes"}),
            err=True,
        )
        sys.exit(1)
