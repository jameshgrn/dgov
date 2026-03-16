"""Live terminal dashboard for dgov pane management.

Curses-based TUI that auto-refreshes pane status. Zero external dependencies.
"""

from __future__ import annotations

import curses
import json as _json
import logging
import os
import re
import subprocess
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from dgov import __version__

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\].*?\x07|\x1b\[.*?m")

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_PREVIEW_LINES = 2


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


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


def format_row(pane: dict, col_widths: dict[str, int], frame: int = 0) -> dict[str, str]:
    """Format a pane dict into display strings for each column."""
    pane_state = pane.get("state", "active")

    # Activity fallback based on state
    activity = pane.get("activity", "")
    if not activity or activity == "idle":
        if pane_state == "active":
            spinner = _SPINNER[frame % len(_SPINNER)]
            activity = f"{spinner} working\u2026"
        elif pane_state in ("done", "reviewed_pass"):
            activity = "\u2713 complete"
        elif pane_state in (
            "failed",
            "abandoned",
            "timed_out",
            "reviewed_fail",
            "merge_conflict",
        ):
            activity = "\u2717 failed"
        elif pane_state == "merged":
            activity = "\u2713 merged"
        elif pane_state == "escalated":
            activity = "\u2191 escalated"
        elif pane_state in ("closed", "superseded"):
            activity = "\u2014 closed"

    # Phase dots indicator
    pane_activity = pane.get("activity", "")
    if pane_state == "active" and "working" in str(pane_activity):
        dots = "\u2b24\u2b24\u2b24\u25cb\u25cb"
    elif pane_state == "active":
        dots = "\u2022\u25e6\u25e6\u25e6\u25e6"
    elif pane_state in ("done", "merged"):
        dots = "\u2b24\u2b24\u2b24\u2b24\u2b24"
    elif pane_state in ("failed", "abandoned", "timed_out"):
        dots = "\u2717\u2717\u2717\u2717\u2717"
    elif pane_state == "escalated":
        dots = "\u2b24\u2b24\u25cb\u25cb\u25cb"
    else:
        dots = "\u25cb\u25cb\u25cb\u25cb\u25cb"

    return {
        "slug": truncate(pane.get("slug", ""), col_widths["slug"]),
        "agent": truncate(pane.get("agent", "?"), col_widths["agent"]),
        "state": truncate(f"{dots} {pane_state}", col_widths["state"] - 2),
        "activity": truncate(activity, col_widths["activity"]),
        "duration": fmt_duration(int(pane.get("duration_s", 0))),
        "prompt": truncate(pane.get("prompt", ""), col_widths["prompt"]),
    }


COLUMNS = [
    ("slug", 20),
    ("agent", 10),
    ("state", 16),
    ("activity", 25),
    ("duration", 10),
    ("prompt", 30),
]


def compute_col_widths(max_width: int) -> dict[str, int]:
    """Compute column widths, shrinking prompt to fit terminal width."""
    fixed_width = sum(w for _, w in COLUMNS if _ != "prompt")
    separators = len(COLUMNS) - 1  # │ between columns
    prefix = 2  # ▸ or space prefix
    prompt_width = max(10, max_width - fixed_width - separators - prefix)
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

    # Render state
    frame: int = 0

    # Detail view cache
    detail_slug: str = ""
    detail_text: str = ""

    # Deferred action to run after curses exits
    post_exit_attach: str = ""  # pane_id to tmux select-window into


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
        # Check for progress files first (faster than log tail)
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
        # Fallback: tail the log file for panes without progress activity
        for p in panes:
            if not p.get("activity"):
                slug = p.get("slug", "")
                log_tail = tail_worker_log(session_root, slug, lines=2)
                if log_tail:
                    lines = [ln.strip() for ln in log_tail.splitlines() if ln.strip()]
                    p["activity"] = lines[-1] if lines else ""
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
    from dgov.inspection import review_worker_pane
    from dgov.status import tail_worker_log

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
            lines.append(f"Commits: {review.get('commit_count', 0)}")
            lines.append(review.get("stat", "(no changes)"))
            lines.append("")
        else:
            lines.append(f"== Diff Stat == (error: {review['error']})")
            lines.append("")
    except Exception:  # noqa: BLE001
        logger.debug("Failed to fetch diff stat for %s", slug, exc_info=True)
        lines.append("== Diff Stat == (unavailable)")
        lines.append("")

    # File-level diff stat
    try:
        from dgov.inspection import diff_worker_pane

        stat_result = diff_worker_pane(
            state.project_root, slug, session_root=state.session_root, stat=True
        )
        if "error" not in stat_result:
            stat_text = stat_result.get("diff", "")
            if stat_text.strip():
                lines.append("== Files Changed ==")
                for stat_line in stat_text.splitlines():
                    lines.append(stat_line)
                lines.append("")
    except Exception:  # noqa: BLE001
        logger.debug("Failed to fetch file stat for %s", slug, exc_info=True)

    # Tail log file for recent output (works even for dead panes)
    try:
        session_root = state.session_root or state.project_root
        output = tail_worker_log(session_root, slug, lines=20)
        if output:
            lines.append("== Recent Output ==")
            lines.append(output)
        else:
            lines.append("== Recent Output == (no log file)")
    except Exception:  # noqa: BLE001
        logger.debug("Failed to read log for %s", slug, exc_info=True)
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
    curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_WHITE)  # reserved


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
    spinner = _SPINNER[state.frame % len(_SPINNER)]

    title = " DGOV "
    status = (
        f" {spinner} v{__version__} \u2502 {project}"
        f" \u2502 {branch} \u2502 {ts} \u2502 {pane_count} panes"
    )
    try:
        stdscr.addnstr(row, 0, title, max_x - 1, curses.color_pair(1) | curses.A_BOLD)
        stdscr.addnstr(row, len(title), status, max_x - len(title) - 1)
    except curses.error:
        pass
    row += 1

    # Thin horizontal rule under header
    try:
        stdscr.addnstr(row, 0, "\u2500" * (max_x - 1), max_x - 1, curses.A_DIM)
    except curses.error:
        pass
    row += 1

    if error:
        try:
            stdscr.addnstr(row, 0, f" err: {error}", max_x - 1, curses.color_pair(3))
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
        "activity": "ACTIVITY",
        "duration": "DURATION",
        "prompt": "PROMPT",
    }
    x = 2  # after prefix margin
    for i, (name, _) in enumerate(COLUMNS):
        w = col_widths[name]
        label = labels.get(name, name.upper())
        try:
            stdscr.addnstr(row, x, label.ljust(w), w, curses.A_DIM)
        except curses.error:
            pass
        x += w
        if i < len(COLUMNS) - 1:
            try:
                stdscr.addnstr(row, x, "\u2502", 1, curses.A_DIM)
            except curses.error:
                pass
            x += 1
    return row + 1


