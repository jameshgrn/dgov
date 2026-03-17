"""SPIM erosion terrain model for dgov dashboard."""

# Adapted from scilint/tui/erosion.py (stream-power-law incision model)

from __future__ import annotations

import math
import random

from rich.text import Text

# D8 neighbor offsets: (dy, dx)
_D8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
_D8_DIST = [math.sqrt(2), 1.0, math.sqrt(2), 1.0, 1.0, math.sqrt(2), 1.0, math.sqrt(2)]

# Hillshade light direction: azimuth=315° (upper-left), altitude=45°
_ALT = math.radians(45)
_AZ = math.radians(315)
_LIGHT_X = math.cos(_ALT) * math.sin(_AZ)
_LIGHT_Y = -math.cos(_ALT) * math.cos(_AZ)  # row increases downward
_LIGHT_Z = math.sin(_ALT)


class ErosionModel:
    """Stream-power-law erosion on a 2D heightfield."""

    def __init__(
        self,
        width: int = 60,
        height: int = 32,
        K: float = 0.003,
        m: float = 0.5,
        n: float = 1.0,
        uplift: float = 0.001,
        seed: int | None = None,
    ) -> None:
        self.width = width
        self.height_count = height
        self.K = K
        self.m = m
        self.n = n
        self.uplift = uplift

        rng = random.Random(seed)

        # Random base [0.25, 0.65] + center-high bias so terrain drains to all edges.
        grid: list[list[float]] = []
        max_edge_dist = max((min(height, width) / 2.0) - 1.0, 1.0)
        for r in range(height):
            row: list[float] = []
            for c in range(width):
                edge_dist = min(r, height - 1 - r, c, width - 1 - c)
                edge_bias = max(0.0, min(1.0, edge_dist / max_edge_dist))
                row.append(rng.uniform(0.25, 0.65) + 0.35 * edge_bias)
            grid.append(row)

        # 2 passes of 3x3 box blur
        for _ in range(2):
            grid = self._box_blur(grid, height, width)

        # All outer edges are drains at 0.0.
        self._apply_boundary_drains(grid)

        self.height = grid
        self.area: list[list[float]] = [[1.0] * width for _ in range(height)]

    @staticmethod
    def _box_blur(grid: list[list[float]], rows: int, cols: int) -> list[list[float]]:
        out: list[list[float]] = []
        for r in range(rows):
            new_row: list[float] = []
            for c in range(cols):
                total = 0.0
                count = 0
                for dr in (-1, 0, 1):
                    for dc in (-1, 0, 1):
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < rows and 0 <= nc < cols:
                            total += grid[nr][nc]
                            count += 1
                new_row.append(total / count)
            out.append(new_row)
        return out

    @staticmethod
    def _apply_boundary_drains(grid: list[list[float]]) -> None:
        """Enforce fixed-value drain boundaries along all outer edges."""
        if not grid or not grid[0]:
            return
        rows = len(grid)
        cols = len(grid[0])
        grid[0] = [0.0] * cols
        grid[rows - 1] = [0.0] * cols
        for r in range(rows):
            grid[r][0] = 0.0
            grid[r][cols - 1] = 0.0

    def step(self) -> None:
        h = self.height
        rows = self.height_count
        cols = self.width

        # 1. Collect and sort cells by decreasing elevation
        cells = []
        for r in range(rows):
            for c in range(cols):
                cells.append((h[r][c], r, c))
        cells.sort(reverse=True)

        # 2. D8 flow routing: each cell flows to steepest downhill neighbor
        receiver = [[(-1, -1)] * cols for _ in range(rows)]
        slope = [[0.0] * cols for _ in range(rows)]

        for _, r, c in cells:
            max_slope = 0.0
            best = (-1, -1)
            for k in range(8):
                nr = r + _D8[k][0]
                nc = c + _D8[k][1]
                if 0 <= nr < rows and 0 <= nc < cols:
                    drop = h[r][c] - h[nr][nc]
                    if drop > 0:
                        s = drop / _D8_DIST[k]
                        if s > max_slope:
                            max_slope = s
                            best = (nr, nc)
            receiver[r][c] = best
            slope[r][c] = max_slope

        # 3. Flow accumulation: pass flow from high to low cells
        area = [[1.0] * cols for _ in range(rows)]
        for _, r, c in cells:
            nr, nc = receiver[r][c]
            if nr >= 0:
                area[nr][nc] += area[r][c]

        self.area = area

        # 4. Erosion: E = K * A^m * S^n, subtract, clamp at 0
        K, m, n = self.K, self.m, self.n
        for _, r, c in cells:
            if r in (0, rows - 1) or c in (0, cols - 1):
                continue  # skip drain row
            a = area[r][c]
            s = slope[r][c]
            if s > 0:
                erosion = K * (a**m) * (s**n)
                h[r][c] = max(0.0, h[r][c] - erosion)

        # 5. Uplift: add constant to all non-drain cells
        u = self.uplift
        if u > 0:
            for r in range(1, max(rows - 1, 1)):
                row = h[r]
                for c in range(1, max(cols - 1, 1)):
                    row[c] += u

        self._apply_boundary_drains(h)


