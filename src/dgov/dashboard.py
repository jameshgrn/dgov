"""Rich-based live dashboard for dgov pane management."""

from __future__ import annotations

import fcntl
import json as _json
import logging
import os
import select
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from dgov import __version__

# MLX overflow test passed
logger = logging.getLogger(__name__)

_STARTUP_TIME = time.time()
_UI_REFRESH_PER_SECOND = 4
_INPUT_POLL_INTERVAL = 0.05
_VISIBLE_ROWS = 15


def state_color(state: str) -> str:
    return {
        "active": "bright_cyan",
        "done": "green",
        "merged": "green",
        "failed": "red",
        "abandoned": "red",
        "timed_out": "red",
        "escalated": "magenta",
        "superseded": "magenta",
        "closed": "dim",
        "stuck": "bold red",
        "waiting_input": "bold yellow",
        "committing": "bold green",
        "working": "yellow",
        "idle": "dim",
    }.get(state, "white")


def fmt_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return f"{seconds:.3f}s"
    if seconds < 3600:
        return f"{int(seconds) // 60}m{int(seconds) % 60}s"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"{h}h{m}m"


@dataclass
class DashboardPreview:
    slug: str
    lines: list[str]


@dataclass
class DashboardState:
    panes: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    last_refresh: float = 0.0
    project_root: str = "."
    session_root: str | None = None
    branch: str = ""
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    force_refresh: threading.Event = field(default_factory=threading.Event)
    selected: int = 0
    scroll_offset: int = 0
    post_exit_attach: str = ""
    preview: DashboardPreview | None = None
    preview_visible: bool = True
    monitor_timestamp: float = 0.0
    # Done notification tracking
    recent_done_slugs: list[str] = field(default_factory=list)
    done_bell_rung: bool = False
    done_notification_ts: float = 0.0  # when the last batch arrived
    # Eval contract from active DAG run (typed persistence, never blobs)
    eval_summary: str = ""


