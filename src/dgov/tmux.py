# tmux pane management utilities

"""Thin wrappers around tmux commands."""

from __future__ import annotations

import shlex
import subprocess
import time


def _run(args: list[str], *, silent: bool = False) -> str:
    """Run a tmux command, return stdout stripped."""
    result = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and not silent:
        raise RuntimeError(f"tmux {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def split_pane(
    *,
    cwd: str | None = None,
    target: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Split the current tmux window and return the new pane ID."""
    args = ["split-window", "-h", "-P", "-F", "#{pane_id}"]
    if env:
        for key, value in sorted(env.items()):
            args.extend(["-e", f"{key}={value}"])
    if target:
        args.extend(["-t", target])
    if cwd:
        args.extend(["-c", cwd])
    return _run(args)


def create_background_pane(
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    name: str | None = None,
    agent: str | None = None,
) -> str:
    """Create a worker in a new background tmux window. Returns pane ID.

    Uses new-window -d so the window is created without switching focus.
    Each worker gets its own full-size TTY in an invisible window.
    """
    args = ["new-window", "-d", "-P", "-F", "#{pane_id}"]
    if name:
        prefix = agent if agent else "dgov"
        args.extend(["-n", f"[{prefix}] {name}"])
    if env:
        for key, value in sorted(env.items()):
            args.extend(["-e", f"{key}={value}"])
    if cwd:
        args.extend(["-c", cwd])
    return _run(args)


def send_command(pane_id: str, command: str) -> None:
    """Send a shell command to a pane and press Enter."""
    _run(["send-keys", "-t", pane_id, command, "Enter"])


def set_title(pane_id: str, title: str) -> None:
    """Set the pane title (shown in pane border)."""
    _run(["select-pane", "-t", pane_id, "-T", title])


def set_pane_option(pane_id: str, option: str, value: str) -> None:
    """Set a pane-level tmux option."""
    _run(["set-option", "-p", "-t", pane_id, option, value])


def capture_pane(pane_id: str, lines: int = 30) -> str:
    """Capture the last N lines of visible pane content."""
    return _run(["capture-pane", "-t", pane_id, "-p", "-S", f"-{lines}"])


def bulk_pane_info() -> dict[str, dict[str, str]]:
    """Fetch pane_id, title, current_command for ALL panes in one tmux call.

    Returns {pane_id: {"title": ..., "current_command": ...}}.
    """
    try:
        output = _run(
            ["list-panes", "-a", "-F", "#{pane_id}|#{pane_title}|#{pane_current_command}"],
            silent=True,
        )
    except RuntimeError:
        return {}
    result: dict[str, dict[str, str]] = {}
    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) >= 3:
            result[parts[0]] = {
                "title": parts[1],
                "current_command": parts[2],
            }
    return result


def pane_exists(pane_id: str) -> bool:
    """Check if a tmux pane exists."""
    try:
        result = _run(
            ["display-message", "-t", pane_id, "-p", "#{pane_id}"],
            silent=True,
        )
        return bool(result.strip())
    except RuntimeError:
        return False


def current_command(pane_id: str) -> str:
    """Get the current foreground command for a pane."""
    return _run(["display-message", "-t", pane_id, "-p", "#{pane_current_command}"])


def kill_pane(pane_id: str) -> None:
    """Kill a tmux pane."""
    _run(["kill-pane", "-t", pane_id], silent=True)


def setup_pane_borders(session_name: str | None = None) -> None:
    """Set pane border styling to match IDE theme (idempotent).

    Only sets border-status and a default border-format here.
    Per-pane colors are applied by ``style_worker_pane`` — setting a
    global ``pane-border-style`` would override those per-pane values.
    """
    scope = ["-t", session_name] if session_name else ["-g"]
    _run(["set-option", *scope, "pane-border-status", "top"], silent=True)
    _run(
        [
            "set-option",
            *scope,
            "pane-border-format",
            " #[bold]#P #[default]#{?pane_title,#{pane_title},#{pane_current_command}} ",
        ],
        silent=True,
    )


def style_dgov_session(session_name: str | None = None) -> None:
    """Apply full IDE styling: pane borders, window shading, status bar."""
    scope = ["-t", session_name] if session_name else ["-g"]

    setup_pane_borders(session_name)

    # Dim inactive panes, normal active — gives visual depth
    _run(["set-option", *scope, "window-style", "fg=colour247,bg=colour236"], silent=True)
    _run(["set-option", *scope, "window-active-style", "fg=default,bg=colour234"], silent=True)

    # Status bar
    _run(["set-option", *scope, "status-style", "fg=colour252,bg=colour236"], silent=True)
    _run(
        ["set-option", *scope, "status-left", " #[bold,fg=colour39]dgov#[default] │ "],
        silent=True,
    )
    _run(
        [
            "set-option",
            *scope,
            "status-right",
            " #{pane_title} │ %H:%M ",
        ],
        silent=True,
    )


_AGENT_COLORS: dict[str, int] = {
    "claude": 39,  # blue
    "pi": 34,  # green
    "codex": 214,  # yellow/orange
    "gemini": 135,  # magenta
}
_DEFAULT_AGENT_COLOR = 252  # white


def style_worker_pane(pane_id: str, agent: str, *, color: int | None = None) -> None:
    """Color-code a worker pane border by agent type.

    If *color* is provided it takes precedence over the built-in lookup.
    Sets per-pane border style, active-border style, and border format so
    the color is visible whether the pane is focused or not.
    """
    colour = color if color is not None else _AGENT_COLORS.get(agent, _DEFAULT_AGENT_COLOR)
    set_pane_option(pane_id, "pane-border-style", f"fg=colour{colour}")
    set_pane_option(pane_id, "pane-active-border-style", f"fg=colour{colour},bold")
    _run(
        [
            "set-option",
            "-p",
            "-t",
            pane_id,
            "pane-border-format",
            f" #[fg=colour{colour},bold]#P "
            f"#[default]#{{?pane_title,#{{pane_title}},#{{pane_current_command}}}} ",
        ],
        silent=True,
    )


def style_governor_pane(pane_id: str) -> None:
    """Style the governor pane: bright active bg, [gov] title."""
    _run(["select-pane", "-t", pane_id, "-P", "fg=default,bg=colour234"], silent=True)
    _run(["select-pane", "-t", pane_id, "-T", "[gov] main"], silent=True)


def select_layout(layout: str = "tiled") -> None:
    """Apply a tmux layout to the current window."""
    _run(["select-layout", layout], silent=True)


def create_utility_pane(command: str, title: str, cwd: str | None = None) -> str:
    """Split a new pane, run command, set title. Returns pane_id."""
    pane_id = split_pane(cwd=cwd)
    send_command(pane_id, command)
    set_title(pane_id, title)
    select_layout("tiled")
    return pane_id


def setup_governor_workspace(project_root: str) -> list[str]:
    """Split dashboard + lazygit into the current window as companion panes.

    Idempotent: skips panes that already exist (by title).
    Returns list of created pane_ids.
    """
    existing = _run(
        ["list-panes", "-F", "#{pane_title}"],
        silent=True,
    ).splitlines()

    panes: list[str] = []

    if "[gov] dashboard" not in existing:
        dash_id = split_pane()
        send_command(dash_id, f"dgov dashboard -r {shlex.quote(project_root)}")
        set_title(dash_id, "[gov] dashboard")
        set_pane_option(dash_id, "pane-border-style", "fg=colour39")
        set_pane_option(dash_id, "pane-active-border-style", "fg=colour39,bold")
        panes.append(dash_id)

    if "[gov] lazygit" not in existing:
        lg_id = split_pane()
        send_command(lg_id, "lazygit")
        set_title(lg_id, "[gov] lazygit")
        set_pane_option(lg_id, "pane-border-style", "fg=colour214")
        set_pane_option(lg_id, "pane-active-border-style", "fg=colour214,bold")
        panes.append(lg_id)

    if panes:
        select_layout("main-vertical")
    return panes


def send_prompt_via_buffer(pane_id: str, prompt: str) -> None:
    """Send prompt via tmux paste buffer (for send-keys transport agents)."""
    buf_name = f"dgov-{int(time.time() * 1000)}"
    _run(["set-buffer", "-b", buf_name, "--", prompt])
    _run(["paste-buffer", "-b", buf_name, "-t", pane_id])
    _run(["send-keys", "-t", pane_id, "Enter"])
    _run(["delete-buffer", "-b", buf_name], silent=True)


def start_logging(pane_id: str, log_file: str) -> None:
    """Start logging pane output to a file via pipe-pane."""
    _run(["pipe-pane", "-t", pane_id, "-o", f"cat >> {shlex.quote(log_file)}"])


def stop_logging(pane_id: str) -> None:
    """Stop logging pane output."""
    _run(["pipe-pane", "-t", pane_id])