_Q = 8  # quantization step — snaps RGB to 32 levels, reduces unique styles per frame


def _clamp(v: int) -> int:
    clamped = max(0, min(255, v))
    return (clamped // _Q) * _Q


def _elevation_color(elev: float, shade: float) -> tuple[int, int, int]:
    """Map elevation to RGB, modulated by hillshade for 3D effect."""
    s = 0.3 + 0.7 * shade
    if elev > 0.85:
        # Snow/peaks
        return (_clamp(int(230 * s)), _clamp(int(225 * s)), _clamp(int(215 * s)))
    if elev > 0.65:
        # Rocky/tan ridges
        return (_clamp(int(165 * s)), _clamp(int(130 * s)), _clamp(int(85 * s)))
    if elev > 0.40:
        # Green slopes
        return (_clamp(int(65 * s)), _clamp(int(140 * s)), _clamp(int(55 * s)))
    # Dark green lowland
    return (_clamp(int(45 * s)), _clamp(int(100 * s)), _clamp(int(40 * s)))


def _river_color(flow: float, shade: float) -> tuple[int, int, int]:
    """Blue river, brighter with more flow."""
    s = 0.4 + 0.6 * shade
    intensity = min(1.0, 0.5 + 0.5 * math.log(max(flow, 1)) / 6.0)
    return (
        _clamp(int(30 * s)),
        _clamp(int(80 * intensity * s)),
        _clamp(int(200 * intensity * s)),
    )


def render_terrain(model: ErosionModel) -> Text:
    """Render 3D hillshaded terrain with half-block chars and river overlay."""
    h = model.height
    rows = model.height_count
    cols = model.width
    area = model.area

    river_thresh = max(cols * 0.4, 20.0)

    # Compute hillshade per cell
    shade = [[0.5] * cols for _ in range(rows)]
    for r in range(rows):
        for c in range(cols):
            # Central differences for gradient
            if 0 < c < cols - 1:
                dzdx = (h[r][c + 1] - h[r][c - 1]) / 2.0
            elif c == 0:
                dzdx = h[r][c + 1] - h[r][c] if cols > 1 else 0.0
            else:
                dzdx = h[r][c] - h[r][c - 1]

            if 0 < r < rows - 1:
                dzdy = (h[r + 1][c] - h[r - 1][c]) / 2.0
            elif r == 0:
                dzdy = h[r + 1][c] - h[r][c] if rows > 1 else 0.0
            else:
                dzdy = h[r][c] - h[r - 1][c]

            # Surface normal (exaggerated for visible relief)
            scale = 8.0
            nx = -dzdx * scale
            ny = -dzdy * scale
            nz = 1.0
            mag = math.sqrt(nx * nx + ny * ny + nz * nz)
            nx /= mag
            ny /= mag
            nz /= mag

            dot = nx * _LIGHT_X + ny * _LIGHT_Y + nz * _LIGHT_Z
            shade[r][c] = max(0.0, min(1.0, dot * 0.5 + 0.5))

    def _pixel(r: int, c: int) -> tuple[int, int, int]:
        elev = h[r][c]
        s = shade[r][c]
        flow = area[r][c]
        if flow > river_thresh and elev < 0.80:
            return _river_color(flow, s)
        return _elevation_color(elev, s)

    # Half-block rendering: ▀ with fg=top row, bg=bottom row
    text = Text()
    pair_count = rows // 2
    for pair in range(pair_count):
        r = pair * 2
        for c in range(cols):
            rt, gt, bt = _pixel(r, c)
            rb, gb, bb = _pixel(r + 1, c)
            text.append("▀", style=f"rgb({rt},{gt},{bt}) on rgb({rb},{gb},{bb})")
        if pair < pair_count - 1:
            text.append("\n")

    # Handle odd row count
    if rows % 2 == 1:
        text.append("\n")
        last = rows - 1
        for c in range(cols):
            rc, gc, bc = _pixel(last, c)
            text.append("▀", style=f"rgb({rc},{gc},{bc})")

    return text


def overlay_agents(text: Text, model: ErosionModel, agents: list[dict]) -> Text:
    """Stamp agent glyphs onto rendered terrain. Each agent dict has: slug, state, role, agent."""
    if not agents or model.width < 1 or model.height_count < 2:
        return text
    import hashlib

    lines = text.plain.split("\n")
    if not lines:
        return text
    display_rows = len(lines)
    display_cols = len(lines[0]) if lines else model.width

    # Build a map of (row, col) -> (glyph, style_string) for agents
    stamps: dict[tuple[int, int], tuple[str, str]] = {}
    for i, ag in enumerate(agents):
        slug = ag.get("slug", f"agent-{i}")
        state = ag.get("state", "active")
        role = ag.get("role", "worker")

        # Deterministic position from slug hash
        h = hashlib.md5(slug.encode()).digest()
        base_col = (h[0] + h[1] * 256) % max(display_cols - 4, 1) + 2
        base_row = (h[2] + h[3] * 256) % max(display_rows - 2, 1) + 1

        # Simple drift for active agents based on time
        if state == "active":
            import time as _time

            t = int(_time.time())
            drift_x = ((h[4] + t) % 5) - 2
            drift_y = ((h[5] + t // 3) % 3) - 1
            base_col = max(1, min(display_cols - 2, base_col + drift_x))
            base_row = max(0, min(display_rows - 1, base_row + drift_y))

        # Glyph and color by role/state
        if role == "lt-gov":
            glyph = "\u25c6"  # diamond
            color = "bold magenta"
        elif state == "done" or state == "merged":
            glyph = "\u2713"  # checkmark
            color = "bold green"
        elif state == "failed":
            glyph = "\u2717"  # x mark
            color = "bold red"
        else:
            glyph = "\u2022"  # bullet
            color = "bold white"

        stamps[(base_row, base_col)] = (glyph, color)

    # Overlay stamps onto a copy of the original text.
    # Same-length plain replacement preserves all existing Rich style spans.
    plain = text.plain
    chars = list(plain)
    # Map (row, col) -> flat offset
    offsets: dict[int, str] = {}  # offset -> style_string
    for (row, col), (glyph, style_str) in stamps.items():
        if row >= display_rows or col >= len(lines[row]):
            continue
        offset = sum(len(lines[r]) + 1 for r in range(row)) + col
        if 0 <= offset < len(chars):
            chars[offset] = glyph
            offsets[offset] = style_str

    result = text.copy()
    result.plain = "".join(chars)  # same length → spans preserved
    for offset, style_str in offsets.items():
        result.stylize(style_str, offset, offset + 1)
    return result