def _get_branch(project_root: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "?"
    except (subprocess.TimeoutExpired, OSError):
        return "?"


_branch_cache: dict[str, tuple[float, str]] = {}
_BRANCH_CACHE_TTL = 10.0


def _get_branch_cached(project_root: str) -> str:
    now = time.time()
    cached = _branch_cache.get(project_root)
    if cached is not None:
        ts, val = cached
        if now - ts < _BRANCH_CACHE_TTL:
            return val
    val = _get_branch(project_root)
    _branch_cache[project_root] = (now, val)
    return val


def fetch_spans_and_traces(session_root: str, trace_id: str) -> tuple[list[dict], list[dict]]:
    """Fetch span and tool-trace data for a pane by trace_id (slug)."""
    try:
        from dgov.spans import get_spans, get_tool_trace

        spans = get_spans(session_root, trace_id)
        tool_trace = get_tool_trace(session_root, trace_id)
        return spans, tool_trace
    except Exception:
        return [], []


def format_span_summary(spans: list[dict]) -> str:
    """Format a concise summary line from span data."""
    if not spans:
        return ""

    parts = []
    for s in sorted(spans, key=lambda x: x.get("started_at", "")):
        kind = s.get("span_kind", "")
        duration_ms = s.get("duration_ms", -1)
        outcome = s.get("outcome", "pending")
        verdict = s.get("verdict", "")

        dur_str = ""
        if duration_ms >= 0:
            if duration_ms < 1000:
                dur_str = f"{duration_ms:.0f}ms"
            else:
                dur_str = f"{duration_ms / 1000:.1f}s"

        part = f"{kind}"
        if dur_str:
            part += f"[{dur_str}]"
        if kind == "review" and verdict:
            part += f" {verdict}"
        elif outcome not in {"", "pending", "success"}:
            part += f" {outcome}"

        parts.append(part)

    return " ".join(parts)


def format_tool_trace_activity(tool_trace: list[dict], max_lines: int = 5) -> str:
    """Format recent tool-trace activity for dashboard preview."""
    if not tool_trace:
        return ""

    tool_calls = [t for t in tool_trace if t.get("action_type") == "tool_call"]
    thinking = [t for t in tool_trace if t.get("action_type") == "thinking"]
    results = [t for t in tool_trace if t.get("action_type") == "tool_result"]

    lines: list[str] = []

    if tool_calls:
        recent = tool_calls[-3:]
        call_names = [t.get("tool_name", "unknown") for t in recent]
        lines.append(f"tools: {', '.join(call_names)}")

    if thinking:
        for t in thinking[-2:]:
            thinking_text = " ".join(t.get("thinking", "").split())[:60]
            if thinking_text:
                suffix = "..." if len(thinking_text) == 60 else ""
                lines.append(f"thought: {thinking_text}{suffix}")

    if results:
        t = results[-1]
        status = t.get("tool_status", "")
        result_text = " ".join(t.get("tool_result", "").split())[:50]
        if status and result_text:
            suffix = "..." if len(result_text) == 50 else ""
            lines.append(f"result[{status}]: {result_text}{suffix}")

    return "\n".join(lines[:max_lines]) if lines else ""


def _format_trace_data(session_root: str, slug: str) -> list[str]:
    """Format structured trace data for the dashboard preview."""

    spans, tool_trace = fetch_spans_and_traces(session_root, slug)
    if not spans and not tool_trace:
        return []

    lines: list[str] = []

    span_summary = format_span_summary(spans)
    if span_summary:
        lines.append(f"phases: {span_summary}")

    tool_activity = format_tool_trace_activity(tool_trace, max_lines=5)
    if tool_activity:
        lines.extend(tool_activity.splitlines())

    return lines


def fetch_panes(state: DashboardState) -> None:
    from dgov.persistence import STATE_DIR, read_events
    from dgov.status import list_worker_panes, tail_worker_log

    try:
        panes = list_worker_panes(
            state.project_root,
            session_root=state.session_root,
            include_freshness=False,
            include_prompt=False,
        )
        branch = _get_branch_cached(state.project_root)
        session_root = state.session_root or state.project_root

        # Monitor status
        monitor_status = {}
        monitor_file = Path(session_root) / STATE_DIR / "monitor" / "status.json"
        if monitor_file.is_file():
            try:
                monitor_status = _json.loads(monitor_file.read_text())
            except (ValueError, OSError):
                pass

        # Merge monitor classifications into panes
        monitor_workers = {w["slug"]: w for w in monitor_status.get("workers", [])}
        for p in panes:
            slug = p.get("slug", "")
            if slug in monitor_workers:
                p["monitor_classification"] = monitor_workers[slug].get("classification")
                p["monitor_has_commits"] = monitor_workers[slug].get("has_commits")

        # Progress files
        progress_dir = Path(session_root) / STATE_DIR / "progress"
        for p in panes:
            slug = p.get("slug", "")
            progress_file = progress_dir / f"{slug}.json"
            if progress_file.is_file():
                try:
                    data = _json.loads(progress_file.read_text())
                    msg = data.get("message", "")
                    status = data.get("status", "")
                    turn = data.get("turn", 0)
                    p["activity"] = f"[T{turn}] {msg}" if msg else status
                except (ValueError, OSError):
                    pass

        # Log tail fallback — only if no summary AND no activity
        for p in panes:
            if not p.get("summary") and not p.get("activity"):
                slug = p.get("slug", "")
                log_tail = tail_worker_log(session_root, slug, lines=2)
                if log_tail:
                    lines = [ln.strip() for ln in log_tail.splitlines() if ln.strip()]
                    p["activity"] = lines[-1] if lines else ""

        events = read_events(session_root, limit=12)

        # Track new pane_done events for notifications
        new_done_slugs: list[str] = []
        with state.lock:
            known_slugs = set(state.recent_done_slugs)
        for ev in events:
            if ev.get("event") == "pane_done":
                slug = ev.get("pane", "")
                if slug and slug not in known_slugs:
                    new_done_slugs.append(slug)

        # Capture preview lines for the selected pane using trace data when available
        preview: DashboardPreview | None = None
        with state.lock:
            sel_idx = state.selected
            want_preview = state.preview_visible
        selected_pane = _selected_visible_pane(panes, sel_idx)
        if want_preview and selected_pane is not None and selected_pane.get("state") == "active":
            slug = selected_pane.get("slug", "")
            preview_lines = _format_trace_data(session_root, slug)
            if not preview_lines:
                raw = tail_worker_log(session_root, slug, lines=5)
                if raw:
                    preview_lines = [ln for ln in raw.splitlines() if ln.strip()][-5:]
            preview = DashboardPreview(slug=slug, lines=preview_lines)

        # Compute eval summary from active DAG runs (typed tables)
        eval_summary = ""
        try:
            from dgov.persistence import list_active_dag_runs, list_dag_tasks

            active_runs = list_active_dag_runs(session_root)
            if active_runs:
                run = active_runs[0]
                run_evals = run.get("evals", [])
                if run_evals:
                    links = run.get("unit_eval_links", [])
                    tasks = list_dag_tasks(session_root, run["id"])
                    task_status = {t["slug"]: t.get("status", "pending") for t in tasks}
                    eval_units: dict[str, list[str]] = {}
                    for lk in links:
                        eval_units.setdefault(lk["eval_id"], []).append(lk["unit_slug"])
                    passed = 0
                    failed = 0
                    for ev in run_evals:
                        units = eval_units.get(ev["eval_id"], [])
                        if units and all(task_status.get(u) == "merged" for u in units):
                            passed += 1
                        elif any(task_status.get(u) in ("failed", "abandoned") for u in units):
                            failed += 1
                    total = len(run_evals)
                    if failed:
                        eval_summary = f"E:{passed}/{total} ({failed} FAIL)"
                    else:
                        eval_summary = f"E:{passed}/{total}"
        except Exception:
            pass

        with state.lock:
            state.panes = panes
            state.events = events
            state.branch = branch
            state.last_refresh = time.time()
            state.error = ""
            state.preview = preview
            state.eval_summary = eval_summary
            m_ts = monitor_status.get("timestamp", 0)
            state.monitor_timestamp = float(m_ts) if m_ts else 0.0
            # Update done notification tracking
            if new_done_slugs:
                state.recent_done_slugs = (new_done_slugs + state.recent_done_slugs)[:5]
                state.done_bell_rung = False
                state.done_notification_ts = time.time()
            elif state.recent_done_slugs and time.time() - state.done_notification_ts > 30:
                # Expire stale done notifications after 30s
                state.recent_done_slugs = []

    except Exception as exc:
        with state.lock:
            state.error = str(exc)
            state.last_refresh = time.time()


def _refresh_dashboard_state(state: DashboardState) -> None:
    fetch_panes(state)
    try:
        import dgov as _pkg

        mod_file = getattr(_pkg, "__file__", "")
        if mod_file and os.path.getmtime(mod_file) > _STARTUP_TIME:
            with state.lock:
                state.error = "dgov reinstalled — restart dashboard (q then dgov resume)"
    except (OSError, AttributeError):
        pass


def _wake_dashboard_observer(state: DashboardState) -> None:
    from dgov.persistence import _notify_waiters

    _notify_waiters(state.session_root or state.project_root)


def _refresh_preview_for_selection_change(state: DashboardState) -> None:
    with state.lock:
        preview_visible = state.preview_visible
    if not preview_visible:
        return
    state.force_refresh.set()
    _wake_dashboard_observer(state)


def _visible_pane_rows(panes: list[dict]) -> tuple[list[tuple[int, dict]], str]:
    pane_rows = list(enumerate(panes))
    active_rows = [row for row in pane_rows if row[1].get("state") == "active"]
    if active_rows:
        return active_rows, "active"
    return pane_rows, "all"


def _sort_pane_rows_hierarchical(
    pane_rows: list[tuple[int, dict]],
) -> list[tuple[dict, int, bool, int]]:
    """Sort visible pane rows while preserving original pane indices."""
    ltgovs: list[tuple[int, dict]] = []
    children: dict[str, list[tuple[int, dict]]] = {}
    standalone: list[tuple[int, dict]] = []

    for orig_idx, pane in pane_rows:
        role = pane.get("role", "worker")
        parent = pane.get("parent_slug", "")
        if role == "lt-gov":
            ltgovs.append((orig_idx, pane))
        elif parent:
            children.setdefault(parent, []).append((orig_idx, pane))
        else:
            standalone.append((orig_idx, pane))

    result: list[tuple[dict, int, bool, int]] = []

    for orig_idx, pane in ltgovs:
        result.append((pane, 0, False, orig_idx))
        kids = children.pop(pane.get("slug", ""), [])
        for child_pos, (child_idx, child) in enumerate(kids):
            result.append((child, 1, child_pos == len(kids) - 1, child_idx))

    for kids in children.values():
        for child_pos, (child_idx, child) in enumerate(kids):
            result.append((child, 1, child_pos == len(kids) - 1, child_idx))

    for orig_idx, pane in standalone:
        result.append((pane, 0, False, orig_idx))

    return result


def _sorted_visible_panes(panes: list[dict]) -> tuple[list[tuple[dict, int, bool, int]], str]:
    pane_rows, mode = _visible_pane_rows(panes)
    return _sort_pane_rows_hierarchical(pane_rows), mode


def _effective_selected_index(panes: list[dict], selected: int) -> int:
    """Return a visible selection, defaulting to the first visible pane."""
    order = _selection_order(panes)
    if selected in order:
        return selected
    return order[0] if order else 0


def _selected_visible_pane(panes: list[dict], selected: int) -> dict | None:
    """Return the selected pane from the currently visible pane set."""
    effective_selected = _effective_selected_index(panes, selected)
    sorted_panes, _mode = _sorted_visible_panes(panes)
    for pane, _indent_level, _is_last_child, orig_idx in sorted_panes:
        if orig_idx == effective_selected:
            return pane
    return None


def data_thread(state: DashboardState, _interval: float) -> None:
    from dgov.persistence import _wait_for_notify

    session_root = state.session_root or state.project_root
    _refresh_dashboard_state(state)
    while not state.stop_event.is_set():
        # Per-process notify pipes: dashboard gets its own FIFO, wakes
        # instantly on any event. 5s ceiling for duration display freshness.
        _wait_for_notify(session_root, 5.0)
        if state.stop_event.is_set():
            break
        state.force_refresh.clear()
        _refresh_dashboard_state(state)


def _sort_panes_hierarchical(
    panes: list[dict], selected: int
) -> list[tuple[dict, int, bool, int]]:
    """Sort panes: LT-GOVs first with children nested beneath, then standalone.

    Returns list of ``(pane, indent_level, is_last_child, original_index)``.
    """
    ltgovs: list[tuple[int, dict]] = []
    children: dict[str, list[tuple[int, dict]]] = {}
    standalone: list[tuple[int, dict]] = []

    for i, p in enumerate(panes):
        role = p.get("role", "worker")
        parent = p.get("parent_slug", "")
        if role == "lt-gov":
            ltgovs.append((i, p))
        elif parent:
            children.setdefault(parent, []).append((i, p))
        else:
            standalone.append((i, p))

    result: list[tuple[dict, int, bool, int]] = []

    for orig_idx, p in ltgovs:
        result.append((p, 0, False, orig_idx))
        kids = children.pop(p.get("slug", ""), [])
        for j, (child_idx, child) in enumerate(kids):
            result.append((child, 1, j == len(kids) - 1, child_idx))

    # Orphan children whose parent LT-GOV wasn't found
    for _parent_slug, kids in children.items():
        for j, (child_idx, child) in enumerate(kids):
            result.append((child, 1, j == len(kids) - 1, child_idx))

    for orig_idx, p in standalone:
        result.append((p, 0, False, orig_idx))

    return result


def _selection_order(panes: list[dict]) -> list[int]:
    sorted_panes, _mode = _sorted_visible_panes(panes)
    return [orig_idx for _, _, _, orig_idx in sorted_panes]


def _move_selection(panes: list[dict], selected: int, step: int) -> tuple[int, int]:
    order = _selection_order(panes)
    if not order:
        return 0, 0
    try:
        position = order.index(selected)
    except ValueError:
        position = 0
    new_position = min(max(position + step, 0), len(order) - 1)
    return order[new_position], new_position


def _build_worker_table(panes: list[dict], selected: int, scroll_offset: int = 0) -> Table:
    table = Table(expand=True, box=None, padding=(0, 1), show_header=True)
    table.add_column("Slug", ratio=3, no_wrap=True)
    table.add_column("Agent", ratio=2, no_wrap=True)
    table.add_column("State", width=10, no_wrap=True)
    table.add_column("Phase", width=12, no_wrap=True)
    table.add_column("Duration", width=8, no_wrap=True)

    sorted_panes, _mode = _sorted_visible_panes(panes)
    effective_selected = _effective_selected_index(panes, selected)

    visible_end = scroll_offset + _VISIBLE_ROWS
    sorted_panes = sorted_panes[scroll_offset:visible_end]

    for p, indent_level, is_last_child, orig_idx in sorted_panes:
        pstate = p.get("state", "active")
        is_selected = orig_idx == effective_selected
        role = p.get("role", "worker")

        color = state_color(pstate)
        style = f"bold {color}" if is_selected else color

        if role == "lt-gov":
            prefix = "\u25c6 " if is_selected else "  \u25c7 "
            style = "bold magenta"
        elif indent_level > 0:
            prefix = "  \u2514\u2500 " if is_last_child else "  \u251c\u2500 "
        else:
            prefix = "\u25b8 " if is_selected else "  "

        slug_display = Text(f"{prefix}{p.get('slug', '')}")
        agent = Text(p.get("agent", "?"))
        # Compute duration live from created_at so it ticks at render rate
        created_at = p.get("created_at") or time.time()
        dur = fmt_duration(time.time() - created_at)

        # Commit indicator
        has_commits = p.get("monitor_has_commits", False)
        commit_tag = " [bold green]C[/bold green]" if has_commits else ""

        state_text = Text.from_markup(f"[{color}]{pstate}[/{color}]{commit_tag}")
        phase = p.get("phase", "")
        monitor_phase = p.get("monitor_classification", "")
        preserved_artifacts = p.get("preserved_artifacts")
        nonterminal_monitor_phases = {
            "starting",
            "working",
            "testing",
            "committing",
            "idle",
            "waiting_input",
            "stuck",
            "unknown",
            "hook_match",
        }
        if pstate != "active":
            phase = ""
            if isinstance(preserved_artifacts, dict):
                phase = "resume" if preserved_artifacts.get("recoverable") else "inspect"
        elif monitor_phase in nonterminal_monitor_phases:
            phase = monitor_phase
        elif phase in {"done", "failed", "merged", "closed", "abandoned"}:
            phase = ""

        phase_text = Text(phase)
        dur_text = Text.from_markup(f"[dim]{dur}[/dim]")

        table.add_row(slug_display, agent, state_text, phase_text, dur_text, style=style)

    return table


def _create_dashboard_layout() -> Layout:
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="header", size=1),
        Layout(name="body"),
        Layout(name="footer", size=1),
    )
    layout["body"].split_column(
        Layout(name="workers", ratio=3),
        Layout(name="bottom", ratio=1),
    )
    layout["body"]["bottom"].split_row(
        Layout(name="monitor", ratio=1),
        Layout(name="preview", ratio=1, visible=False),
    )
    return layout


