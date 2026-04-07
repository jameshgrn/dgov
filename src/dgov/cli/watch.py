"""Watch subcommand — live event stream for second terminal tab."""

from __future__ import annotations

import time
from pathlib import Path

import click

from dgov.cli import cli
from dgov.persistence import latest_event_id, read_events


@cli.command(name="watch")
def watch_cmd() -> None:
    """Stream governor events in real time."""
    _cmd_watch(str(Path.cwd()))


def _dim(text: str) -> str:
    return click.style(text, dim=True)


def _format_event(ev: dict) -> str | None:
    """Format a single event. Returns None to suppress."""
    event_type = ev.get("event", "?")
    task_slug = ev.get("task_slug") or ev.get("slug") or ""
    ts_raw = ev.get("ts", "")
    ts = _dim(ts_raw[11:19] if len(ts_raw) >= 19 else ts_raw)

    # Suppress lifecycle done — worker_log done already has the summary
    if event_type == "task_done":
        return None
    # Suppress review_pass — merged line is enough for happy path
    if event_type == "review_pass":
        return None

    if event_type == "worker_log":
        return _format_worker_log(ts, task_slug, ev)

    # Dispatch header
    if event_type == "dag_task_dispatched":
        agent = ev.get("agent", "")
        agent_short = agent.rsplit("/", 1)[-1] if agent else ""
        return (
            f"{ts}  {click.style('>>', bold=True)} "
            f"{click.style(task_slug, bold=True)} "
            f"{_dim(f'({agent_short})')}"
        )

    # Failure events
    if event_type in ("task_failed", "review_fail", "task_merge_failed"):
        label = _EVENT_LABELS.get(event_type, event_type)
        suffix = ""
        error = ev.get("error")
        if error:
            suffix = f" — {error[:100]}"
        verdict = ev.get("verdict")
        if verdict and verdict != "ok":
            suffix = f" ({verdict})"
        return f"{ts} {click.style(f'{label:>12s}', fg='red')}  {task_slug}{suffix}"

    # Merged
    if event_type == "merge_completed":
        return f"{ts} {click.style('      merged', fg='green')}  {task_slug}"

    # Settlement retry
    if event_type == "settlement_retry":
        error = ev.get("error", "")
        short = error[:100] if error else ""
        return f"{ts} {click.style('       RETRY', fg='yellow', bold=True)}  {task_slug}: {short}"

    # Everything else
    label = _EVENT_LABELS.get(event_type, event_type)
    return f"{ts} {label:>12s}  {task_slug}"


def _format_worker_log(ts: str, task_slug: str, ev: dict) -> str | None:
    """Format worker_log events. Returns None to suppress."""
    log_type = ev.get("log_type", "")
    content = ev.get("content")

    if log_type == "error":
        return f"{ts} {click.style('      ERROR', fg='red', bold=True)}  {task_slug}: {content}"
    if log_type == "done":
        text = str(content)[:150] if content else ""
        return f"{ts} {click.style('         ok', fg='green')}  {task_slug}: {text}"
    if log_type == "thought":
        text = str(content)[:120] if content else ""
        return f"{ts}  {_dim(f'           {task_slug}: {text}')}"
    if log_type == "call":
        if isinstance(content, dict):
            tool = content.get("tool", "?")
            args = content.get("args", {})
            summary = ", ".join(f"{k}={repr(v)[:40]}" for k, v in args.items())
            return f"{ts} {_dim('       call')}  {task_slug}: {tool}({_dim(summary)})"
        return f"{ts} {_dim('       call')}  {task_slug}: {content}"
    if log_type == "result":
        if isinstance(content, dict) and content.get("status") == "failed":
            tool = content.get("tool", "?")
            return f"{ts} {click.style('       FAIL', fg='red')}  {task_slug}: {tool}"
        return None

    return f"{ts}  {_dim(f'           {task_slug}: [{log_type}] {content}')}"


_EVENT_LABELS: dict[str, str] = {
    "dag_task_dispatched": ">>",
    "task_done": "done",
    "task_failed": "FAILED",
    "review_pass": "review ok",
    "review_fail": "review FAIL",
    "merge_completed": "merged",
    "task_merge_failed": "merge FAIL",
    "shutdown_requested": "shutdown",
    "dag_completed": "dag done",
    "dag_failed": "dag FAILED",
    "settlement_retry": "RETRY",
}


def _cmd_watch(project_root: str) -> None:
    """Stream events from the current run. Open in a second tab."""
    click.echo("dgov watch (Ctrl-C to exit)")

    last_id = 0
    last_task = ""
    try:
        while True:
            # Detect DB reset (new run started) — last_id would be ahead of max
            current_max = latest_event_id(project_root)
            if current_max < last_id:
                click.echo("\n  --- new run ---\n")
                last_id = 0
                last_task = ""

            events = read_events(project_root, after_id=last_id)
            for ev in events:
                last_id = max(last_id, ev.get("id", 0))
                line = _format_event(ev)
                if line is None:
                    continue

                # Blank line between tasks
                task = ev.get("task_slug") or ev.get("slug") or ""
                event_type = ev.get("event", "")
                if event_type == "dag_task_dispatched" and last_task:
                    click.echo("")
                if task:
                    last_task = task

                click.echo(line)

            time.sleep(0.5)
    except KeyboardInterrupt:
        click.echo("")
