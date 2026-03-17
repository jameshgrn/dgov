"""Rich-based live dashboard for dgov pane management."""

from __future__ import annotations

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
    from dgov.persistence import read_events
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

        # Progress files
        progress_dir = Path(session_root) / ".dgov" / "progress"
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

        events = read_events(session_root, limit=8)

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
    table.add_column("Agent", width=8, no_wrap=True)
    table.add_column("State", width=14, no_wrap=True)
    table.add_column("Summary", ratio=4)

    sorted_panes = _sort_panes_hierarchical(panes, selected)

    for p, indent_level, is_last_child, orig_idx in sorted_panes:
        pstate = p.get("state", "active")
        activity = p.get("activity", "")
        summary = p.get("summary", str(activity)[:60]) or ""
        color = state_color(pstate)
        is_selected = orig_idx == selected
        role = p.get("role", "worker")

        if role == "lt-gov":
            prefix = "\u25c6 " if is_selected else "  \u25c7 "
            style = "bold magenta"
        elif indent_level > 0:
            prefix = "  \u2514\u2500 " if is_last_child else "  \u251c\u2500 "
            style = f"bold {color}" if is_selected else color
        else:
            prefix = "\u25b8 " if is_selected else "  "
            style = f"bold {color}" if is_selected else color

        slug_display = Text(f"{prefix}{p.get('slug', '')}")
        agent = Text(p.get("agent", "?"))
        dur = fmt_duration(int(p.get("duration_s", 0)))
        state_text = Text(f"{pstate} {dur}", style=color)
        summary_text = Text(str(summary)[:60])

        table.add_row(slug_display, agent, state_text, summary_text, style=style)

    return table


def _create_dashboard_layout() -> Layout:
    layout = Layout(name="root")
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="footer", size=3),
    )
    layout["body"].split_column(
        Layout(name="workers"),
        Layout(name="preview", size=7, visible=False),
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
        branch = state.branch
        last_refresh = state.last_refresh
        error = state.error
        selected = state.selected
        preview_lines = list(state.preview_lines)
        preview_visible = state.preview_visible

    ts = time.strftime("%H:%M:%S", time.localtime(last_refresh)) if last_refresh else "--:--:--"
    header_text = Text()
    header_text.append(
        f" DGOV v{__version__} \u2502 {branch} \u2502 {ts} \u2502 {len(panes)} workers"
    )

    if error:
        header_text.append(f"  err: {error}", style="red")

    table = _build_worker_table(panes, selected)

    footer = Text(
        " q:quit  j/k:\u2191\u2193  Enter:view  r:refresh  m:merge  x:close  p:preview",
        style="dim",
    )
    worker_panel = Panel(table, title="Workers", border_style="blue")

    # Determine selected slug for preview title
    selected_slug = ""
    if panes and 0 <= selected < len(panes):
        selected_slug = panes[selected].get("slug", "")

    show_preview = preview_visible and bool(preview_lines) and bool(selected_slug)

    preview_text = Text("\n".join(preview_lines)) if show_preview else Text("")
    preview_title = f"Output: {selected_slug}" if selected_slug else "Output"

    if layout is None:
        layout = _create_dashboard_layout()

    layout["header"].update(Panel(header_text, height=3))
    layout["footer"].update(Panel(footer, height=3))
    layout["body"]["workers"].update(worker_panel)
    layout["body"]["preview"].update(
        Panel(
            preview_text,
            title=preview_title,
            border_style="cyan",
            height=7,
        )
    )
    layout["body"]["preview"].visible = show_preview
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


def _acquire_dashboard_lock(session_root: str) -> Path | None:
    """Write a PID file; kill any stale dashboard process first.

    Returns the pidfile path on success, None if another live dashboard is running.
    """
    pidfile = Path(session_root) / ".dgov" / "dashboard.pid"
    pidfile.parent.mkdir(parents=True, exist_ok=True)

    if pidfile.is_file():
        try:
            old_pid = int(pidfile.read_text().strip())
            # Check if the old process is still alive
            os.kill(old_pid, 0)
            # Still alive — kill it so we can take over
            logger.info("Killing stale dashboard pid=%d", old_pid)
            os.kill(old_pid, 15)  # SIGTERM
            time.sleep(0.5)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass  # stale pidfile, process already dead

    pidfile.write_text(str(os.getpid()))
    return pidfile


def run_dashboard_v2(
    project_root: str,
    session_root: str | None = None,
    refresh_interval: float = 1.0,
) -> None:
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)
    pidfile = _acquire_dashboard_lock(session_root)
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
                pidfile.unlink(missing_ok=True)
            except OSError:
                pass
