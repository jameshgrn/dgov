"""Standalone terrain simulation pane for dgov governor workspace."""

from __future__ import annotations

import os
import shutil
import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.segment import Segment
from rich.text import Text

from dgov.isometric import render_isometric
from dgov.terrain import (
    AgentSim,
    ErosionModel,
    EventTranslator,
    _spawn_position_from_slug,
    overlay_stamps,
    render_terrain,
)


class KittyRenderable:
    """Renderable that outputs raw Kitty graphics escape sequences."""

    def __init__(self, data: str):
        self.data = data

    def __rich_console__(self, console, options):
        # \x1b[H move to 1,1
        # \x1b[J clear from cursor to end of screen
        # Yield the raw data as a control segment
        yield Segment(f"\x1b[H\x1b[J{self.data}", control=True)


_PANEL_BORDER_WIDTH = 2
_PANEL_BORDER_HEIGHT = 2
_STARTUP_DELAY_S = 0.3
_AGENT_POLL_INTERVAL_S = 5.0


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
    # Force ISO mode if env var is set
    iso_mode = os.environ.get("DGOV_ISOMETRIC") == "1"

    # Force full terminal for Rich
    console = Console(force_terminal=True)

    model: ErosionModel | None = None
    last_w = 0
    last_h = 0
    tick = 0
    agents_cache: list[dict] = []
    agents_last_read: float = 0.0
    sim = AgentSim()
    translator = EventTranslator()

    def _make_model(w: int, h: int) -> ErosionModel:
        m = ErosionModel(width=max(w * 2, 1), height=max(h * 2, 2))
        for _ in range(5):
            m.step()
        return m

    def _render() -> Panel | KittyRenderable:
        nonlocal model, last_w, last_h, tick, agents_cache, agents_last_read
        w, panel_rows = _detect_pane_size(console)
        h = panel_rows * 2

        if model is None or w != last_w or h != last_h:
            model = _make_model(w, h)
            last_w, last_h = w, h

        # Poll pane state
        now = time.time()
        if now - agents_last_read > _AGENT_POLL_INTERVAL_S:
            pr = os.environ.get("DGOV_PROJECT_ROOT", os.getcwd())
            try:
                from dgov.status import list_worker_panes

                raw = list_worker_panes(pr, include_freshness=False, include_prompt=False)
                agents_cache = [
                    {
                        "slug": p.get("slug", ""),
                        "state": p.get("state", ""),
                        "role": p.get("role", "worker"),
                        "agent": p.get("agent", ""),
                        "parent_slug": p.get("parent_slug", ""),
                    }
                    for p in raw
                ]
            except Exception:
                agents_cache = []
            try:
                from dgov.persistence import read_events

                if model is not None:
                    for event in read_events(pr):
                        translated = translator.translate(event)
                        if translated is None:
                            continue
                        effect_type, intensity = translated
                        slug = str(event.get("pane", ""))
                        row, col = (
                            sim._pos[slug]
                            if slug in sim._pos
                            else _spawn_position_from_slug(slug, h // 2, w // 2)
                        )
                        model.terrain_event(
                            effect_type,
                            min(int(round(row)) * 2, model.height_count - 2),
                            min(int(round(col)) * 2, model.width - 2),
                            intensity,
                        )
            except Exception:
                pass
            agents_last_read = now

        try:
            model.step()
            if iso_mode:
                # Return raw KittyRenderable to bypass Panel layout
                return KittyRenderable(render_isometric(model))
            else:
                rendered = render_terrain(model, supersample=2)
                rendered = _clamp_rendered(rendered, width=w, height=panel_rows)
        except Exception as exc:
            if iso_mode:
                raise exc  # Fail loudly in ISO mode for debugging
            rendered = Text(f"(terrain error: {exc})")
            rendered.no_wrap = True

        if not iso_mode and agents_cache and model is not None:
            stamps = sim.update(agents_cache, panel_rows, w, model)
            rendered = overlay_stamps(rendered, stamps)

        tick += 1
        n = len(agents_cache)
        title = f"Terrain  t={tick}" + (f"  [{n} agents]" if n else "")
        return Panel(rendered, title=title, border_style="green")

    time.sleep(_STARTUP_DELAY_S)

    # In ISO mode, we don't want screen=True because it clears the terminal buffer
    # which can conflict with persistent graphics.
    with Live(
        console=console,
        get_renderable=_render,
        refresh_per_second=2,
        transient=False,
        screen=(not iso_mode),
    ) as live:  # noqa: F841
        try:
            while True:
                time.sleep(refresh)
        except KeyboardInterrupt:
            pass
