# Sprite System Check

**Date:** 2026-03-17  
**File:** `src/dgov/terrain.py`

## Summary

The terrain module implements a SPIM (Stream-Power-Law Incision Model) erosion simulation with an integrated agent-based sprite overlay system.

### Key Components

- **ErosionModel**: 2D heightfield with D8 flow routing, flow accumulation, and stream-power erosion. Boundary drains at edges. Supports localized `terrain_event()` perturbations (uplift, erode, deposit, tremor).
- **AgentSim**: Lightweight particle system where agents wander terrain, follow downhill gradients, repel each other, and LT-GOVs gravitate toward child workers. Each agent renders as a 3-cell-wide pixel-art sprite using half-block characters.
- **Render Pipeline**: `render_terrain()` produces hillshaded RGB terrain with river overlay. `overlay_stamps()` composites agent sprites on top.

### Sprite Definitions

| Agent    | Color Palette         | Notes                      |
|----------|-----------------------|----------------------------|
| pi       | Green                 | Default agent              |
| claude   | Purple                | Fallback for unknown agents|
| codex    | Yellow/Gold           |                            |
| gemini   | Blue                  |                            |
| hunter   | Orange                |                            |
| cursor   | Steel Blue            |                            |
| lt-gov   | Gold (bright)         | Governor role              |
| done     | Green (settled)       | Terminal state             |
| failed   | Red (jittery)         | Terminal state             |

### Event Translation

`EventTranslator` maps dgov persistence events to terrain perturbations (e.g., `pane_created` → uplift, `pane_done` → erode, `mission_failed` → deposit).

### Status

All sprite definitions present, no missing agents. System is functional.
