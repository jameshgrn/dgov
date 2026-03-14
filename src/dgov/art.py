"""ANSI art banners for dgov launch."""

from __future__ import annotations

import os

# Colors (ANSI 256-color)
# Using shades of orange/yellow/brown for the "Hermes" look
# 220: Gold/Yellow
# 214: Orange
# 208: Dark Orange/Brownish
COLOR_TOP = "\033[38;5;220m"
COLOR_MID = "\033[38;5;214m"
COLOR_BOT = "\033[38;5;208m"
RESET = "\033[0m"
BOLD = "\033[1m"

BANNER_LINES = [
    (COLOR_TOP, " ██████╗   ██████╗  ██████╗ ██╗   ██╗"),
    (COLOR_TOP, " ██╔══██╗ ██╔════╝ ██╔═══██╗██║   ██║"),
    (COLOR_MID, " ██║  ██║ ██║  ███╗██║   ██║██║   ██║"),
    (COLOR_MID, " ██║  ██║ ██║   ██║██║   ██║╚██╗ ██╔╝"),
    (COLOR_BOT, " ██████╔╝ ╚██████╔╝╚██████╔╝ ╚████╔╝ "),
    (COLOR_BOT, " ╚═════╝   ╚═════╝  ╚═════╝   ╚═══╝  "),
]


def print_banner() -> None:
    """Print the stylized dgov banner to the terminal."""
    # Check if terminal supports color (simple check)
    if os.environ.get("TERM") in ("dumb", "emacs"):
        print("DGOV — governor ready")
        return

    print()
    for color, line in BANNER_LINES:
        print(f"{BOLD}{color}{line}{RESET}")
    print()