def _draw_pane_row(
    stdscr: curses.window,
    row: int,
    pane: dict,
    col_widths: dict[str, int],
    selected: bool,
    max_x: int,
    frame: int = 0,
) -> None:
    """Draw a single pane row."""
    formatted = format_row(pane, col_widths, frame=frame)
    pane_state = pane.get("state", "active")
    color = curses.color_pair(state_color(pane_state))
    sel_attr = curses.A_BOLD if selected else 0

    # Selection prefix
    prefix = "\u25b8 " if selected else "  "
    try:
        stdscr.addnstr(row, 0, prefix, 2, color if selected else 0)
    except curses.error:
        pass

    x = 2
    for i, (name, _) in enumerate(COLUMNS):
        w = col_widths[name]
        val = formatted.get(name, "")
        try:
            if name == "state":
                # ● dot in state color, text in default
                stdscr.addnstr(row, x, "\u25cf ", 2, color)
                stdscr.addnstr(row, x + 2, val.ljust(w - 2), w - 2, sel_attr)
            else:
                stdscr.addnstr(row, x, val.ljust(w), w, sel_attr)
        except curses.error:
            pass
        x += w
        if i < len(COLUMNS) - 1:
            try:
                stdscr.addnstr(row, x, "\u2502", 1, curses.A_DIM)
            except curses.error:
                pass
            x += 1


