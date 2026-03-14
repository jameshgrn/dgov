"""ASCII art banner for dgov launch."""

from __future__ import annotations

import os

_DIM = "\033[2m"
_RESET = "\033[0m"

BANNER = """\
 ██████   ██████  ██████ ██   ██
 ██   ██ ██      ██   ██ ██   ██
 ██   ██ ██  ███ ██   ██ ██   ██
 ██   ██ ██   ██ ██   ██  ██ ██
 ██████   ██████  ██████   ████"""


def print_banner() -> None:
    """Print the dgov banner to the terminal."""
    if os.environ.get("TERM") in ("dumb", "emacs"):
        print("dgov — dispatch · wait · review · merge")
        return

    print()
    print(BANNER)
    print(f"{_DIM}  dispatch · wait · review · merge{_RESET}")
    print()
