# Installation

dgov v0.5.0 requires Python 3.12+ and a running `tmux` session.

## Prerequisites

- **Python**: >= 3.12 (required for `tomllib`).
- **Git**: must be installed and on your `PATH`.
- **Tmux**: the default `TmuxBackend` requires a running tmux session.
- **uv**: recommended for installation and dependency management.

## Install as a global tool

For general use, install dgov globally using `uv`:

```bash
uv tool install dgov
```

This installs the `dgov` CLI to your PATH. The tool will automatically detect which agent CLIs are available on your system.

## Verify installation

```bash
# Should print dgov v0.5.0
dgov version

# List available agents and their install status
dgov agents
```

## tmux configuration

dgov requires specific tmux settings for optimal display and to avoid "not a terminal" errors. Add these to your `~/.tmux.conf`:

```tmux
# Ensure 256 colors for agent styling
set -g default-terminal "tmux-256color"

# Ghostty xterm-ghostty fix
# If using Ghostty, you may need to force xterm-256color or tmux-256color
# to avoid pane creation failures.
```

## Agent CLIs

dgov is an orchestrator; it does not include the agents themselves. You must install the CLI for each agent you plan to use (e.g., `claude`, `codex`, `gemini`).

Run `dgov agents` to see which are currently detected on your `PATH`.

## Custom agents

To add custom or private agent configurations, create an `agents.toml` file in your home directory (`~/.dgov/agents.toml`) with your agent CLI paths and environment setup.
