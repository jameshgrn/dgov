"""Standalone terrain simulation pane for dgov governor workspace."""

from __future__ import annotations

import shutil
import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from dgov.terrain import ErosionModel, render_terrain

_PANEL_BORDER_WIDTH = 2
_PANEL_BORDER_HEIGHT = 2
_STARTUP_DELAY_S = 0.3


def _detect_pane_size(console: Console) -> tuple[int, int]:
    """Return pane columns/rows available inside the panel border."""
    console_size = console.size
    terminal_size = shutil.get_terminal_size(fallback=(console_size.width, console_size.height))

    width = console_size.width
    height = console_size.height
    if terminal_size.columns > 0 and terminal_size.columns != width:
        width = terminal_size.columns
    if terminal_size.lines > 0 and terminal_size.lines != height:
        height = terminal_size.lines

    return (
        max(width - _PANEL_BORDER_WIDTH, 1),
        max(height - _PANEL_BORDER_HEIGHT, 1),
    )


def _clamp_rendered(rendered: Text, width: int, height: int) -> Text:
    """Crop rendered terrain to the visible panel area and disable wrapping."""
    lines = rendered.split(allow_blank=True)
    visible_lines = min(len(lines), height)

    clamped = Text()
    clamped.no_wrap = True
    clamped.overflow = "crop"
    for index, line in enumerate(lines[:visible_lines]):
        cropped = line.copy()
        cropped.no_wrap = True
        cropped.overflow = "crop"
        cropped.truncate(width, overflow="crop")
        clamped.append_text(cropped)
        if index < visible_lines - 1:
            clamped.append("\n")

    return clamped


def run_terrain(refresh: float = 0.5) -> None:
    """Run the SPIM erosion model in a Rich Live display, forever."""
    console = Console(force_terminal=True)

    model: ErosionModel | None = None
    last_w = 0
    last_h = 0
    tick = 0

    def _make_model(w: int, h: int) -> ErosionModel:
        m = ErosionModel(width=max(w, 1), height=max(h, 2))
        for _ in range(5):
            m.step()
        return m

    def _render() -> Panel:
        nonlocal model, last_w, last_h, tick
        w, panel_rows = _detect_pane_size(console)
        h = panel_rows * 2  # half-block doubles rows

        if model is None or w != last_w or h != last_h:
            model = _make_model(w, h)
            last_w, last_h = w, h

        try:
            model.step()
            rendered = render_terrain(model)
            rendered = _clamp_rendered(rendered, width=w, height=panel_rows)
        except Exception:
            rendered = Text("(terrain error)")
            rendered.no_wrap = True
            rendered.overflow = "crop"
        tick += 1
        return Panel(rendered, title=f"Terrain  t={tick}", border_style="green")

    time.sleep(_STARTUP_DELAY_S)

    with Live(
        console=console,
        get_renderable=_render,
        refresh_per_second=2,
        transient=False,
        screen=True,
    ) as live:  # noqa: F841
        try:
            while True:
                time.sleep(refresh)
        except KeyboardInterrupt:
            pass
