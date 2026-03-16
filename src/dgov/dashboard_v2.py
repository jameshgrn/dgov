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

try:
    from dgov.terrain import ErosionModel, render_terrain

    _HAS_TERRAIN = True
except ImportError:
    _HAS_TERRAIN = False

logger = logging.getLogger(__name__)


def phase_dots(state: str, activity: str) -> str:
    if state == "active" and "working" in activity:
        return "\u2b24\u2b24\u2b24\u25cb\u25cb"
    if state == "active":
        return "\u2b24\u25cb\u25cb\u25cb\u25cb"
    if state in ("done", "merged"):
        return "\u2b24\u2b24\u2b24\u2b24\u2b24"
    if state in ("failed", "abandoned", "timed_out"):
        return "\u2717\u2717\u2717\u2717\u2717"
    if state == "escalated":
        return "\u2b24\u2b24\u25cb\u25cb\u25cb"
    return "\u25cb\u25cb\u25cb\u25cb\u25cb"


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
    terrain_text: Text | None = None
    terrain_model: object | None = None  # ErosionModel instance
    terrain_tick: int = 0
    post_exit_attach: str = ""


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
        branch = _get_branch(state.project_root)
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

        # Log tail fallback
        for p in panes:
            if not p.get("activity"):
                slug = p.get("slug", "")
                log_tail = tail_worker_log(session_root, slug, lines=2)
                if log_tail:
                    lines = [ln.strip() for ln in log_tail.splitlines() if ln.strip()]
                    p["activity"] = lines[-1] if lines else ""

        events = read_events(session_root)[-8:]

        with state.lock:
            state.panes = panes
            state.events = events
            state.branch = branch
            state.last_refresh = time.time()
            state.error = ""

        # Step terrain model every 3rd refresh
        with state.lock:
            model = state.terrain_model
            tick = state.terrain_tick
        if model is not None and tick % 3 == 0:
            try:
                model.step()
                rendered = render_terrain(model)
                with state.lock:
                    state.terrain_text = rendered
            except Exception:
                logger.debug("Terrain step failed", exc_info=True)
        with state.lock:
            state.terrain_tick += 1
    except Exception as exc:
        with state.lock:
            state.error = str(exc)
            state.last_refresh = time.time()


def data_thread(state: DashboardState, interval: float) -> None:
    while not state.stop_event.is_set():
        fetch_panes(state)
        state.force_refresh.wait(timeout=interval)
        state.force_refresh.clear()


def _build_worker_table(panes: list[dict], selected: int) -> Table:
    table = Table(expand=True, box=None, padding=(0, 1), show_header=True)
    table.add_column("Phase", width=6, no_wrap=True)
    table.add_column("Slug", ratio=2, no_wrap=True)
    table.add_column("Agent", width=8, no_wrap=True)
    table.add_column("State", width=12, no_wrap=True)
    table.add_column("Activity", ratio=3)
    table.add_column("Duration", width=8, justify="right", no_wrap=True)

    for i, p in enumerate(panes):
        pstate = p.get("state", "active")
        activity = p.get("activity", "")
        color = state_color(pstate)
        style = f"bold {color}" if i == selected else color

        dots = Text(phase_dots(pstate, activity))
        slug = Text(p.get("slug", ""))
        agent = Text(p.get("agent", "?"))
        state_text = Text(pstate, style=color)
        activity_text = Text(str(activity)[:60] if activity else "")
        duration = Text(fmt_duration(int(p.get("duration_s", 0))))

        prefix = "\u25b8 " if i == selected else "  "
        slug_display = Text(f"{prefix}{slug.plain}")

        table.add_row(dots, slug_display, agent, state_text, activity_text, duration, style=style)

    return table


def _build_event_feed(events: list[dict]) -> Text:
    text = Text()
    for ev in events:
        ts = ev.get("timestamp", 0)
        if ts:
            t = time.strftime("%H:%M", time.localtime(ts))
        else:
            t = "--:--"
        event_type = ev.get("event", "?")
        pane = ev.get("pane", "")
        text.append(f"  {t} ", style="dim")
        text.append(f"{event_type}", style="bold")
        if pane:
            text.append(f" {pane}", style="dim")
        text.append("\n")
    return text