def _build_layout(
    state: DashboardState,
    term_width: int | None = None,
    term_height: int | None = None,
    layout: Layout | None = None,
) -> Layout:
    with state.lock:
        panes = list(state.panes)
        events = list(state.events)
        branch = state.branch
        last_refresh = state.last_refresh
        error = state.error
        selected = state.selected
        preview = state.preview
        preview_visible = state.preview_visible
        monitor_timestamp = state.monitor_timestamp
        scroll_offset = state.scroll_offset
        eval_summary = state.eval_summary

    ts = (
        time.strftime("%a %b %d, %I:%M:%S %p %Z", time.localtime(last_refresh))
        if last_refresh
        else "--"
    )
    sorted_visible_panes, pane_mode = _sorted_visible_panes(panes)
    visible_count = len(sorted_visible_panes)
    header_text = Text()
    worker_summary = (
        f"{visible_count} active / {len(panes)} total"
        if pane_mode == "active"
        else f"{len(panes)} panes"
    )
    header_text.append(f" DGOV v{__version__} \u2502 {branch} \u2502 {ts} \u2502 {worker_summary}")

    monitor_alive = bool(monitor_timestamp) and (time.time() - monitor_timestamp < 45)
    mon_color = "green" if monitor_alive else "red"
    header_text.append(" \u2502 ", style="dim")
    header_text.append("\u25cf", style=mon_color)

    if eval_summary:
        header_text.append(" \u2502 ", style="dim")
        eval_color = "red" if "FAIL" in eval_summary else "green"
        header_text.append(eval_summary, style=eval_color)

    # Show done notification banner
    with state.lock:
        done_slugs = list(state.recent_done_slugs)
        bell_rung = state.done_bell_rung

    if done_slugs:
        header_text.append(" \u2502 ", style="dim")
        # Show up to 3 slugs in the banner
        display_slugs = done_slugs[:3]
        slug_str = ", ".join(display_slugs)
        more = (
            f" (+{len(done_slugs) - len(display_slugs)})"
            if len(done_slugs) > len(display_slugs)
            else ""
        )
        header_text.append(f"\u2713 {slug_str}{more}", style="bold green")
        # Ring bell once per batch of new done events
        if not bell_rung and not error:
            try:
                sys.stdout.write("\x07")
                sys.stdout.flush()
            except Exception:
                pass
            with state.lock:
                state.done_bell_rung = True

    if error:
        header_text.append(f"  err: {error}", style="red")

    table = _build_worker_table(panes, selected, scroll_offset)

    # Build events list
    ev_text = Text()
    for ev in reversed(events):
        kind = ev.get("event", "")
        slug = ev.get("pane", "")
        # Highlight monitor actions
        if kind.startswith("monitor_"):
            if kind == "monitor_tick":
                style = "dim"
                # For ticks, slug is 'monitor', data has 'states'
                import json

                try:
                    data = json.loads(ev.get("data", "{}"))
                except (ValueError, TypeError):
                    data = {}
                states = data.get("states", "")
                slug = f"({states})" if states else ""
            else:
                style = "bold cyan"
        elif kind in ("done", "merged", "closed"):
            style = "green"
        elif kind in ("failed", "error"):
            style = "red"
        else:
            style = "dim"

        from datetime import datetime

        try:
            dt = datetime.fromisoformat(ev.get("ts", "")).astimezone()
            ev_time = dt.strftime("%I:%M %p")
        except (ValueError, TypeError):
            ev_time = "--:--"

        ev_text.append(f"{ev_time} ", style="dim")
        ev_text.append(f"{kind:<18} ", style=style)
        ev_text.append(f"{slug}\n")

    footer = Text(
        " q:quit  j/k/\u2191\u2193:scroll  Enter:view  r:refresh  m:merge  x:close  p:preview",
        style="dim",
    )
    worker_panel = Panel(table, title="Workers", border_style="blue", box=box.ROUNDED)

    # Determine selected slug for preview title
    selected_pane = _selected_visible_pane(panes, selected)
    selected_slug = selected_pane.get("slug", "") if selected_pane else ""

    show_preview = (
        preview_visible
        and selected_pane is not None
        and selected_pane.get("state") == "active"
        and preview is not None
        and preview.slug == selected_slug
        and bool(preview.lines)
    )
    preview_text = Text("\n".join(preview.lines)) if show_preview and preview else Text("")
    preview_title = f"Output: {selected_slug}" if selected_slug else "Output"

    if layout is None:
        layout = _create_dashboard_layout()

    layout["header"].update(header_text)
    layout["footer"].update(footer)
    layout["body"]["workers"].update(worker_panel)
    layout["body"]["bottom"]["monitor"].update(Panel(ev_text, title="Monitor", border_style="dim"))
    layout["body"]["bottom"]["preview"].update(
        Panel(
            preview_text,
            title=preview_title,
            border_style="cyan",
        )
    )
    layout["body"]["bottom"]["preview"].visible = show_preview
    return layout


