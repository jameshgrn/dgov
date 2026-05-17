"""Watch subcommand — live event stream for second terminal tab."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from dgov.cli import cli, load_project_config_or_exit
from dgov.event_types import (
    DgovEvent,
    EvtTaskDispatched,
    IntegrationCandidateFailed,
    IntegrationCandidatePassed,
    IntegrationOverlapDetected,
    IntegrationRiskScored,
    IterationFork,
    MergeCompleted,
    ReviewFail,
    ReviewPass,
    SelfReviewAutoPassed,
    SelfReviewError,
    SelfReviewFixStarted,
    SelfReviewPassed,
    SelfReviewRejected,
    SemanticGateRejected,
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
from dgov.semantic_settlement import describe_evidence_payload

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
_VERIFY_TOOLS = frozenset({
    "run_tests",
    "lint_check",
    "lint_fix",
    "format_file",
    "type_check",
    "check_syntax",
})


@dataclass
class _WatchState:
    active_plan_name: str | None
    last_id: int
    last_task: str = ""


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
    if isinstance(event, WorkerLog):
        return _format_worker_log(event, ts, task_slug)
    if isinstance(event, ReviewPass):
        return None
    if isinstance(event, EvtTaskDispatched):
        return _format_dispatch_event(event, ts, agents)
    if isinstance(event, (TaskDone, TaskFailed)):
        return _format_task_completion_event(event, ts, task_slug)
    if isinstance(
        event,
        (ReviewFail, TaskMergeFailed, MergeCompleted, SettlementRetry, IterationFork),
    ):
        return _format_settlement_event(event, ts, task_slug)
    if isinstance(
        event,
        (
            SelfReviewPassed,
            SelfReviewRejected,
            SelfReviewAutoPassed,
            SelfReviewFixStarted,
            SelfReviewError,
        ),
    ):
        return _format_self_review_event(event, ts, task_slug)
    if isinstance(
        event,
        (
            IntegrationRiskScored,
            IntegrationOverlapDetected,
            IntegrationCandidatePassed,
            IntegrationCandidateFailed,
            SemanticGateRejected,
        ),
    ):
        return _format_semantic_settlement_event(event, ts, task_slug)
    return _format_default_event(event, ts, task_slug)


def _format_semantic_settlement_event(
    event: IntegrationRiskScored
    | IntegrationOverlapDetected
    | IntegrationCandidatePassed
    | IntegrationCandidateFailed
    | SemanticGateRejected,
    ts: str,
    task_slug: str,
) -> RenderableType:
    if isinstance(event, IntegrationRiskScored):
        return _format_integration_risk_event(event, ts, task_slug)
    if isinstance(event, IntegrationOverlapDetected):
        evidence = _format_evidence_lines((event.evidence,))
        return _make_row(ts, "!", "overlap", "bold yellow", task_slug, evidence, full_width=True)
    if isinstance(event, IntegrationCandidatePassed):
        detail = _short_sha_detail("candidate", event.candidate_sha)
        return _make_row(ts, "✔", "cand ok", "green", task_slug, detail)
    if isinstance(event, IntegrationCandidateFailed):
        detail = _failure_detail(event.failure_class, event.error_message, event.evidence)
        return _make_row(ts, "✖", "cand err", "bold red", task_slug, detail, full_width=True)
    detail = _failure_detail(event.failure_class, event.error_message, event.evidence)
    if event.gate_name:
        detail = f"gate={event.gate_name}\n{detail}" if detail else f"gate={event.gate_name}"
    return _make_row(ts, "✖", "gate err", "bold red", task_slug, detail, full_width=True)


def _format_integration_risk_event(
    event: IntegrationRiskScored,
    ts: str,
    task_slug: str,
) -> RenderableType:
    parts = [f"risk={event.risk_level or 'unknown'}"]
    if event.python_overlap_detected:
        parts.append("overlap detected")
    if event.claimed_files:
        parts.append(f"claimed={len(event.claimed_files)}")
    if event.changed_files:
        parts.append(f"changed={len(event.changed_files)}")
    evidence = _format_evidence_lines(event.overlap_evidence)
    detail = ", ".join(parts)
    if evidence:
        detail = f"{detail}\n{evidence}"
    return _make_row(ts, "!", "risk", "bold yellow", task_slug, detail, full_width=bool(evidence))


def _short_sha_detail(label: str, sha: str) -> str:
    if not sha:
        return ""
    return f"{label}={sha[:8]}"


def _failure_detail(
    failure_class: str,
    error_message: str,
    evidence_payload: tuple[dict, ...],
) -> str:
    lines: list[str] = []
    if failure_class:
        lines.append(f"class={failure_class}")
    if error_message:
        lines.append(error_message)
    evidence = _format_evidence_lines(evidence_payload)
    if evidence:
        lines.append(evidence)
    return "\n".join(lines)


def _format_evidence_lines(evidence_payload: tuple[dict, ...]) -> str:
    descriptions = describe_evidence_payload(evidence_payload)
    return "\n".join(f"evidence: {description}" for description in descriptions)


def _format_dispatch_event(
    event: EvtTaskDispatched, ts: str, agents: dict[str, str] | None
) -> RenderableType:
    task_slug = event.task_slug
    agent = event.agent
    if agents and agent in agents:
        agent = agents[agent]
    agent_short = agent.rsplit("/", 1)[-1] if agent else ""
    return _make_row(ts, "⚙", "start", "bold blue", task_slug, f"agent: {agent_short}")


def _format_task_completion_event(
    event: TaskDone | TaskFailed, ts: str, task_slug: str
) -> RenderableType | None:
    if isinstance(event, TaskDone):
        token_summary = _format_token_summary(event)
        if token_summary is None:
            return None
        return _make_row(ts, "✓", "done", "green", task_slug, token_summary)
    return _format_task_failed_event(event, ts, task_slug)


def _format_task_failed_event(event: TaskFailed, ts: str, task_slug: str) -> RenderableType:
    token_summary = _format_token_summary(event)
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
    return _make_row(ts, "✖", "FAILED", "bold red", task_slug, content, full_width=full_width)


def _format_settlement_event(
    event: ReviewFail | TaskMergeFailed | MergeCompleted | SettlementRetry | IterationFork,
    ts: str,
    task_slug: str,
) -> RenderableType:
    if isinstance(event, ReviewFail):
        error = event.verdict or ""
        return _make_row(ts, "✖", "rev FAIL", "bold red", task_slug, error, full_width=True)
    if isinstance(event, TaskMergeFailed):
        error = event.error or ""
        return _make_row(ts, "✖", "merge FAIL", "bold red", task_slug, error, full_width=True)
    if isinstance(event, MergeCompleted):
        return _make_row(ts, "●", "merged", "bold green", task_slug, "")
    if isinstance(event, SettlementRetry):
        return _make_row(ts, "⟳", "retry", "bold yellow", task_slug, event.error, full_width=True)
    depth = event.fork_depth
    return _make_row(ts, "⑂", "fork", "bold yellow", task_slug, f"depth {depth}")


def _format_self_review_event(
    event: SelfReviewPassed
    | SelfReviewRejected
    | SelfReviewAutoPassed
    | SelfReviewFixStarted
    | SelfReviewError,
    ts: str,
    task_slug: str,
) -> RenderableType:
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


def _format_default_event(event: DgovEvent, ts: str, task_slug: str) -> RenderableType:
    if isinstance(event, TaskAbandoned):
        return _make_row(ts, " ", "task_abandoned", "dim", task_slug, "")
    if isinstance(event, UnknownEvent):
        label = event.event_name or "unknown_event"
        return _make_row(ts, " ", label, "dim", task_slug, "")
    label = getattr(event, "event_type", "unknown")
    return _make_row(ts, " ", label, "dim", task_slug, "")


def _format_worker_log(event: WorkerLog, ts: str, task_slug: str) -> RenderableType | None:
    """Format worker_log events. Returns Renderable or None to suppress."""
    log_type = event.log_type
    content = event.content
    if log_type == "error":
        return _make_row(ts, "✖", "error", "bold red", task_slug, str(content), full_width=True)
    if log_type == "done":
        text = str(content) if content else ""
        return _make_row(ts, "✔", "ok", "green", task_slug, Markdown(text), full_width=True)
    if log_type == "thought":
        text = str(content) if content else ""
        return _make_row(ts, "…", "thought", "dim", task_slug, text, content_dim=True)
    if log_type == "call":
        return _format_worker_call(ts, task_slug, content)
    if log_type == "result":
        return _format_worker_result(ts, task_slug, content)
    return _make_row(ts, " ", log_type, "dim", task_slug, str(content), content_dim=True)


def _format_worker_call(ts: str, task_slug: str, content) -> RenderableType:
    if not isinstance(content, dict):
        content_text = Text(str(content))
        content_text.stylize("dim")
        return _make_row(ts, "○", "call", "blue", task_slug, content_text)

    tool = content.get("tool", "?")
    args = content.get("args", {})
    summary = ", ".join(f"{k}={repr(v)[:80]}" for k, v in args.items())
    content_text = Text()
    content_text.append(tool, style="bold yellow")
    content_text.append("(", style="dim")
    content_text.append(summary, style="dim")
    content_text.append(")", style="dim")
    return _make_row(ts, "○", "call", "blue", task_slug, content_text)


def _format_worker_result(ts: str, task_slug: str, content) -> RenderableType | None:
    if not isinstance(content, dict):
        return None
    status = content.get("status")
    if status == "success":
        return _format_successful_worker_result(ts, task_slug, content.get("tool", "?"))
    if status == "failed":
        return _format_failed_worker_result(ts, task_slug, content.get("tool", "?"))
    return None


def _format_successful_worker_result(ts: str, task_slug: str, tool) -> RenderableType | None:
    if tool not in _VERIFY_TOOLS:
        return None
    content_text = Text()
    content_text.append("tool: ", style="dim")
    content_text.append(str(tool), style="bold green")
    return _make_row(ts, "✔", "ok", "green", task_slug, content_text)


def _format_failed_worker_result(ts: str, task_slug: str, tool) -> RenderableType:
    content_text = Text()
    content_text.append("tool: ", style="dim")
    content_text.append(str(tool), style="bold red")
    return _make_row(ts, "✖", "fail", "red", task_slug, content_text)


def _create_row_table() -> Table:
    """Create a grid table with fixed column widths for watch rows."""
    table = Table.grid(padding=(0, 1))
    table.add_column(width=9)  # TS
    table.add_column(width=10)  # Symbol + Label
    table.add_column(width=24)  # Slug
    table.add_column(width=1)  # Separator
    table.add_column()  # Content (flexible)
    return table


def _prepare_slug_and_color(slug: str) -> tuple[str, str]:
    """Return cleaned slug and assigned color for the task."""
    clean_slug = _clean_slug(slug)
    slug_color = _get_task_color(slug)
    return clean_slug, slug_color


def _add_header_row(
    table: Table,
    ts: str,
    symbol: str,
    label: str,
    label_style: str,
    clean_slug: str,
    slug_color: str,
) -> None:
    """Add header row with empty content column for full-width layout."""
    table.add_row(
        Text(ts, style="dim"),
        Text(f"{symbol} {label}", style=label_style),
        Text(clean_slug, style=f"bold {slug_color}"),
        Text("│", style="dim"),
        "",
    )


def _render_content(content: str | RenderableType, content_dim: bool) -> RenderableType:
    """Convert content to renderable, applying dim style if requested."""
    if isinstance(content, Text):
        if content_dim:
            content.stylize("dim")
        return content
    if isinstance(content, str):
        c_style = "dim" if content_dim else ""
        return Text(content, style=c_style)
    return content


def _make_full_width_row(
    ts: str,
    symbol: str,
    label: str,
    label_style: str,
    clean_slug: str,
    slug_color: str,
    content: str | RenderableType,
    content_dim: bool,
) -> RenderableType:
    """Build a full-width row with content on a second line."""
    table = _create_row_table()
    _add_header_row(table, ts, symbol, label, label_style, clean_slug, slug_color)

    c_renderable = content if not isinstance(content, str) else Text(content)
    if content_dim and isinstance(c_renderable, Text):
        c_renderable.stylize("dim")

    return Group(table, Padding(c_renderable, (0, 0, 1, 4)))


def _make_normal_row(
    ts: str,
    symbol: str,
    label: str,
    label_style: str,
    clean_slug: str,
    slug_color: str,
    content: RenderableType,
) -> RenderableType:
    """Build a normal row with all content in a single table row."""
    table = _create_row_table()
    table.add_row(
        Text(ts, style="dim"),
        Text(f"{symbol} {label}", style=label_style),
        Text(clean_slug, style=slug_color),
        Text("│", style="dim"),
        content,
    )
    return table


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
    clean_slug, slug_color = _prepare_slug_and_color(slug)

    if full_width and content:
        return _make_full_width_row(
            ts, symbol, label, label_style, clean_slug, slug_color, content, content_dim
        )

    content_renderable = _render_content(content, content_dim)
    return _make_normal_row(
        ts, symbol, label, label_style, clean_slug, slug_color, content_renderable
    )


def _cmd_watch(
    project_root: str,
    watch_all: bool = False,
    plan_name: str | None = None,
) -> None:
    """Stream events from the current run. Open in a second tab."""
    console.print("dgov watch", style="bold cyan")
    config = load_project_config_or_exit(project_root)
    agents = config.agents if config else {}
    state = _initial_watch_state(project_root, watch_all, plan_name)
    _print_watch_scope(watch_all, plan_name, state.active_plan_name)

    try:
        while True:
            _refresh_watch_plan_filter(project_root, state, watch_all, plan_name)
            _reset_watch_state_if_needed(project_root, state, watch_all, plan_name)
            _print_new_watch_events(project_root, state, agents)
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped watch[/dim]")


def _initial_watch_state(
    project_root: str,
    watch_all: bool,
    plan_name: str | None,
) -> _WatchState:
    active_plan_name, last_id = _default_watch_state(project_root, watch_all, plan_name)
    return _WatchState(active_plan_name=active_plan_name, last_id=last_id)


def _print_watch_scope(
    watch_all: bool,
    plan_name: str | None,
    active_plan_name: str | None,
) -> None:
    if plan_name:
        console.print(f"  plan: {plan_name}", style="dim")
    elif watch_all:
        console.print("  scope: all plans", style="dim")
    elif active_plan_name:
        console.print(f"  inferred plan: {active_plan_name}", style="dim")
    else:
        console.print("  scope: live tail (no active plan inferred)", style="dim")
    console.print("  (Ctrl-C to exit)\n", style="dim")


def _refresh_watch_plan_filter(
    project_root: str,
    state: _WatchState,
    watch_all: bool,
    plan_name: str | None,
) -> None:
    if state.active_plan_name is None and not watch_all and plan_name is None:
        state.active_plan_name = _infer_plan_name_from_active_tasks(project_root)


def _reset_watch_state_if_needed(
    project_root: str,
    state: _WatchState,
    watch_all: bool,
    plan_name: str | None,
) -> None:
    current_max = latest_event_id(project_root)
    if current_max >= state.last_id:
        return

    console.print("\n  --- [bold]new run[/bold] ---\n", style="dim")
    state.last_id = 0
    state.last_task = ""
    _TASK_COLORS.clear()
    if not watch_all and plan_name is None:
        state.active_plan_name, state.last_id = _default_watch_state(project_root, False, None)


def _print_new_watch_events(
    project_root: str,
    state: _WatchState,
    agents: dict[str, str],
) -> None:
    events = read_events(project_root, after_id=state.last_id, plan_name=state.active_plan_name)
    for event in events:
        state.last_id = max(state.last_id, event.get("id", 0))
        typed_event = deserialize_event(event)
        line = _format_event(typed_event, _watch_event_time(event), agents=agents)
        if line is not None:
            _print_watch_event_line(typed_event, line, state)


def _watch_event_time(event: dict[str, object]) -> str:
    ts_raw = str(event.get("ts", ""))
    return ts_raw[11:19] if len(ts_raw) >= 19 else ts_raw


def _print_watch_event_line(
    event: DgovEvent,
    line: RenderableType,
    state: _WatchState,
) -> None:
    task = getattr(event, "task_slug", "")
    if isinstance(event, EvtTaskDispatched) and state.last_task:
        console.print("")
    if task:
        state.last_task = task
    console.print(line)