def _draw_prompt_preview(
    stdscr: curses.window,
    row: int,
    pane: dict | None,
    max_x: int,
    max_y: int = 0,
    log_tail: str = "",
) -> int:
    """Draw the full prompt preview below the table. Returns next row."""
    # Separator
    try:
        stdscr.addnstr(row, 0, "\u2500" * (max_x - 1), max_x - 1, curses.A_DIM)
    except curses.error:
        pass
    row += 1

    if not pane:
        return row

    prompt = pane.get("prompt", "")
    if not prompt:
        return row

    wrapped = textwrap.wrap(prompt, width=max(20, max_x - 4))
    show_hint = len(wrapped) > _PREVIEW_LINES
    display_lines = wrapped[:_PREVIEW_LINES]

    for line in display_lines:
        try:
            stdscr.addnstr(row, 2, line, max_x - 3, curses.A_DIM)
        except curses.error:
            pass
        row += 1

    if show_hint:
        try:
            stdscr.addnstr(row, 2, "Enter for more\u2026", max_x - 3, curses.A_DIM)
        except curses.error:
            pass
        row += 1

    # Log tail fills remaining vertical space
    if log_tail and max_y > 0:
        footer_height = 2
        available = max_y - row - footer_height
        if available > 1:
            # Dim separator with label
            label = " output "
            sep_width = max_x - 1
            try:
                left = (sep_width - len(label)) // 2
                sep = "\u2504" * left + label + "\u2504" * (sep_width - left - len(label))
                stdscr.addnstr(row, 0, sep, sep_width, curses.A_DIM)
            except curses.error:
                pass
            row += 1
            available -= 1

            # Filter out bootstrap noise (env exports, source commands, prompts)
            _NOISE = {
                "unset",
                "export",
                "source",
                "DGOV_",
                "if DGOV",
                "set +o",
                "compinit",
                "zcompdump",
                "autoload",
                "kunset",
            }
            cleaned = []
            for ln in log_tail.splitlines():
                stripped = _strip_ansi(ln).strip()
                if not stripped:
                    continue
                if any(stripped.startswith(n) for n in _NOISE):
                    continue
                if stripped.startswith("\x1b") or "\x1b" in stripped:
                    continue
                # Skip lines that are just control chars or very short fragments
                if len(stripped) < 3:
                    continue
                cleaned.append(stripped)
            # Show the last N lines that fit
            cleaned = cleaned[-available:]
            for tl in cleaned:
                try:
                    stdscr.addnstr(row, 2, truncate(tl, max_x - 3), max_x - 3, curses.A_DIM)
                except curses.error:
                    pass
                row += 1

    return row


def _draw_footer(stdscr: curses.window, row: int, max_x: int, mode: str = "list") -> None:
    """Draw the footer with keybindings."""
    if mode == "list":
        keys = (
            "q:quit  r:refresh  j/k:\u2191\u2193  Enter:detail"
            "  d:diff  p:patch  c:capture  m:merge  x:close  a:attach  s:send"
        )
    else:
        keys = "q/Esc:back  j/k:\u2191\u2193 scroll"
    try:
        stdscr.addnstr(row, 0, "\u2500" * (max_x - 1), max_x - 1, curses.A_DIM)
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
        stdscr.addnstr(row, 0, "\u2500" * (max_x - 1), max_x - 1, curses.A_DIM)
    except curses.error:
        pass
    row += 1

    # Content
    lines = text.split("\n")
    is_patch = slug and text.lstrip().startswith("== Patch:")
    visible_rows = max_y - 4  # header(2) + footer(2)
    for i in range(scroll_offset, min(scroll_offset + visible_rows, len(lines))):
        line = lines[i]
        try:
            if line.startswith("=="):
                attr = curses.A_BOLD
            elif is_patch and line.startswith("+"):
                attr = curses.color_pair(2)  # green
            elif is_patch and line.startswith("-"):
                attr = curses.color_pair(3)  # red
            elif is_patch and line.startswith("@@"):
                attr = curses.color_pair(4)  # cyan
            else:
                attr = 0
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
    refresh_interval: float = 1.0,
) -> None:
    """Launch the curses dashboard. Blocks until user quits."""
    attach_pane: str = curses.wrapper(
        _curses_main,
        project_root=project_root,
        session_root=session_root,
        refresh_interval=refresh_interval,
    )
    if attach_pane:
        subprocess.run(["tmux", "select-window", "-t", attach_pane], check=False)


