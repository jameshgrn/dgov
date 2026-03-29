"""Standalone terrain simulation pane for dgov governor workspace."""

from __future__ import annotations

import os
import shutil
import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from dgov.terrain import (
    AgentSim,
    ErosionModel,
    EventTranslator,
    _spawn_position_from_slug,
    overlay_stamps,
    render_effect_stamps,
    render_terrain,
)

_PANEL_BORDER_WIDTH = 2
_PANEL_BORDER_HEIGHT = 2
_STARTUP_DELAY_S = 0.3
_AGENT_POLL_INTERVAL_S = 5.0

# Hysteresis state to prevent HUD label flickering near thresholds
_hud_state: dict[str, object] = {"last_label": "", "last_maturity": 0.0}
_HYSTERESIS_MARGIN = 0.03


def _get_label_for_maturity(maturity: float) -> str:
    """Return the state label for a given maturity value.

    Thresholds: 0.25, 0.50, 0.75
    """
    if maturity < 0.25:
        return "youthful"
    elif maturity < 0.50:
        return "organizing"
    elif maturity < 0.75:
        return "mature"
    else:
        return "settled"


def _get_threshold_band(last_label: str, new_label: str) -> dict[str, float] | None:
    """Determine if changing from last_label to new_label requires crossing a threshold.

    Returns a dict with 'lower' and 'upper' bounds for the hysteresis band,
    or None if no threshold needs to be crossed.

    Example:
        - last_label="organizing", new_label="mature" -> crosses 0.50 threshold
          returns {"lower": 0.47, "upper": 0.53}
        - last_label="mature", new_label="organizing" -> crosses 0.50 threshold
          returns {"lower": 0.47, "upper": 0.53}
        - last_label="youthful", new_label="organizing" -> crosses 0.25 threshold
          returns {"lower": 0.22, "upper": 0.28}
        - same labels -> None
        - non-adjacent labels (e.g., youthful->mature) -> None (must pass through intermediate)
    """
    # Define threshold boundaries with hysteresis margin
    thresholds = {
        ("youthful", "organizing"): (0.25 - _HYSTERESIS_MARGIN, 0.25 + _HYSTERESIS_MARGIN),
        ("organizing", "youthful"): (0.25 - _HYSTERESIS_MARGIN, 0.25 + _HYSTERESIS_MARGIN),
        ("organizing", "mature"): (0.50 - _HYSTERESIS_MARGIN, 0.50 + _HYSTERESIS_MARGIN),
        ("mature", "organizing"): (0.50 - _HYSTERESIS_MARGIN, 0.50 + _HYSTERESIS_MARGIN),
        ("mature", "settled"): (0.75 - _HYSTERESIS_MARGIN, 0.75 + _HYSTERESIS_MARGIN),
        ("settled", "mature"): (0.75 - _HYSTERESIS_MARGIN, 0.75 + _HYSTERESIS_MARGIN),
    }

    key = (last_label, new_label)
    if key in thresholds:
        lower, upper = thresholds[key]
        return {"lower": lower, "upper": upper}

    # Same label - no transition needed
    if last_label == new_label:
        return None

    # Non-adjacent labels (e.g., youthful->mature, youthful->settled, etc.)
    # These must pass through intermediate states, so we handle them step by step
    # For now, allow the transition since there's no direct threshold between them
    return None


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


