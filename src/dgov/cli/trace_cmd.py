"""Span and tool-trace CLI commands."""

from __future__ import annotations

import json
import os

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.group("trace")
def trace_cmd():
    """Query spans and tool traces."""


@trace_cmd.command("show")
@click.argument("slug")
@click.option(
    "--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT", help="Git repo root"
)
@SESSION_ROOT_OPTION
def trace_show(slug, project_root, session_root):
    """Show spans and tool trace summary for a pane."""
    from dgov.spans import get_spans, get_tool_trace

    session_root = os.path.abspath(session_root or project_root)
    spans = get_spans(session_root, slug)
    tool_trace = get_tool_trace(session_root, slug)

    if not spans and not tool_trace:
        click.echo(f"No trace data for {slug}")
        return

    if spans:
        click.echo(f"=== Spans ({len(spans)}) ===")
        for s in spans:
            dur = f"{s['duration_ms']:.0f}ms" if s["duration_ms"] >= 0 else "pending"
            outcome = s["outcome"]
            kind = s["span_kind"]
            detail_parts = []
            if s.get("agent"):
                detail_parts.append(f"agent={s['agent']}")
            if s.get("verdict"):
                detail_parts.append(f"verdict={s['verdict']}")
            if s.get("commit_count"):
                detail_parts.append(f"commits={s['commit_count']}")
            if s.get("files_changed"):
                detail_parts.append(f"files={s['files_changed']}")
            if s.get("wait_method"):
                detail_parts.append(f"method={s['wait_method']}")
            if s.get("error"):
                detail_parts.append(f"error={s['error'][:60]}")
            detail = " ".join(detail_parts)
            click.echo(f"  {kind:<10s} {outcome:<8s} {dur:>8s}  {detail}")

    if tool_trace:
        click.echo(f"\n=== Tool Trace ({len(tool_trace)} items) ===")
        tool_calls = [t for t in tool_trace if t["action_type"] == "tool_call"]
        thinking = [t for t in tool_trace if t["action_type"] == "thinking"]
        results = [t for t in tool_trace if t["action_type"] == "tool_result"]
        click.echo(f"  tool_calls: {len(tool_calls)}")
        click.echo(f"  thinking:   {len(thinking)}")
        click.echo(f"  results:    {len(results)}")
        total_in = sum(t["tokens_in"] for t in tool_trace)
        total_out = sum(t["tokens_out"] for t in tool_trace)
        if total_in or total_out:
            click.echo(f"  tokens:     {total_in} in / {total_out} out")
        # Show tool call breakdown
        if tool_calls:
            from collections import Counter

            tools = Counter(t["tool_name"] for t in tool_calls)
            click.echo("  tools used: " + ", ".join(f"{n}({c})" for n, c in tools.most_common()))


@trace_cmd.command("export")
@click.argument("slug", required=False)
@click.option(
    "--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT", help="Git repo root"
)
@SESSION_ROOT_OPTION
@click.option("--all", "export_all", is_flag=True, help="Export all trajectories")
@click.option("--outcome", default=None, help="Filter by outcome (success/failure)")
def trace_export(slug, project_root, session_root, export_all, outcome):
    """Export trajectory JSON for a pane (or all panes with --all)."""
    from dgov.spans import export_all_trajectories, export_trajectory

    session_root = os.path.abspath(session_root or project_root)

    if export_all:
        trajectories = export_all_trajectories(session_root, outcome=outcome)
        click.echo(json.dumps(trajectories, indent=2, default=str))
    elif slug:
        traj = export_trajectory(session_root, slug)
        click.echo(json.dumps(traj, indent=2, default=str))
    else:
        click.echo("Provide a slug or --all", err=True)
        raise SystemExit(1)