def _build_layout(state: DashboardState) -> Layout:
    with state.lock:
        panes = list(state.panes)
        events = list(state.events)
        branch = state.branch
        last_refresh = state.last_refresh
        error = state.error
        selected = state.selected
        terrain = state.terrain_text

    ts = time.strftime("%H:%M:%S", time.localtime(last_refresh)) if last_refresh else "--:--:--"
    header_text = Text()
    header_text.append(" DGOV ", style="bold yellow")
    header_text.append(f" v{__version__} \u2502 {branch} \u2502 {ts} \u2502 {len(panes)} workers")

    if error:
        header_text.append(f"  err: {error}", style="red")

    # Worker table
    table = _build_worker_table(panes, selected)

    # Event feed
    event_text = Text("\n Events\n", style="bold dim")
    event_text.append_text(_build_event_feed(events))

    # Footer
    footer = Text(
        " q:quit  j/k:\u2191\u2193  Enter:detail  r:refresh  m:merge  x:close  a:attach",
        style="dim",
    )

    # Build layout
    layout = Layout()
    layout.split_column(
        Layout(Panel(header_text, height=3), name="header", size=3),
        Layout(name="body"),
        Layout(Panel(footer, height=3), name="footer", size=3),
    )

    body = layout["body"]
    # We need to use a console to render the table to text, or use Panel with table
    worker_panel = Panel(
        Layout().split_column(
            Layout(table, name="table", ratio=3),
            Layout(
                Panel(event_text, title="Events", border_style="dim"),
                name="events",
                ratio=1,
            ),
        ),
        title="Workers",
        border_style="blue",
    )

    # Hide terrain on narrow terminals
    try:
        term_width = os.get_terminal_size().columns
    except OSError:
        term_width = 80
    if terrain is not None and term_width >= 100:
        body.split_row(
            Layout(worker_panel, name="workers", ratio=3),
            Layout(
                Panel(terrain, title="Terrain", border_style="green"),
                name="terrain",
                ratio=1,
            ),
        )
    else:
        body.split_row(
            Layout(worker_panel, name="workers"),
        )

    return layout


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


def run_dashboard_v2(
    project_root: str,
    session_root: str | None = None,
    refresh_interval: float = 1.0,
) -> None:
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)
    console = Console()
    is_tty = sys.stdin.isatty()

    state = DashboardState(
        project_root=project_root,
        session_root=session_root,
    )

    if _HAS_TERRAIN:
        try:
            state.terrain_model = ErosionModel(width=25, height=30)
        except Exception:
            logger.debug("Failed to initialize terrain model", exc_info=True)

    thread = threading.Thread(target=data_thread, args=(state, refresh_interval), daemon=True)
    thread.start()

    old_settings = None
    if is_tty:
        try:
            old_settings = termios.tcgetattr(sys.stdin)
            tty.setraw(sys.stdin.fileno())
        except termios.error:
            old_settings = None
            is_tty = False

    try:
        with Live(
            _build_layout(state),
            console=console,
            refresh_per_second=4,
            auto_refresh=False,
        ) as live:
            while not state.stop_event.is_set():
                live.update(_build_layout(state))
                live.refresh()

                if not is_tty:
                    time.sleep(0.25)
                    continue

                # Key handling with select
                rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not rlist:
                    continue

                ch = sys.stdin.read(1)
                if ch == "q":
                    break
                elif ch == "j":
                    with state.lock:
                        state.selected = min(state.selected + 1, max(0, len(state.panes) - 1))
                elif ch == "k":
                    with state.lock:
                        state.selected = max(0, state.selected - 1)
                elif ch == "r":
                    state.force_refresh.set()
                elif ch == "\r" or ch == "\n":
                    # Detail view — show review for selected pane
                    with state.lock:
                        panes = list(state.panes)
                        sel = state.selected
                    if panes and 0 <= sel < len(panes):
                        slug = panes[sel]["slug"]
                        # Temporarily restore terminal for detail output
                        if old_settings:
                            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                        live.stop()
                        try:
                            from dgov.inspection import review_worker_pane

                            review = review_worker_pane(
                                project_root, slug, session_root=session_root
                            )
                            console.print_json(data=review)
                            console.print("\nPress any key to return...", style="dim")
                            if is_tty:
                                tty.setraw(sys.stdin.fileno())
                                sys.stdin.read(1)
                                if old_settings:
                                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                                    tty.setraw(sys.stdin.fileno())
                        except Exception as exc:
                            console.print(f"Error: {exc}", style="red")
                        live.start()
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
                            tty.setraw(sys.stdin.fileno())
                            confirm = sys.stdin.read(1)
                            if old_settings:
                                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                                tty.setraw(sys.stdin.fileno())
                        else:
                            confirm = "n"
                        console.print()
                        if confirm == "y":
                            _execute_action(state, "merge", slug)
                            state.force_refresh.set()
                        live.start()
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
                            tty.setraw(sys.stdin.fileno())
                            confirm = sys.stdin.read(1)
                            if old_settings:
                                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
                                tty.setraw(sys.stdin.fileno())
                        else:
                            confirm = "n"
                        console.print()
                        if confirm == "y":
                            _execute_action(state, "close", slug)
                            state.force_refresh.set()
                        live.start()
                elif ch == "a":
                    with state.lock:
                        panes = list(state.panes)
                        sel = state.selected
                    if panes and 0 <= sel < len(panes):
                        pane_id = panes[sel].get("pane_id", "")
                        if pane_id:
                            state.post_exit_attach = pane_id
                            break
    finally:
        state.stop_event.set()
        if old_settings:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass

    if state.post_exit_attach:
        subprocess.run(["tmux", "select-window", "-t", state.post_exit_attach], check=False)