def _switch_to_worker_window(pane_id: str) -> None:
    """Switch to the worker's background tmux window."""
    try:
        subprocess.run(
            ["tmux", "select-window", "-t", pane_id],
            capture_output=True,
        )
    except Exception:
        return


def _execute_action(state: DashboardState, action: str, slug: str) -> None:
    if action == "merge":
        from dgov.executor import run_land_only

        try:
            result = run_land_only(state.project_root, slug, session_root=state.session_root)
            if result.error:
                with state.lock:
                    state.error = result.error
                return
        except Exception:
            logger.exception("Dashboard merge failed for %s", slug)
            with state.lock:
                state.error = f"Merge failed for {slug}"
    elif action == "close":
        from dgov.executor import run_close_only

        try:
            result = run_close_only(state.project_root, slug, session_root=state.session_root)
            if not result.closed:
                with state.lock:
                    state.error = f"Close failed for {slug}"
        except Exception:
            logger.exception("Dashboard close failed for %s", slug)
            with state.lock:
                state.error = f"Close failed for {slug}"


def _acquire_dashboard_lock(session_root: str) -> tuple[Path, IO[str]] | None:
    """Acquire an exclusive flock on the dashboard pidfile.

    Returns (pidfile_path, lock_fd) on success, None on failure.
    """
    pidfile = Path(session_root) / ".dgov" / "dashboard.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)

    # Open (or create) the pidfile for read+write
    fd = open(pidfile, "a+")  # noqa: SIM115
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fd.close()
        logger.info("Another dashboard holds the lock on %s", pidfile)
        return None

    # We hold the lock — write our PID
    fd.seek(0)
    fd.truncate()
    fd.write(str(os.getpid()))
    fd.flush()
    return pidfile, fd


