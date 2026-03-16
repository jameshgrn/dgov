"""CLI command: dgov briefing — live status briefing via glow."""

from __future__ import annotations

import os
import time
from pathlib import Path

import click

from dgov.cli import SESSION_ROOT_OPTION


def _generate_briefing(project_root: str, session_root: str) -> str:
    """Generate a markdown briefing from current dgov state."""
    from dgov.persistence import all_panes, read_events

    lines = ["# dgov Briefing", ""]
    lines.append(f"**Project**: `{project_root}`")
    lines.append(f"**Generated**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # Pane summary
    panes = all_panes(session_root)
    active = [p for p in panes if p.get("state") in ("active", "working")]
    done = [p for p in panes if p.get("state") == "done"]
    merged = [p for p in panes if p.get("state") == "merged"]
    failed = [p for p in panes if p.get("state") in ("failed", "timed_out")]

    lines.append("## Workers")
    lines.append("")
    lines.append("| Status | Count |")
    lines.append("|--------|-------|")
    lines.append(f"| Active | {len(active)} |")
    lines.append(f"| Done | {len(done)} |")
    lines.append(f"| Merged | {len(merged)} |")
    lines.append(f"| Failed | {len(failed)} |")
    lines.append("")

    # Active panes detail
    if active:
        lines.append("### Active Workers")
        lines.append("")
        for p in active:
            slug = p.get("slug", "?")
            agent = p.get("agent", "?")
            prompt = (p.get("prompt") or "")[:80]
            lines.append(f"- **{slug}** ({agent}): {prompt}")
        lines.append("")

    # Done panes awaiting review
    if done:
        lines.append("### Awaiting Review")
        lines.append("")
        for p in done:
            slug = p.get("slug", "?")
            agent = p.get("agent", "?")
            lines.append(f"- **{slug}** ({agent})")
        lines.append("")

    # Recent events (last 20)
    events = read_events(session_root)
    if events:
        recent = events[-20:]
        lines.append("## Recent Events")
        lines.append("")
        for ev in reversed(recent):
            ts = ev.get("ts", "")
            if isinstance(ts, (int, float)):
                ts = time.strftime("%H:%M:%S", time.localtime(ts))
            event_type = ev.get("event", "?")
            pane_slug = ev.get("pane", "")
            lines.append(f"- `{ts}` **{event_type}** {pane_slug}")
        lines.append("")

    # Ideas
    ideas_path = Path(session_root) / ".dgov" / "ideas.jsonl"
    if ideas_path.exists():
        import json

        ideas = []
        for line in ideas_path.read_text().strip().splitlines():
            try:
                ideas.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        if ideas:
            lines.append("## Ideas Backlog")
            lines.append("")
            for idea in ideas[-10:]:
                lines.append(f"- {idea.get('summary', idea.get('text', '?'))}")
            lines.append("")

    return "\n".join(lines)


@click.command("briefing")
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@SESSION_ROOT_OPTION
@click.option("--no-pane", is_flag=True, help="Generate briefing.md without opening glow pane")
@click.option("--watch", "-w", is_flag=True, help="Auto-refresh briefing every 5 seconds in glow")
def briefing_cmd(project_root: str, session_root: str | None, no_pane: bool, watch: bool) -> None:
    """Generate a status briefing and display it with glow."""
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)

    briefing_path = Path(session_root) / ".dgov" / "briefing.md"
    briefing_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate briefing
    content = _generate_briefing(project_root, session_root)
    briefing_path.write_text(content)
    click.echo(f"Briefing written to {briefing_path}")

    if no_pane:
        return

    # Open glow in a utility pane
    from dgov.tmux import _run, select_layout, send_command, set_title, split_pane

    existing = _run(["list-panes", "-F", "#{pane_title}"], silent=True).splitlines()
    if "[gov] briefing" in existing:
        # Refresh existing pane — just regenerate the file, glow/watch will pick it up
        click.echo("Briefing pane already open — content refreshed.")
        return

    briefing_str = str(briefing_path)
    if watch:
        cmd = f"watch -t -n 5 -c -- glow {briefing_str}"
    else:
        cmd = f"glow -p {briefing_str}"

    pane_id = split_pane()
    send_command(pane_id, cmd)
    set_title(pane_id, "[gov] briefing")

    # Style the pane border
    _run(
        [
            "set-option",
            "-p",
            "-t",
            pane_id,
            "pane-border-format",
            " #[fg=colour141,bold]#{pane_index} #[fg=colour141]#{pane_title} ",
        ],
        silent=True,
    )

    select_layout("main-vertical")
    click.echo("Briefing pane opened (glow)")
