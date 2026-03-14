"""Live terminal dashboard for dgov pane management.

Curses-based TUI that auto-refreshes pane status. Zero external dependencies.
"""

from __future__ import annotations

import curses
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field

from dgov import __version__

# State → curses color pair index
STATE_COLORS: dict[str, int] = {
    "active": 1,  # yellow
    "done": 2,  # green
    "merged": 2,  # green
    "failed": 3,  # red
    "abandoned": 3,  # red
    "timed_out": 3,  # red
    "reviewed_pass": 4,  # cyan
    "reviewed_fail": 3,  # red
    "merge_conflict": 3,  # red
    "escalated": 5,  # magenta
    "superseded": 5,  # magenta
    "closed": 6,  # dim white
}


def state_color(state: str) -> int:
    """Return the curses color pair index for a pane state."""
    return STATE_COLORS.get(state, 0)


def truncate(text: str, width: int) -> str:
    """Truncate text to fit width, adding ellipsis if needed."""
    if not text:
        return ""
    if len(text) <= width:
        return text
    if width < 2:
        return text[:width]
    return text[: width - 1] + "\u2026"


def fmt_duration(seconds: int) -> str:
    """Format duration in human-readable format."""
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m}m"


def format_row(pane: dict, col_widths: dict[str, int]) -> dict[str, str]:
    """Format a pane dict into display strings for each column."""
    return {
        "slug": truncate(pane.get("slug", ""), col_widths["slug"]),
        "agent": truncate(pane.get("agent", "?"), col_widths["agent"]),
        "state": truncate(pane.get("state", "active"), col_widths["state"]),
        "alive": "\u2713" if pane.get("alive") else "\u2717",
        "done": "\u2713" if pane.get("done") else "\u2717",
        "freshness": truncate(pane.get("freshness", "?"), col_widths["freshness"]),
        "duration": fmt_duration(int(pane.get("duration_s", 0))),
        "prompt": truncate(pane.get("prompt", ""), col_widths["prompt"]),
    }


COLUMNS = [
    ("slug", 20),
    ("agent", 10),
    ("state", 12),
    ("alive", 5),
    ("done", 5),
    ("freshness", 10),
    ("duration", 10),
    ("prompt", 40),
]


def compute_col_widths(max_width: int) -> dict[str, int]:
    """Compute column widths, shrinking prompt to fit terminal width."""
    fixed_width = sum(w for _, w in COLUMNS if _ != "prompt")
    separators = len(COLUMNS) - 1  # one space between each
    prompt_width = max(10, max_width - fixed_width - separators)
    return {name: (prompt_width if name == "prompt" else w) for name, w in COLUMNS}


@dataclass
class DashboardState:
    """Shared state between the data thread and the curses UI."""

    panes: list[dict] = field(default_factory=list)
    last_refresh: float = 0.0
    project_root: str = "."
    session_root: str | None = None
    branch: str = ""
    error: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop_event: threading.Event = field(default_factory=threading.Event)
    force_refresh: threading.Event = field(default_factory=threading.Event)

    # Detail view cache
    detail_slug: str = ""
    detail_text: str = ""


def _get_branch(project_root: str) -> str:
    """Get current git branch name."""
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
    """Fetch pane data from dgov and update shared state."""
    from dgov.panes import list_worker_panes

    try:
        panes = list_worker_panes(state.project_root, session_root=state.session_root)
        branch = _get_branch(state.project_root)
        with state.lock:
            state.panes = panes
            state.branch = branch
            state.last_refresh = time.time()
            state.error = ""
    except Exception as exc:  # noqa: BLE001
        with state.lock:
            state.error = str(exc)
            state.last_refresh = time.time()


