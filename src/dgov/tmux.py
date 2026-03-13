# tmux pane management utilities

"""Thin wrappers around tmux commands."""

from __future__ import annotations

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


def has_session() -> bool:
    """Return True if a tmux server is running and has at least one session."""
    result = subprocess.run(
        ["tmux", "list-sessions"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def ensure_session(name: str = "dgov") -> None:
    """Start a tmux session if no server is running, then attach to it.

    If already inside tmux ($TMUX is set), this is a no-op.
    If no server is running, starts a detached session named *name*.
    """
    import os

    if os.environ.get("TMUX"):
        return
    if has_session():
        return
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name],
        capture_output=True,
        text=True,
        check=True,
    )


def split_pane(*, cwd: str | None = None, target: str | None = None) -> str:
    """Split the current tmux window and return the new pane ID."""
    args = ["split-window", "-h", "-P", "-F", "#{pane_id}"]
    if target:
        args.extend(["-t", target])
    if cwd:
        args.extend(["-c", cwd])
    return _run(args)


def send_command(pane_id: str, command: str) -> None:
    """Send a shell command to a pane and press Enter."""
    _run(["send-keys", "-t", pane_id, command, "Enter"])


def set_title(pane_id: str, title: str) -> None:
    """Set the pane title (shown in pane border)."""
    _run(["select-pane", "-t", pane_id, "-T", title])


def update_pane_status(pane_id: str, agent: str, slug: str, status: str) -> None:
    """Update pane title to reflect current status."""
    icon = {"active": "\u23f3", "done": "\u2713", "failed": "\u2717", "timed_out": "\u23f0"}.get(
        status, "?"
    )
    set_title(pane_id, f"[{agent}] {slug} {icon}")


def set_pane_option(pane_id: str, option: str, value: str) -> None:
    """Set a pane-level tmux option."""
    _run(["set-option", "-p", "-t", pane_id, option, value])


def capture_pane(pane_id: str, lines: int = 30) -> str:
    """Capture the last N lines of visible pane content."""
    return _run(["capture-pane", "-t", pane_id, "-p", "-S", f"-{lines}"])


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


def list_panes() -> list[dict[str, str]]:
    """List all panes in the current window."""
    output = _run(["list-panes", "-F", "#{pane_id}|#{pane_title}|#{pane_width}|#{pane_height}"])
    panes = []
    for line in output.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|")
        if len(parts) >= 4:
            panes.append(
                {
                    "pane_id": parts[0],
                    "title": parts[1],
                    "width": parts[2],
                    "height": parts[3],
                }
            )
    return panes


def setup_pane_borders(session_name: str | None = None) -> None:
    """Set pane border styling to match IDE theme (idempotent)."""
    scope = ["-t", session_name] if session_name else ["-g"]
    _run(["set-option", *scope, "pane-border-status", "top"], silent=True)
    _run(
        ["set-option", *scope, "pane-active-border-style", "fg=colour39,bg=default"],
        silent=True,
    )
    _run(
        ["set-option", *scope, "pane-border-style", "fg=colour238,bg=default"],
        silent=True,
    )
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


def style_worker_pane(pane_id: str, agent: str) -> None:
    """Color-code a worker pane border by agent type."""
    colour = _AGENT_COLORS.get(agent, _DEFAULT_AGENT_COLOR)
    set_pane_option(pane_id, "pane-border-style", f"fg=colour{colour}")


def style_governor_pane(pane_id: str) -> None:
    """Style the governor pane: bright active bg, [gov] title."""
    _run(["select-pane", "-t", pane_id, "-P", "fg=default,bg=colour234"], silent=True)
    _run(["select-pane", "-t", pane_id, "-T", "[gov] main"], silent=True)


def select_pane(pane_id: str) -> None:
    """Focus the given tmux pane."""
    _run(["select-pane", "-t", pane_id])


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


def send_prompt_via_buffer(pane_id: str, prompt: str) -> None:
    """Send prompt via tmux paste buffer (for send-keys transport agents)."""
    buf_name = f"dgov-{int(time.time() * 1000)}"
    _run(["set-buffer", "-b", buf_name, "--", prompt])
    _run(["paste-buffer", "-b", buf_name, "-t", pane_id])
    _run(["send-keys", "-t", pane_id, "Enter"])
    _run(["delete-buffer", "-b", buf_name], silent=True)
