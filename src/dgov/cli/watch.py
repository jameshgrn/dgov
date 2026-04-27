"""Watch subcommand — live event stream for second terminal tab."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from dgov.cli import cli
from dgov.event_types import (
    DgovEvent,
    EvtTaskDispatched,
    IterationFork,
    MergeCompleted,
    ReviewFail,
    ReviewPass,
    SelfReviewAutoPassed,
    SelfReviewError,
    SelfReviewFixStarted,
    SelfReviewPassed,
    SelfReviewRejected,
    SettlementRetry,
    TaskAbandoned,
    TaskDone,
    TaskFailed,
    TaskMergeFailed,
    UnknownEvent,
    WorkerLog,
    deserialize_event,
)
from dgov.live_state import live_plan_names
from dgov.persistence import latest_event_id, read_events
from dgov.project_root import resolve_project_root

if TYPE_CHECKING:
    from rich.console import RenderableType

console = Console()

_TASK_COLORS: dict[str, str] = {}
_PALETTE = [
    "cyan",
    "magenta",
    "blue",
    "yellow",
    "green",
    "bright_cyan",
    "bright_magenta",
    "bright_blue",
]


def _get_task_color(slug: str) -> str:
    """Assign a stable color to a task slug for the duration of the watch."""
    if slug not in _TASK_COLORS:
        _TASK_COLORS[slug] = _PALETTE[len(_TASK_COLORS) % len(_PALETTE)]
    return _TASK_COLORS[slug]


def _infer_plan_name_from_active_tasks(project_root: str) -> str | None:
    """Return the shared live plan name from the latest run-scoped event view."""
    plan_names = live_plan_names(project_root)
    if len(plan_names) != 1:
        return None
    return next(iter(plan_names))


def _default_watch_state(
    project_root: str,
    watch_all: bool,
    plan_name: str | None,
) -> tuple[str | None, int]:
    """Return the initial plan filter and event cursor for watch mode."""
    if plan_name:
        return plan_name, 0
    if watch_all:
        return None, 0
    inferred_plan_name = _infer_plan_name_from_active_tasks(project_root)
    if inferred_plan_name:
        return inferred_plan_name, 0
    return None, latest_event_id(project_root)


@cli.command(name="watch")
@click.option("--all", "watch_all", is_flag=True, help="Stream all plans and history")
@click.option("--plan", "plan_name", help="Stream only events for this plan name")
@click.option(
    "--root",
    "root_path",
    type=click.Path(path_type=Path, exists=True),
    help="Project root or path inside the repo whose state DB you want to watch",
)
def watch_cmd(watch_all: bool, plan_name: str | None, root_path: Path | None) -> None:
    """Stream governor events in real time."""
    project_root = (
        resolve_project_root(root_path) if root_path is not None else resolve_project_root()
    )
    _cmd_watch(str(project_root), watch_all=watch_all, plan_name=plan_name)


def _clean_slug(slug: str) -> str:
    """Strip 'tasks/' prefix and '.toml' suffix for cleaner display."""
    if not slug:
        return ""
    if slug.startswith("tasks/"):
        slug = slug[6:]
    if slug.endswith(".toml"):
        slug = slug[:-5]
    return slug


def _format_token_summary(event: TaskDone | TaskFailed) -> Text | None:
    prompt_tokens = event.prompt_tokens or 0
    completion_tokens = event.completion_tokens or 0
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return None
    return Text(
        f"({prompt_tokens:,} prompt + {completion_tokens:,} completion tokens)",
        style="dim",
    )


def _format_event(
    event: DgovEvent, ts: str, agents: dict[str, str] | None = None
) -> RenderableType | None:
    """Format a single event. Returns a Renderable or None to suppress."""
    task_slug = getattr(event, "task_slug", "")

    # Handle WorkerLog separately
    if isinstance(event, WorkerLog):
        return _format_worker_log(event, ts, task_slug)

    # Suppress review_pass — merged line is enough for happy path
    if isinstance(event, ReviewPass):
        return None

    # Dispatch header
    if isinstance(event, EvtTaskDispatched):
        agent = event.agent
        # Resolve from project config mapping if available
        if agents and agent in agents:
            agent = agents[agent]
        agent_short = agent.rsplit("/", 1)[-1] if agent else ""
        return _make_row(ts, "⚙", "start", "bold blue", task_slug, f"agent: {agent_short}")

    # task_done - suppress lifecycle done without token metadata
    if isinstance(event, TaskDone):
        token_summary = _format_token_summary(event)
        if token_summary is None:
            return None
        return _make_row(ts, "✓", "done", "green", task_slug, token_summary)

    # task_failed
    if isinstance(event, TaskFailed):
        token_summary = _format_token_summary(event)
        label = "FAILED"
        error = event.error or ""
        content: str | Text = error
        full_width = bool(error)
        if token_summary is not None:
            if error:
                content_text = Text(error)
                content_text.append(f"  {token_summary.plain}", style="dim")
                content = content_text
            else:
                content = token_summary
                full_width = False
        return _make_row(ts, "✖", label, "bold red", task_slug, content, full_width=full_width)

    # review_fail
    if isinstance(event, ReviewFail):
        error = event.verdict or ""
        return _make_row(ts, "✖", "rev FAIL", "bold red", task_slug, error, full_width=True)

    # task_merge_failed
    if isinstance(event, TaskMergeFailed):
        error = event.error or ""
        return _make_row(ts, "✖", "merge FAIL", "bold red", task_slug, error, full_width=True)

    # Merged
    if isinstance(event, MergeCompleted):
        return _make_row(ts, "●", "merged", "bold green", task_slug, "")

    # Settlement retry
    if isinstance(event, SettlementRetry):
        return _make_row(ts, "⟳", "retry", "bold yellow", task_slug, event.error, full_width=True)

    # Iteration fork
    if isinstance(event, IterationFork):
        depth = event.fork_depth
        return _make_row(ts, "⑂", "fork", "bold yellow", task_slug, f"depth {depth}")

    # Self-review events
    if isinstance(event, SelfReviewPassed):
        return _make_row(ts, "✔", "self-rev ok", "green", task_slug, "")

    if isinstance(event, SelfReviewRejected):
        findings = event.findings or ""
        preview = findings[:120] + "…" if len(findings) > 120 else findings
        return _make_row(ts, "✖", "self-rev ✗", "bold yellow", task_slug, preview, full_width=True)

    if isinstance(event, SelfReviewAutoPassed):
        return _make_row(ts, "⟳", "self-rev auto", "yellow", task_slug, "auto-passed after fix")

    if isinstance(event, SelfReviewFixStarted):
        return _make_row(ts, "⟳", "self-rev fix", "yellow", task_slug, "relaunching worker")

    if isinstance(event, SelfReviewError):
        error = event.error or ""
        return _make_row(
            ts,
            "✖",
            "self-rev err",
            "bold red",
            task_slug,
            f"auto-passed: {error}",
            full_width=True,
        )

    # TaskAbandoned - no special formatting, uses default label
    if isinstance(event, TaskAbandoned):
        return _make_row(ts, " ", "task_abandoned", "dim", task_slug, "")

    # UnknownEvent - fall through to default label rendering
    if isinstance(event, UnknownEvent):
        label = event.event_name or "unknown_event"
        return _make_row(ts, " ", label, "dim", task_slug, "")

    # Everything else - use event_type as label
    label = getattr(event, "event_type", "unknown")
    return _make_row(ts, " ", label, "dim", task_slug, "")


def _format_worker_log(event: WorkerLog, ts: str, task_slug: str) -> RenderableType | None:
    """Format worker_log events. Returns Renderable or None to suppress."""
    log_type = event.log_type
    content = event.content
    verify_tools = frozenset({
        "run_tests",
        "lint_check",
        "lint_fix",
        "format_file",
        "type_check",
        "check_syntax",
    })

    if log_type == "error":
        return _make_row(ts, "✖", "error", "bold red", task_slug, str(content), full_width=True)
    if log_type == "done":
        text = str(content) if content else ""
        # Render summaries as Markdown for beautiful lists and bolding
        return _make_row(ts, "✔", "ok", "green", task_slug, Markdown(text), full_width=True)
    if log_type == "thought":
        text = str(content) if content else ""
        return _make_row(ts, "…", "thought", "dim", task_slug, text, content_dim=True)
    if log_type == "call":
        if isinstance(content, dict):
            tool = content.get("tool", "?")
            args = content.get("args", {})
            summary = ", ".join(f"{k}={repr(v)[:80]}" for k, v in args.items())

            content_text = Text()
            content_text.append(tool, style="bold yellow")
            content_text.append("(", style="dim")
            content_text.append(summary, style="dim")
            content_text.append(")", style="dim")
            return _make_row(ts, "○", "call", "blue", task_slug, content_text)

        content_text = Text(str(content))
        content_text.stylize("dim")
        return _make_row(ts, "○", "call", "blue", task_slug, content_text)

    if log_type == "result":
        if isinstance(content, dict) and content.get("status") == "success":
            tool = content.get("tool", "?")
            if tool in verify_tools:
                content_text = Text()
                content_text.append("tool: ", style="dim")
                content_text.append(str(tool), style="bold green")
                return _make_row(ts, "✔", "ok", "green", task_slug, content_text)
        if isinstance(content, dict) and content.get("status") == "failed":
            tool = content.get("tool", "?")
            content_text = Text()
            content_text.append("tool: ", style="dim")
            content_text.append(tool, style="bold red")
            return _make_row(ts, "✖", "fail", "red", task_slug, content_text)
        return None

    return _make_row(ts, " ", log_type, "dim", task_slug, str(content), content_dim=True)


def _make_row(
    ts: str,
    symbol: str,
    label: str,
    label_style: str,
    slug: str,
    content: str | RenderableType,
    content_dim: bool = False,
    full_width: bool = False,
) -> RenderableType:
    """Assemble a beautiful grid-aligned row. Optionally puts content on new line."""
    table = Table.grid(padding=(0, 1))
    table.add_column(width=9)  # TS
    table.add_column(width=10)  # Symbol + Label
    table.add_column(width=24)  # Slug
    table.add_column(width=1)  # Separator
    table.add_column()  # Content (flexible)

    clean_slug = _clean_slug(slug)
    slug_color = _get_task_color(slug)

    # If full_width, we print the content on a second line with a slight indent
    if full_width and content:
        # Header row with empty content
        table.add_row(
            Text(ts, style="dim"),
            Text(f"{symbol} {label}", style=label_style),
            Text(clean_slug, style=f"bold {slug_color}"),
            Text("│", style="dim"),
            "",
        )

        c_renderable = content if not isinstance(content, str) else Text(content)
        if content_dim and isinstance(c_renderable, Text):
            c_renderable.stylize("dim")

        return Group(table, Padding(c_renderable, (0, 0, 1, 4)))

    if isinstance(content, Text):
        content_renderable = content
        if content_dim:
            content_renderable.stylize("dim")
    elif isinstance(content, str):
        c_style = "dim" if content_dim else ""
        content_renderable = Text(content, style=c_style)
    else:
        content_renderable = content

    table.add_row(
        Text(ts, style="dim"),
        Text(f"{symbol} {label}", style=label_style),
        Text(clean_slug, style=slug_color),
        Text("│", style="dim"),
        content_renderable,
    )
    return table


def _cmd_watch(
    project_root: str,
    watch_all: bool = False,
    plan_name: str | None = None,
) -> None:
    """Stream events from the current run. Open in a second tab."""
    from dgov.config import load_project_config

    console.print("dgov watch", style="bold cyan")
    config = load_project_config(project_root)
    agents = config.agents if config else {}
    active_plan_name, last_id = _default_watch_state(project_root, watch_all, plan_name)

    if plan_name:
        console.print(f"  plan: {plan_name}", style="dim")
    elif watch_all:
        console.print("  scope: all plans", style="dim")
    elif active_plan_name:
        console.print(f"  inferred plan: {active_plan_name}", style="dim")
    else:
        console.print("  scope: live tail (no active plan inferred)", style="dim")
    console.print("  (Ctrl-C to exit)\n", style="dim")

    last_task = ""
    try:
        while True:
            if active_plan_name is None and not watch_all and plan_name is None:
                active_plan_name = _infer_plan_name_from_active_tasks(project_root)

            # Detect DB reset (new run started) — last_id would be ahead of max
            current_max = latest_event_id(project_root)
            if current_max < last_id:
                console.print("\n  --- [bold]new run[/bold] ---\n", style="dim")
                last_id = 0
                last_task = ""
                _TASK_COLORS.clear()
                if not watch_all and plan_name is None:
                    active_plan_name, last_id = _default_watch_state(project_root, False, None)

            events = read_events(project_root, after_id=last_id, plan_name=active_plan_name)
            for ev in events:
                last_id = max(last_id, ev.get("id", 0))
                ts_raw = ev.get("ts", "")
                ts = ts_raw[11:19] if len(ts_raw) >= 19 else ts_raw
                typed_event = deserialize_event(ev)
                line = _format_event(typed_event, ts, agents=agents)
                if line is None:
                    continue

                # Blank line between tasks
                task = getattr(typed_event, "task_slug", "")
                if isinstance(typed_event, EvtTaskDispatched) and last_task:
                    console.print("")
                if task:
                    last_task = task

                console.print(line)

            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped watch[/dim]")