def fetch_detail(state: DashboardState, slug: str) -> None:
    """Fetch detail info for a single pane."""
    from dgov.panes import capture_worker_output, review_worker_pane

    lines: list[str] = []

    # Find the pane record
    with state.lock:
        pane = next((p for p in state.panes if p.get("slug") == slug), None)

    if not pane:
        with state.lock:
            state.detail_slug = slug
            state.detail_text = f"Pane not found: {slug}"
        return

    # Full prompt
    lines.append("== Prompt ==")
    lines.append(pane.get("prompt", "(no prompt)"))
    lines.append("")

    # Branch / worktree
    lines.append("== Info ==")
    lines.append(f"Agent:    {pane.get('agent', '?')}")
    lines.append(f"State:    {pane.get('state', '?')}")
    lines.append(f"Branch:   {pane.get('branch', '?')}")
    lines.append(f"Worktree: {pane.get('worktree_path', '?')}")
    lines.append(f"Pane ID:  {pane.get('pane_id', '?')}")
    lines.append(f"Duration: {fmt_duration(int(pane.get('duration_s', 0)))}")
    lines.append("")

    # Diff stat
    try:
        review = review_worker_pane(state.project_root, slug, session_root=state.session_root)
        if "error" not in review:
            lines.append("== Diff Stat ==")
            lines.append(review.get("diff_stat", "(no changes)"))
            lines.append(f"Commits: {review.get('commit_count', 0)}")
            lines.append("")
        else:
            lines.append(f"== Diff Stat == (error: {review['error']})")
            lines.append("")
    except Exception:  # noqa: BLE001
        lines.append("== Diff Stat == (unavailable)")
        lines.append("")

    # Capture last 20 lines
    try:
        output = capture_worker_output(
            state.project_root, slug, lines=20, session_root=state.session_root
        )
        if output:
            lines.append("== Recent Output ==")
            lines.append(output)
        else:
            lines.append("== Recent Output == (pane dead or not found)")
    except Exception:  # noqa: BLE001
        lines.append("== Recent Output == (unavailable)")

    with state.lock:
        state.detail_slug = slug
        state.detail_text = "\n".join(lines)


def data_thread(state: DashboardState, interval: float) -> None:
    """Background thread that periodically fetches pane data."""
    while not state.stop_event.is_set():
        fetch_panes(state)
        # Wait for interval or force refresh
        state.force_refresh.wait(timeout=interval)
        state.force_refresh.clear()