@trace_cmd.command("stats")
@click.option(
    "--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT", help="Git repo root"
)
@SESSION_ROOT_OPTION
def trace_stats(project_root, session_root):
    """Aggregate span metrics."""
    from dgov.spans import _get_db

    session_root = os.path.abspath(session_root or project_root)
    conn = _get_db(session_root)

    # Overall counts
    total = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
    if total == 0:
        click.echo("No spans recorded yet.")
        return

    click.echo(f"Total spans: {total}\n")

    # By kind + outcome
    click.echo("=== By Kind ===")
    rows = conn.execute(
        "SELECT span_kind, outcome, COUNT(*), AVG(duration_ms) "
        "FROM spans WHERE outcome != 'pending' "
        "GROUP BY span_kind, outcome ORDER BY span_kind, outcome"
    ).fetchall()
    for kind, outcome, count, avg_ms in rows:
        avg = f"{avg_ms:.0f}ms" if avg_ms and avg_ms >= 0 else "-"
        click.echo(f"  {kind:<10s} {outcome:<8s} {count:>5d}  avg {avg:>8s}")

    # Review verdicts
    click.echo("\n=== Review Verdicts ===")
    verdicts = conn.execute(
        "SELECT verdict, COUNT(*) FROM spans "
        "WHERE span_kind = 'review' AND verdict != '' "
        "GROUP BY verdict"
    ).fetchall()
    for verdict, count in verdicts:
        click.echo(f"  {verdict}: {count}")

    # Tool usage from traces
    tool_count = conn.execute("SELECT COUNT(*) FROM tool_traces").fetchone()[0]
    if tool_count:
        click.echo(f"\n=== Tool Traces ({tool_count} total) ===")
        tools = conn.execute(
            "SELECT tool_name, COUNT(*) FROM tool_traces "
            "WHERE action_type = 'tool_call' AND tool_name != '' "
            "GROUP BY tool_name ORDER BY COUNT(*) DESC LIMIT 10"
        ).fetchall()
        for name, count in tools:
            click.echo(f"  {name}: {count}")

    # Per-agent breakdown
    agent_rows = conn.execute(
        "SELECT agent, outcome, COUNT(*), AVG(duration_ms) "
        "FROM spans WHERE agent != '' "
        "GROUP BY agent, outcome ORDER BY agent, outcome"
    ).fetchall()
    if agent_rows:
        click.echo("\n=== By Agent ===")
        for agent, outcome, count, avg_ms in agent_rows:
            avg = f"{avg_ms:.0f}ms" if avg_ms and avg_ms >= 0 else "-"
            click.echo(f"  {agent:<16s} {outcome:<8s} {count:>5d}  avg {avg:>8s}")


@trace_cmd.command("training")
@click.option(
    "--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT", help="Git repo root"
)
@SESSION_ROOT_OPTION
@click.option("--outcome", default=None, help="Filter by outcome (success/failure)")
@click.option("--min-tools", default=1, type=int, help="Min tool calls per example")
@click.option("--output", "-o", "output_file", default=None, help="Output file (default: stdout)")
def trace_training(project_root, session_root, outcome, min_tools, output_file):
    """Export training JSONL for fine-tuning."""
    from dgov.spans import export_training_jsonl

    session_root = os.path.abspath(session_root or project_root)
    examples = export_training_jsonl(session_root, outcome=outcome, min_tool_calls=min_tools)

    if not examples:
        click.echo("No training examples found.", err=True)
        return

    if output_file:
        with open(output_file, "w") as f:
            for ex in examples:
                f.write(json.dumps(ex, default=str) + "\n")
        click.echo(f"Exported {len(examples)} training examples to {output_file}", err=True)
    else:
        for ex in examples:
            click.echo(json.dumps(ex, default=str))
        click.echo(f"Exported {len(examples)} training examples", err=True)


@trace_cmd.command("ingest")
@click.argument("slug")
@click.option(
    "--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT", help="Git repo root"
)
@SESSION_ROOT_OPTION
def trace_ingest(slug, project_root, session_root):
    """Manually ingest a transcript for a pane."""
    from pathlib import Path

    from dgov.spans import ingest_transcript

    session_root = os.path.abspath(session_root or project_root)
    transcript_path = Path(project_root) / ".dgov" / "logs" / f"{slug}.transcript.jsonl"

    if not transcript_path.exists():
        click.echo(f"No transcript found at {transcript_path}", err=True)
        raise SystemExit(1)

    count = ingest_transcript(session_root, slug, str(transcript_path))
    click.echo(f"Ingested {count} tool trace rows for {slug}")