def run_dashboard(
    project_root: str,
    session_root: str | None = None,
    refresh_interval: float = 1.0,
) -> None:
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)
    lock_result = _acquire_dashboard_lock(session_root)
    if lock_result is None:
        return
    pidfile, lock_fd = lock_result
    console = Console(force_terminal=sys.stdout.isatty())
    is_tty = sys.stdin.isatty()

    state = DashboardState(
        project_root=project_root,
        session_root=session_root,
    )
    ui_refresh_per_second = (
        1.0 / max(refresh_interval, 0.05) if refresh_interval > 0 else _UI_REFRESH_PER_SECOND
    )

    thread = threading.Thread(target=data_thread, args=(state, refresh_interval), daemon=True)
    thread.start()

    old_settings = None
    if is_tty:
        try:
            old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except termios.error:
            old_settings = None
            is_tty = False

    layout = _create_dashboard_layout()

    def _render_dashboard() -> Layout:
        size = console.size
        return _build_layout(
            state,
            term_width=size.width,
            term_height=size.height,
            layout=layout,
        )

    try:
        with Live(
            console=console,
            auto_refresh=True,
            get_renderable=_render_dashboard,
            refresh_per_second=ui_refresh_per_second,
            transient=False,
            screen=True,
        ) as live:
            while not state.stop_event.is_set():
                if not is_tty:
                    time.sleep(0.25)
                    continue

                # Key handling with select
                rlist, _, _ = select.select([sys.stdin], [], [], _INPUT_POLL_INTERVAL)
                if not rlist:
                    continue

                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    seq = sys.stdin.read(2)
                    if seq == "[A":
                        ch = "k"
                    elif seq == "[B":
                        ch = "j"
                    elif seq == "[H":
                        ch = "g"
                    elif seq == "[F":
                        ch = "G"
                if ch == "q":
                    break
                elif ch == "j":
                    with state.lock:
                        state.selected, position = _move_selection(state.panes, state.selected, 1)
                        if position >= state.scroll_offset + _VISIBLE_ROWS:
                            state.scroll_offset = position - _VISIBLE_ROWS + 1
                    _refresh_preview_for_selection_change(state)
                    live.refresh()
                elif ch == "k":
                    with state.lock:
                        state.selected, position = _move_selection(state.panes, state.selected, -1)
                        if position < state.scroll_offset:
                            state.scroll_offset = position
                    _refresh_preview_for_selection_change(state)
                    live.refresh()
                elif ch == "g":
                    with state.lock:
                        order = _selection_order(state.panes)
                        if order:
                            state.selected = order[0]
                            state.scroll_offset = 0
                    _refresh_preview_for_selection_change(state)
                    live.refresh()
                elif ch == "G":
                    with state.lock:
                        order = _selection_order(state.panes)
                        if order:
                            state.selected = order[-1]
                            state.scroll_offset = max(0, len(order) - _VISIBLE_ROWS)
                    _refresh_preview_for_selection_change(state)
                    live.refresh()
                elif ch == "r":
                    state.force_refresh.set()
                    _wake_dashboard_observer(state)
                    live.refresh()
                elif ch == "\r" or ch == "\n":
                    # Switch to worker's background tmux window
                    with state.lock:
                        selected_pane = _selected_visible_pane(state.panes, state.selected)
                    if selected_pane is not None:
                        pane_id = selected_pane.get("pane_id", "")
                        if pane_id:
                            _switch_to_worker_window(pane_id)
                elif ch == "m":
                    with state.lock:
                        selected_pane = _selected_visible_pane(state.panes, state.selected)
                    if selected_pane is not None:
                        slug = selected_pane["slug"]
                        if old_settings:
                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                        live.stop()
                        console.print(f"Merge [bold]{slug}[/bold]? (y/n) ", end="")
                        if is_tty:
                            tty.setcbreak(sys.stdin.fileno())
                            confirm = sys.stdin.read(1)
                            if old_settings:
                                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                                tty.setcbreak(sys.stdin.fileno())
                        else:
                            confirm = "n"
                        console.print()
                        if confirm == "y":
                            _execute_action(state, "merge", slug)
                            state.force_refresh.set()
                            _wake_dashboard_observer(state)
                        live.start()
                        live.refresh()
                elif ch == "x":
                    with state.lock:
                        selected_pane = _selected_visible_pane(state.panes, state.selected)
                    if selected_pane is not None:
                        slug = selected_pane["slug"]
                        if old_settings:
                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                        live.stop()
                        console.print(f"Close [bold]{slug}[/bold]? (y/n) ", end="")
                        if is_tty:
                            tty.setcbreak(sys.stdin.fileno())
                            confirm = sys.stdin.read(1)
                            if old_settings:
                                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                                tty.setcbreak(sys.stdin.fileno())
                        else:
                            confirm = "n"
                        console.print()
                        if confirm == "y":
                            _execute_action(state, "close", slug)
                            state.force_refresh.set()
                            _wake_dashboard_observer(state)
                        live.start()
                        live.refresh()
                elif ch == "p":
                    with state.lock:
                        state.preview_visible = not state.preview_visible
                    state.force_refresh.set()
                    _wake_dashboard_observer(state)
                    live.refresh()
                elif ch == "a":
                    # Alias for Enter — switch to worker window
                    with state.lock:
                        selected_pane = _selected_visible_pane(state.panes, state.selected)
                    if selected_pane is not None:
                        pane_id = selected_pane.get("pane_id", "")
                        if pane_id:
                            _switch_to_worker_window(pane_id)
    finally:
        state.stop_event.set()
        _wake_dashboard_observer(state)
        if old_settings:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass
        if pidfile:
            try:
                lock_fd.close()
                pidfile.unlink(missing_ok=True)
            except OSError:
                pass