def _init_colors() -> None:
    """Initialize curses color pairs."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_YELLOW, -1)  # active
    curses.init_pair(2, curses.COLOR_GREEN, -1)  # done/merged
    curses.init_pair(3, curses.COLOR_RED, -1)  # failed/abandoned
    curses.init_pair(4, curses.COLOR_CYAN, -1)  # reviewed
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # escalated
    curses.init_pair(6, curses.COLOR_WHITE, -1)  # closed/dim
    curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE)  # selected row


def _draw_header(stdscr: curses.window, state: DashboardState, max_x: int) -> int:
    """Draw the header. Returns the next row to draw on."""
    row = 0
    with state.lock:
        branch = state.branch
        last_refresh = state.last_refresh
        error = state.error
        pane_count = len(state.panes)

    project = os.path.basename(os.path.abspath(state.project_root))
    ts = time.strftime("%H:%M:%S", time.localtime(last_refresh)) if last_refresh else "--:--:--"

    header = f" dgov v{__version__}  |  {project}  |  {branch}  |  {ts}  |  {pane_count} panes"
    try:
        stdscr.addnstr(row, 0, header, max_x - 1, curses.A_BOLD)
    except curses.error:
        pass
    row += 1

    if error:
        try:
            stdscr.addnstr(row, 0, f" Error: {error}", max_x - 1, curses.color_pair(3))
        except curses.error:
            pass
        row += 1

    # Separator
    try:
        stdscr.addnstr(row, 0, "\u2500" * (max_x - 1), max_x - 1)
    except curses.error:
        pass
    row += 1

    return row


def _draw_table_header(
    stdscr: curses.window, row: int, col_widths: dict[str, int], max_x: int
) -> int:
    """Draw the table column headers. Returns next row."""
    labels = {
        "slug": "SLUG",
        "agent": "AGENT",
        "state": "STATE",
        "alive": "LIVE",
        "done": "DONE",
        "freshness": "FRESH",
        "duration": "DURATION",
        "prompt": "PROMPT",
    }
    x = 1
    for name, _ in COLUMNS:
        w = col_widths[name]
        label = labels.get(name, name.upper())
        try:
            stdscr.addnstr(row, x, label.ljust(w), w, curses.A_BOLD | curses.A_UNDERLINE)
        except curses.error:
            pass
        x += w + 1
    return row + 1


def _draw_pane_row(
    stdscr: curses.window,
    row: int,
    pane: dict,
    col_widths: dict[str, int],
    selected: bool,
    max_x: int,
) -> None:
    """Draw a single pane row."""
    formatted = format_row(pane, col_widths)
    pane_state = pane.get("state", "active")
    color = curses.color_pair(state_color(pane_state))

    if selected:
        attr = curses.color_pair(7) | curses.A_BOLD
    else:
        attr = color

    # Clear the row
    try:
        stdscr.addnstr(row, 0, " " * (max_x - 1), max_x - 1, attr if selected else 0)
    except curses.error:
        pass

    x = 1
    for name, _ in COLUMNS:
        w = col_widths[name]
        val = formatted.get(name, "")
        try:
            stdscr.addnstr(row, x, val.ljust(w), w, attr)
        except curses.error:
            pass
        x += w + 1


def _draw_footer(stdscr: curses.window, row: int, max_x: int, mode: str = "list") -> None:
    """Draw the footer with keybindings."""
    if mode == "list":
        keys = (
            "q:quit  r:refresh  j/k:\u2191\u2193  Enter:detail"
            "  d:diff  c:capture  m:merge  x:close"
        )
    else:
        keys = "q/Esc:back  j/k:\u2191\u2193 scroll"
    try:
        stdscr.addnstr(row, 0, "\u2500" * (max_x - 1), max_x - 1)
    except curses.error:
        pass
    try:
        stdscr.addnstr(row + 1, 1, keys, max_x - 2, curses.A_DIM)
    except curses.error:
        pass


def _draw_detail(
    stdscr: curses.window,
    state: DashboardState,
    scroll_offset: int,
    max_y: int,
    max_x: int,
) -> None:
    """Draw the detail view for a selected pane."""
    with state.lock:
        text = state.detail_text
        slug = state.detail_slug

    # Header
    row = 0
    title = f" Detail: {slug} "
    try:
        stdscr.addnstr(row, 0, title, max_x - 1, curses.A_BOLD)
    except curses.error:
        pass
    row += 1
    try:
        stdscr.addnstr(row, 0, "\u2500" * (max_x - 1), max_x - 1)
    except curses.error:
        pass
    row += 1

    # Content
    lines = text.split("\n")
    visible_rows = max_y - 4  # header(2) + footer(2)
    for i in range(scroll_offset, min(scroll_offset + visible_rows, len(lines))):
        line = lines[i]
        try:
            attr = curses.A_BOLD if line.startswith("==") else 0
            stdscr.addnstr(row, 1, truncate(line, max_x - 2), max_x - 2, attr)
        except curses.error:
            pass
        row += 1

    _draw_footer(stdscr, max_y - 2, max_x, mode="detail")


def _draw_confirmation(stdscr: curses.window, message: str, max_y: int, max_x: int) -> None:
    """Draw a confirmation prompt at the bottom of the screen."""
    row = max_y - 3
    try:
        stdscr.addnstr(row, 0, " " * (max_x - 1), max_x - 1, curses.color_pair(1))
        stdscr.addnstr(row, 1, message, max_x - 2, curses.color_pair(1) | curses.A_BOLD)
    except curses.error:
        pass


def run_dashboard(
    project_root: str = ".",
    session_root: str | None = None,
    refresh_interval: float = 2.0,
) -> None:
    """Launch the curses dashboard. Blocks until user quits."""
    curses.wrapper(
        _curses_main,
        project_root=project_root,
        session_root=session_root,
        refresh_interval=refresh_interval,
    )


def _curses_main(
    stdscr: curses.window,
    *,
    project_root: str,
    session_root: str | None,
    refresh_interval: float,
) -> None:
    """Main curses loop."""
    _init_colors()
    curses.curs_set(0)  # hide cursor
    stdscr.timeout(200)  # 200ms for key polling

    state = DashboardState(
        project_root=os.path.abspath(project_root),
        session_root=os.path.abspath(session_root or project_root),
    )

    # Start background data fetcher
    thread = threading.Thread(
        target=data_thread,
        args=(state, refresh_interval),
        daemon=True,
    )
    thread.start()

    selected = 0
    mode = "list"  # "list" or "detail"
    detail_scroll = 0
    confirm_action: str | None = None  # "merge" or "close"

    try:
        while True:
            stdscr.erase()
            max_y, max_x = stdscr.getmaxyx()

            if max_y < 5 or max_x < 40:
                try:
                    stdscr.addnstr(0, 0, "Terminal too small", max_x - 1)
                except curses.error:
                    pass
                stdscr.refresh()
                key = stdscr.getch()
                if key == ord("q"):
                    break
                continue

            if mode == "detail":
                _draw_detail(stdscr, state, detail_scroll, max_y, max_x)
            else:
                # List view
                row = _draw_header(stdscr, state, max_x)
                col_widths = compute_col_widths(max_x)
                row = _draw_table_header(stdscr, row, col_widths, max_x)

                with state.lock:
                    panes = list(state.panes)

                if not panes:
                    try:
                        stdscr.addnstr(row, 1, "No panes.", max_x - 2, curses.A_DIM)
                    except curses.error:
                        pass
                    row += 1
                else:
                    selected = max(0, min(selected, len(panes) - 1))
                    visible_rows = max_y - row - 2  # leave room for footer
                    # Scroll window
                    if selected >= visible_rows:
                        start = selected - visible_rows + 1
                    else:
                        start = 0
                    end = min(start + visible_rows, len(panes))

                    for i in range(start, end):
                        _draw_pane_row(
                            stdscr,
                            row,
                            panes[i],
                            col_widths,
                            selected=i == selected,
                            max_x=max_x,
                        )
                        row += 1

                _draw_footer(stdscr, max_y - 2, max_x, mode="list")

                if confirm_action:
                    slug = panes[selected]["slug"] if panes else "?"
                    msg = f" {confirm_action.upper()} '{slug}'? y/n "
                    _draw_confirmation(stdscr, msg, max_y, max_x)

            stdscr.refresh()

            key = stdscr.getch()
            if key == -1:
                continue

            # Confirmation mode
            if confirm_action:
                if key == ord("y"):
                    with state.lock:
                        panes_snap = list(state.panes)
                    if panes_snap and 0 <= selected < len(panes_snap):
                        slug = panes_snap[selected]["slug"]
                        _execute_action(state, confirm_action, slug)
                        state.force_refresh.set()
                    confirm_action = None
                else:
                    confirm_action = None
                continue

            # Resize
            if key == curses.KEY_RESIZE:
                continue

            if mode == "detail":
                if key in (ord("q"), 27):  # q or Esc
                    mode = "list"
                    detail_scroll = 0
                elif key in (ord("j"), curses.KEY_DOWN):
                    detail_scroll += 1
                elif key in (ord("k"), curses.KEY_UP):
                    detail_scroll = max(0, detail_scroll - 1)
                continue

            # List mode keys
            if key == ord("q"):
                break
            elif key == ord("r"):
                state.force_refresh.set()
            elif key in (ord("j"), curses.KEY_DOWN):
                selected += 1
            elif key in (ord("k"), curses.KEY_UP):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_ENTER, 10, 13):
                with state.lock:
                    panes_snap = list(state.panes)
                if panes_snap and 0 <= selected < len(panes_snap):
                    slug = panes_snap[selected]["slug"]
                    fetch_detail(state, slug)
                    mode = "detail"
                    detail_scroll = 0
            elif key == ord("d"):
                with state.lock:
                    panes_snap = list(state.panes)
                if panes_snap and 0 <= selected < len(panes_snap):
                    slug = panes_snap[selected]["slug"]
                    _show_diff(state, slug)
                    mode = "detail"
                    detail_scroll = 0
            elif key == ord("c"):
                with state.lock:
                    panes_snap = list(state.panes)
                if panes_snap and 0 <= selected < len(panes_snap):
                    slug = panes_snap[selected]["slug"]
                    _show_capture(state, slug)
                    mode = "detail"
                    detail_scroll = 0
            elif key == ord("m"):
                with state.lock:
                    panes_snap = list(state.panes)
                if panes_snap:
                    confirm_action = "merge"
            elif key == ord("x"):
                with state.lock:
                    panes_snap = list(state.panes)
                if panes_snap:
                    confirm_action = "close"
    finally:
        state.stop_event.set()


def _show_diff(state: DashboardState, slug: str) -> None:
    """Fetch diff stat and show in detail view."""
    from dgov.panes import diff_worker_pane

    try:
        result = diff_worker_pane(
            state.project_root,
            slug,
            session_root=state.session_root,
            stat=True,
        )
        if "error" in result:
            text = f"Error: {result['error']}"
        else:
            text = result.get("diff", "(no diff)")
    except Exception as exc:  # noqa: BLE001
        text = f"Error fetching diff: {exc}"

    with state.lock:
        state.detail_slug = slug
        state.detail_text = f"== Diff: {slug} ==\n\n{text}"


def _show_capture(state: DashboardState, slug: str) -> None:
    """Fetch pane capture output and show in detail view."""
    from dgov.panes import capture_worker_output

    try:
        output = capture_worker_output(
            state.project_root, slug, lines=30, session_root=state.session_root
        )
        if output:
            text = output
        else:
            text = "(pane dead or not found)"
    except Exception as exc:  # noqa: BLE001
        text = f"Error capturing output: {exc}"

    with state.lock:
        state.detail_slug = slug
        state.detail_text = f"== Capture: {slug} ==\n\n{text}"


def _execute_action(state: DashboardState, action: str, slug: str) -> None:
    """Execute a merge or close action."""
    if action == "merge":
        from dgov.merger import merge_worker_pane_with_close

        try:
            merge_worker_pane_with_close(state.project_root, slug, session_root=state.session_root)
        except Exception:  # noqa: BLE001
            pass
    elif action == "close":
        from dgov.panes import close_worker_pane

        try:
            close_worker_pane(state.project_root, slug, session_root=state.session_root)
        except Exception:  # noqa: BLE001
            pass