def _compute_hud(model: ErosionModel) -> Text:
    """Compute terrain maturity metrics and format as Rich Text HUD.

    Returns a single-line HUD with 5 compact metrics:
    - dz: mean |delta_z| (terrain change rate)
    - mat: maturity (0.0 youthful to 1.0 settled)
    - e/u: erosion/uplift balance ratio
    - ch: active channel count
    - state label (youthful/organizing/mature/settled) with color coding

    Example output: "dz:0.0042  mat:0.73  e/u:1.42  ch:47  [settled]"
    """
    hud = Text()

    # 1. mean |dz|: Average of recent deltas if non-empty, else 0.0
    if model._ring_buffer:
        mean_dz = sum(model._ring_buffer) / len(model._ring_buffer)
    else:
        mean_dz = 0.0
    hud.append(f"dz:{mean_dz:.4f}", "dim white")

    # 2. maturity: model.maturity property (0.0 to 1.0)
    maturity = model.maturity
    hud.append("  ", "none")
    hud.append(f"mat:{maturity:.2f}", "cyan")

    # 3. erosion/uplift balance: compare recent trend of mean deltas
    if len(model._ring_buffer) >= 2:
        # Take last two values to determine trend
        recent = model._ring_buffer[-1]
        older = model._ring_buffer[-2]
        if recent < older:
            # Erosion winning (terrain settling down)
            ratio_str = f"{older / recent:.2f}" if recent > 0 else "inf"
            hud.append("  ", "none")
            hud.append(f"e/u:{ratio_str}", "bold bright_cyan")
        else:
            # Uplift winning (terrain building up)
            ratio_str = f"{recent / older:.2f}" if older > 0 else "inf"
            hud.append("  ", "none")
            hud.append(f"e/u:{ratio_str}", "bold bright_green")
    else:
        hud.append("  ", "none")
        hud.append("e/u:—", "dim white")

    # 4. active channels: count cells where area exceeds river threshold
    river_thresh = model.width * 0.4
    channel_count = sum(
        1
        for r in range(model.height_count)
        for c in range(model.width)
        if model.area[r][c] > river_thresh
    )
    hud.append("  ", "none")
    hud.append(f"ch:{channel_count}", "yellow")

    # 5. state label: derived from maturity thresholds with color coding
    # Use hysteresis to prevent flickering when maturity oscillates near thresholds
    raw_label = _get_label_for_maturity(maturity)
    last_label = str(_hud_state.get("last_label", ""))

    if not last_label:
        # First call - use raw label and initialize state
        chosen_label = raw_label
    else:
        # Check if we should change labels based on hysteresis
        threshold_band = _get_threshold_band(last_label, raw_label)
        if threshold_band is None:
            # No threshold to cross (same label or non-adjacent bands)
            # Just use the raw label
            chosen_label = raw_label
        else:
            # Need to cross a threshold - check if we've crossed by enough margin
            if maturity > threshold_band["upper"]:
                # Crossed upward past threshold + margin
                chosen_label = raw_label
            elif maturity < threshold_band["lower"]:
                # Crossed downward past threshold - margin
                chosen_label = raw_label
            else:
                # Still in hysteresis zone - keep previous label
                chosen_label = last_label

    # Update hysteresis state
    _hud_state["last_label"] = chosen_label
    _hud_state["last_maturity"] = maturity

    # Apply style for chosen label
    if chosen_label == "youthful":
        style = "bold bright_green"
    elif chosen_label == "organizing":
        style = "bold yellow"
    elif chosen_label == "mature":
        style = "bold cyan"
    else:
        style = "dim white"

    hud.append("  ", "none")
    hud.append(f"[{chosen_label}]", style)

    return hud


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
        # Pass session_start to enable session-scale pacing
        m = ErosionModel(width=max(w * 2, 1), height=max(h * 2, 2), session_start=time.time())
        for _ in range(5):
            m.step()
        return m

    def _render() -> Panel:
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
            # Run multiple substeps per frame based on session maturity
            for _ in range(model.substeps):
                model.step()
            # Decay activity memory once per frame (not per substep)
            if model is not None:
                model.decay_activity_memory()
            rendered = render_terrain(model, supersample=2)
            rendered = _clamp_rendered(rendered, width=w, height=panel_rows)
        except Exception as exc:
            rendered = Text(f"(terrain error: {exc})")
            rendered.no_wrap = True

        # Overlay transient event effects (before agent stamps so agents render on top)
        if model._active_effects:
            effect_stamps = render_effect_stamps(model, panel_rows, w, supersample=2)
            rendered = overlay_stamps(rendered, effect_stamps)

        if agents_cache and model is not None:
            stamps = sim.update(agents_cache, panel_rows, w, model)
            rendered = overlay_stamps(rendered, stamps)

        tick += 1
        n = len(agents_cache)
        title = f"Terrain  t={tick}" + (f"  [{n} agents]" if n else "")

        # Compute HUD metrics
        hud = _compute_hud(model) if model is not None else Text("")

        return Panel(rendered, title=title, subtitle=hud, border_style="green")

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
