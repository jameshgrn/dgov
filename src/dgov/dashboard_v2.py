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

_STARTUP_TIME = time.time()
_UI_REFRESH_PER_SECOND = 4
_INPUT_POLL_INTERVAL = 0.05
_MIN_TERRAIN_WIDTH = 100
_MIN_TERRAIN_HEIGHT = 14


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

        # Log tail fallback — only if no summary AND no activity
        for p in panes:
            if not p.get("summary") and not p.get("activity"):
                slug = p.get("slug", "")
                log_tail = tail_worker_log(session_root, slug, lines=2)
                if log_tail:
                    lines = [ln.strip() for ln in log_tail.splitlines() if ln.strip()]
                    p["activity"] = lines[-1] if lines else ""

        events = read_events(session_root)[-8:]

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


def _build_layout(
    state: DashboardState,
    term_width: int | None = None,
    term_height: int | None = None,
) -> Layout:
    with state.lock:
        panes = list(state.panes)
        branch = state.branch
        last_refresh = state.last_refresh
        error = state.error
        selected = state.selected
        terrain = state.terrain_text
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
        " q:quit  j/k:\u2191\u2193  Enter:attach  r:refresh  m:merge  x:close  p:preview",
        style="dim",
    )

    layout = Layout()
    layout.split_column(
        Layout(Panel(header_text, height=3), name="header", size=3),
        Layout(name="body"),
        Layout(Panel(footer, height=3), name="footer", size=3),
    )

    body = layout["body"]
    worker_panel = Panel(table, title="Workers", border_style="blue")

    if term_width is None or term_height is None:
        try:
            size = os.get_terminal_size()
            if term_width is None:
                term_width = size.columns
            if term_height is None:
                term_height = size.lines
        except OSError:
            term_width = 80 if term_width is None else term_width
            term_height = 24 if term_height is None else term_height

    show_terrain = (
        terrain is not None
        and term_width >= _MIN_TERRAIN_WIDTH
        and term_height >= _MIN_TERRAIN_HEIGHT
    )

    # Determine selected slug for preview title
    selected_slug = ""
    if panes and 0 <= selected < len(panes):
        selected_slug = panes[selected].get("slug", "")

    show_preview = preview_visible and bool(preview_lines) and bool(selected_slug)

    if show_preview:
        preview_text = Text("\n".join(preview_lines))
        preview_panel = Panel(
            preview_text,
            title=f"Output: {selected_slug}",
            border_style="cyan",
            height=7,
        )
        main_body = Layout(name="main_body")
        if show_terrain:
            main_body.split_row(
                Layout(worker_panel, name="workers", ratio=3),
                Layout(
                    Panel(terrain, title="Terrain", border_style="green"),
                    name="terrain",
                    ratio=1,
                ),
            )
        else:
            main_body.split_row(
                Layout(worker_panel, name="workers"),
            )
        body.split_column(
            main_body,
            Layout(preview_panel, name="preview", size=7),
        )
    else:
        if show_terrain:
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
            tty.setcbreak(sys.stdin.fileno())
        except termios.error:
            old_settings = None
            is_tty = False

    def _render_dashboard() -> Layout:
        size = console.size
        return _build_layout(state, term_width=size.width, term_height=size.height)

    try:
        with Live(
            console=console,
            auto_refresh=True,
            get_renderable=_render_dashboard,
            refresh_per_second=_UI_REFRESH_PER_SECOND,
            vertical_overflow="crop",
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
                    # Attach to selected worker's tmux window
                    with state.lock:
                        panes = list(state.panes)
                        sel = state.selected
                    if panes and 0 <= sel < len(panes):
                        pane_id = panes[sel].get("pane_id", "")
                        if pane_id:
                            state.post_exit_attach = pane_id
                            break
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
                    # Alias for Enter — attach to worker window
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
