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

logger = logging.getLogger(__name__)

_STARTUP_TIME = time.time()
_UI_REFRESH_PER_SECOND = 2
_INPUT_POLL_INTERVAL = 0.05


def state_color(state: str) -> str:
    return {
        "active": "yellow",
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


def fmt_duration(seconds: int) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m}m"


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
    post_exit_attach: str = ""
    preview_lines: list[str] = field(default_factory=list)
    preview_visible: bool = False
    monitor_alive: bool = False


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

        # Capture preview lines for the selected pane
        preview: list[str] = []
        with state.lock:
            sel_idx = state.selected
            want_preview = state.preview_visible
        if want_preview and panes and 0 <= sel_idx < len(panes):
            slug = panes[sel_idx].get("slug", "")
            raw = tail_worker_log(session_root, slug, lines=5)
            if raw:
                preview = [ln for ln in raw.splitlines() if ln.strip()][-5:]

        with state.lock:
            state.panes = panes
            state.events = events
            state.branch = branch
            state.last_refresh = time.time()
            state.error = ""
            state.preview_lines = preview
            # Add monitor health info to state for header
            m_ts = monitor_status.get("timestamp", 0)
            state.monitor_alive = (time.time() - m_ts < 45) if m_ts else False

    except Exception as exc:
        with state.lock:
            state.error = str(exc)
            state.last_refresh = time.time()


def data_thread(state: DashboardState, interval: float) -> None:
    while not state.stop_event.is_set():
        fetch_panes(state)
        # Detect stale binary
        try:
            import dgov as _pkg

            _mod_file = getattr(_pkg, "__file__", "")
            if _mod_file and os.path.getmtime(_mod_file) > _STARTUP_TIME:
                with state.lock:
                    state.error = "dgov reinstalled — restart dashboard (q then dgov resume)"
        except (OSError, AttributeError):
            pass
        state.force_refresh.wait(timeout=interval)
        state.force_refresh.clear()


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


def _build_worker_table(panes: list[dict], selected: int) -> Table:
    table = Table(expand=True, box=None, padding=(0, 1), show_header=True)
    table.add_column("Slug", ratio=3, no_wrap=True)
    table.add_column("Agent", ratio=2, no_wrap=True)
    table.add_column("State", width=16, no_wrap=True)

    sorted_panes = _sort_panes_hierarchical(panes, selected)

    for p, indent_level, is_last_child, orig_idx in sorted_panes:
        pstate = p.get("state", "active")
        is_selected = orig_idx == selected
        role = p.get("role", "worker")

        # Use monitor classification if available for active panes
        m_class = p.get("monitor_classification")
        display_state = pstate
        if pstate == "active" and m_class:
            display_state = m_class

        color = state_color(display_state)
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
        dur = fmt_duration(int(p.get("duration_s", 0)))

        # Commit indicator
        has_commits = p.get("monitor_has_commits", False)
        commit_tag = " [bold green]C[/bold green]" if has_commits else ""

        state_text = Text.from_markup(f"[{color}]{display_state} {dur}[/{color}]{commit_tag}")

        table.add_row(slug_display, agent, state_text, style=style)

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
        preview_lines = list(state.preview_lines)
        preview_visible = state.preview_visible
        monitor_alive = state.monitor_alive

    ts = (
        time.strftime("%a %b %d, %I:%M:%S %p %Z", time.localtime(last_refresh))
        if last_refresh
        else "--"
    )
    header_text = Text()
    header_text.append(
        f" DGOV v{__version__} \u2502 {branch} \u2502 {ts} \u2502 {len(panes)} workers"
    )

    mon_color = "green" if monitor_alive else "red"
    header_text.append(" \u2502 ", style="dim")
    header_text.append("\u25cf", style=mon_color)

    if error:
        header_text.append(f"  err: {error}", style="red")

    table = _build_worker_table(panes, selected)

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
    selected_slug = ""
    if panes and 0 <= selected < len(panes):
        selected_slug = panes[selected].get("slug", "")

    show_preview = preview_visible and bool(preview_lines) and bool(selected_slug)

    preview_text = Text("\n".join(preview_lines)) if show_preview else Text("")
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
        from dgov.merger import merge_worker_pane

        try:
            merge_worker_pane(state.project_root, slug, session_root=state.session_root)
        except Exception:
            logger.exception("Dashboard merge failed for %s", slug)
            with state.lock:
                state.error = f"Merge failed for {slug}"
    elif action == "close":
        from dgov.lifecycle import close_worker_pane

        try:
            close_worker_pane(state.project_root, slug, session_root=state.session_root)
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
            refresh_per_second=_UI_REFRESH_PER_SECOND,
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
                if ch == "q":
                    break
                elif ch == "j":
                    with state.lock:
                        state.selected = min(state.selected + 1, max(0, len(state.panes) - 1))
                    live.refresh()
                elif ch == "k":
                    with state.lock:
                        state.selected = max(0, state.selected - 1)
                    live.refresh()
                elif ch == "r":
                    state.force_refresh.set()
                    live.refresh()
                elif ch == "\r" or ch == "\n":
                    # Switch to worker's background tmux window
                    with state.lock:
                        panes = list(state.panes)
                        sel = state.selected
                    if panes and 0 <= sel < len(panes):
                        pane_id = panes[sel].get("pane_id", "")
                        if pane_id:
                            _switch_to_worker_window(pane_id)
                elif ch == "m":
                    with state.lock:
                        panes = list(state.panes)
                        sel = state.selected
                    if panes and 0 <= sel < len(panes):
                        slug = panes[sel]["slug"]
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
                        live.start()
                        live.refresh()
                elif ch == "x":
                    with state.lock:
                        panes = list(state.panes)
                        sel = state.selected
                    if panes and 0 <= sel < len(panes):
                        slug = panes[sel]["slug"]
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
                        live.start()
                        live.refresh()
                elif ch == "p":
                    with state.lock:
                        state.preview_visible = not state.preview_visible
                    state.force_refresh.set()
                    live.refresh()
                elif ch == "a":
                    # Alias for Enter — switch to worker window
                    with state.lock:
                        panes = list(state.panes)
                        sel = state.selected
                    if panes and 0 <= sel < len(panes):
                        pane_id = panes[sel].get("pane_id", "")
                        if pane_id:
                            _switch_to_worker_window(pane_id)
    finally:
        state.stop_event.set()
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
