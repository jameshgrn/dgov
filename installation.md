# Installation

dgov v0.5.0 requires Python 3.12+ and a running `tmux` session.

## Prerequisites

- **Python**: >= 3.12 (required for `tomllib`).
- **Git**: must be installed and on your `PATH`.
- **Tmux**: the default `TmuxBackend` requires a running tmux session.
- **uv**: recommended for installation and dependency management.

## Install from source

If you are developing dgov or want the latest version:

```bash
git clone https://github.com/jameshgrn/dgov
cd dgov
uv pip install -e .
```

## Install as a global tool

For general use, install it globally using `uv`:

```bash
uv tool install --force --python 3.12 -e /path/to/dgov
```

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