def _curses_main(
    stdscr: curses.window,
    *,
    project_root: str,
    session_root: str | None,
    refresh_interval: float,
) -> str:
    """Main curses loop. Returns pane_id to attach to, or empty string."""
    _init_colors()
    curses.curs_set(0)  # hide cursor
    stdscr.timeout(150)  # 150ms for key polling

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
                    msg = (
                        "No active panes \u2014 dispatch with:"
                        ' dgov pane create -a <agent> -p "task"'
                    )
                    cx = max(0, (max_x - len(msg)) // 2)
                    try:
                        stdscr.addnstr(row + 2, cx, msg, max_x - 1, curses.A_DIM)
                    except curses.error:
                        pass
                else:
                    selected = max(0, min(selected, len(panes) - 1))
                    footer_height = 2
                    preview_reserve = _PREVIEW_LINES + 2  # separator + lines + hint
                    visible_rows = max_y - row - footer_height - preview_reserve
                    show_preview = True
                    if visible_rows < 3:
                        # Not enough room for preview, skip it
                        show_preview = False
                        visible_rows = max_y - row - footer_height

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
                            frame=state.frame,
                        )
                        row += 1

                    # Prompt preview + log tail below table
                    if show_preview:
                        selected_pane = panes[selected] if panes else None
                        log_tail = ""
                        if selected_pane:
                            slug = selected_pane.get("slug", "")
                            if slug:
                                from dgov.status import tail_worker_log

                                sr = state.session_root or state.project_root
                                log_tail = tail_worker_log(sr, slug, lines=20) or ""
                        _draw_prompt_preview(
                            stdscr,
                            row,
                            selected_pane,
                            max_x,
                            max_y=max_y,
                            log_tail=log_tail,
                        )

                _draw_footer(stdscr, max_y - 2, max_x, mode="list")

                if confirm_action:
                    slug = panes[selected]["slug"] if panes else "?"
                    msg = f" {confirm_action.upper()} '{slug}'? y/n "
                    _draw_confirmation(stdscr, msg, max_y, max_x)

            state.frame += 1
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
            elif key == ord("p"):
                with state.lock:
                    panes_snap = list(state.panes)
                if panes_snap and 0 <= selected < len(panes_snap):
                    slug = panes_snap[selected]["slug"]
                    _show_patch(state, slug)
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
            elif key == ord("a"):
                with state.lock:
                    panes_snap = list(state.panes)
                if panes_snap and 0 <= selected < len(panes_snap):
                    pane_id = panes_snap[selected].get("pane_id", "")
                    if pane_id:
                        state.post_exit_attach = pane_id
                        break
            elif key == ord("s"):
                with state.lock:
                    panes_snap = list(state.panes)
                if panes_snap and 0 <= selected < len(panes_snap):
                    slug = panes_snap[selected]["slug"]
                    msg = _prompt_input(stdscr, f"Send to {slug}: ", max_y, max_x)
                    if msg:
                        _send_to_pane(state, slug, msg)
                        state.force_refresh.set()
    finally:
        state.stop_event.set()

    return state.post_exit_attach


def _prompt_input(stdscr: curses.window, prompt: str, max_y: int, max_x: int) -> str:
    """Show a prompt at the bottom of the screen and read a line of input.

    Returns the entered string, or empty string if cancelled (Esc).
    """
    row = max_y - 3
    try:
        stdscr.addnstr(row, 0, " " * (max_x - 1), max_x - 1, curses.color_pair(1))
        stdscr.addnstr(row, 1, prompt, max_x - 2, curses.color_pair(1) | curses.A_BOLD)
    except curses.error:
        pass
    stdscr.refresh()

    curses.echo()
    curses.curs_set(1)
    try:
        stdscr.move(row, min(1 + len(prompt), max_x - 2))
        raw = stdscr.getstr(row, min(1 + len(prompt), max_x - 2), max_x - len(prompt) - 3)
        return raw.decode("utf-8", errors="replace").strip() if raw else ""
    except (curses.error, UnicodeDecodeError):
        return ""
    finally:
        curses.noecho()
        curses.curs_set(0)


def _send_to_pane(state: DashboardState, slug: str, message: str) -> None:
    """Send a message to a worker pane via interact_with_pane."""
    from dgov.waiter import interact_with_pane

    session_root = state.session_root or state.project_root
    try:
        ok = interact_with_pane(session_root, slug, message)
        if not ok:
            with state.lock:
                state.error = f"Send failed: pane '{slug}' not found or dead"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Dashboard send failed for %s", slug)
        with state.lock:
            state.error = f"Send failed for {slug}: {exc}"


def _show_diff(state: DashboardState, slug: str) -> None:
    """Fetch diff stat and show in detail view."""
    from dgov.inspection import diff_worker_pane

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


def _show_patch(state: DashboardState, slug: str) -> None:
    """Fetch full diff and show in detail view with syntax coloring hints."""
    from dgov.inspection import diff_worker_pane

    try:
        result = diff_worker_pane(
            state.project_root,
            slug,
            session_root=state.session_root,
            stat=False,
        )
        if "error" in result:
            text = f"Error: {result['error']}"
        else:
            text = result.get("diff", "(no diff)")
    except Exception as exc:  # noqa: BLE001
        text = f"Error fetching patch: {exc}"

    with state.lock:
        state.detail_slug = slug
        state.detail_text = f"== Patch: {slug} ==\n\n{text}"


def _show_capture(state: DashboardState, slug: str) -> None:
    """Fetch pane capture output and show in detail view."""
    from dgov.status import capture_worker_output

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
        from dgov.merger import merge_worker_pane

        try:
            merge_worker_pane(state.project_root, slug, session_root=state.session_root)
        except Exception:  # noqa: BLE001
            logger.exception("Dashboard merge failed for %s", slug)
            with state.lock:
                state.detail_text = f"Merge failed for {slug} \u2014 check logs"
    elif action == "close":
        from dgov.lifecycle import close_worker_pane

        try:
            close_worker_pane(state.project_root, slug, session_root=state.session_root)
        except Exception:  # noqa: BLE001
            logger.exception("Dashboard close failed for %s", slug)
            with state.lock:
                state.detail_text = f"Close failed for {slug} \u2014 check logs"
