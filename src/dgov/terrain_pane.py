"""Standalone terrain simulation pane for dgov governor workspace."""

from __future__ import annotations

import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from dgov.terrain import ErosionModel, render_terrain


def run_terrain(refresh: float = 0.5) -> None:
    """Run the SPIM erosion model in a Rich Live display, forever."""
    console = Console(force_terminal=True)

    model: ErosionModel | None = None
    last_w = 0
    last_h = 0
    tick = 0

    def _make_model(w: int, h: int) -> ErosionModel:
        m = ErosionModel(width=max(w, 10), height=max(h, 10))
        for _ in range(5):
            m.step()
        return m

    def _render() -> Panel:
        nonlocal model, last_w, last_h, tick
        size = console.size
        w = size.width - 2  # panel borders
        h = (size.height - 2) * 2  # half-block doubles rows

        if model is None or w != last_w or h != last_h:
            model = _make_model(w, h)
            last_w, last_h = w, h

        try:
            model.step()
            rendered = render_terrain(model)
        except Exception:
            rendered = Text("(terrain error)")
        tick += 1
        return Panel(rendered, title=f"Terrain  t={tick}", border_style="green")

    with Live(
        console=console,
        get_renderable=_render,
        refresh_per_second=2,
        transient=False,
    ) as live:  # noqa: F841
        try:
            while True:
                time.sleep(refresh)
        except KeyboardInterrupt:
            pass
