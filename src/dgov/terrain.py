"""SPIM erosion terrain model for dgov dashboard."""

# Adapted from scilint/tui/erosion.py (stream-power-law incision model)

from __future__ import annotations

import hashlib
import math
import random
from typing import Any

from rich.text import Text

# D8 neighbor offsets: (dy, dx)
_D8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
_D8_DIST = [math.sqrt(2), 1.0, math.sqrt(2), 1.0, 1.0, math.sqrt(2), 1.0, math.sqrt(2)]

_LETTER_D = ["11110", "10001", "10001", "10001", "10001", "10001", "11110"]
_LETTER_G = ["01110", "10001", "10000", "10110", "10001", "10001", "01110"]
_LETTER_O = ["01110", "10001", "10001", "10001", "10001", "10001", "01110"]
_LETTER_V = ["10001", "10001", "10001", "10001", "01010", "01010", "00100"]


def _build_dgov_bitmap():
    letters = [_LETTER_D, _LETTER_G, _LETTER_O, _LETTER_V]
    bitmap = []
    for r in range(7):
        row = []
        for i, letter in enumerate(letters):
            if i > 0:
                row.extend([0] * 2)
            row.extend(int(c) for c in letter[r])
        bitmap.append(row)
    return bitmap


def _stamp_dgov(grid, rows, cols):
    bitmap = _build_dgov_bitmap()
    bh = len(bitmap)
    bw = len(bitmap[0])
    scale = max(1, int(cols * 0.5 / bw))
    total_w = bw * scale
    total_h = bh * scale
    start_r = (rows - total_h) // 2
    start_c = (cols - total_w) // 2
    for br in range(bh):
        for bc in range(bw):
            if bitmap[br][bc]:
                for dr in range(scale):
                    for dc in range(scale):
                        gr = start_r + br * scale + dr
                        gc = start_c + bc * scale + dc
                        if 1 <= gr < rows - 1 and 1 <= gc < cols - 1:
                            grid[gr][gc] *= 0.97


# Hillshade light direction: azimuth=315° (upper-left), altitude=45°
_ALT = math.radians(45)
_AZ = math.radians(315)
_LIGHT_X = math.cos(_ALT) * math.sin(_AZ)
_LIGHT_Y = -math.cos(_ALT) * math.cos(_AZ)  # row increases downward
_LIGHT_Z = math.sin(_ALT)


def _spawn_position_from_slug(slug: str, rows: int, cols: int) -> tuple[float, float]:
    """Return the deterministic spawn position used for agents and terrain events."""
    digest = hashlib.md5(slug.encode()).digest()
    col = float((digest[0] + digest[1] * 256) % max(cols - 4, 1) + 2)
    row = float((digest[2] + digest[3] * 256) % max(rows - 2, 1) + 1)
    return row, col


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
        self._rng = random.Random(seed)

        # Random base [0.25, 0.65] + center-high bias so terrain drains to all edges.
        grid: list[list[float]] = []
        max_edge_dist = max((min(height, width) / 2.0) - 1.0, 1.0)
        for r in range(height):
            row: list[float] = []
            for c in range(width):
                edge_dist = min(r, height - 1 - r, c, width - 1 - c)
                edge_bias = max(0.0, min(1.0, edge_dist / max_edge_dist))
                row.append(self._rng.uniform(0.25, 0.65) + 0.35 * edge_bias)
            grid.append(row)

        # 2 passes of 3x3 box blur
        for _ in range(2):
            grid = self._box_blur(grid, height, width)

        # All outer edges are drains at 0.0.
        _stamp_dgov(grid, height, width)
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

    def terrain_event(self, event_type: str, row: int, col: int, intensity: float = 1.0) -> None:
        """Apply a localized terrain perturbation around an interior cell."""
        if not (0 <= row < self.height_count and 0 <= col < self.width):
            raise ValueError(
                f"terrain_event position out of bounds: row={row}, col={col}, "
                f"size={self.height_count}x{self.width}"
            )

        event_specs = {
            "uplift": (5, 0.08),
            "erode": (3, -0.06),
            "deposit": (3, 0.04),
            "tremor": (4, 0.02),
        }
        if event_type not in event_specs:
            raise ValueError(f"Unknown terrain event type: {event_type!r}")

        radius, base_amplitude = event_specs[event_type]
        sigma_sq = max((radius / 2.0) ** 2, 1e-9)

        row_min = max(1, row - radius)
        row_max = min(self.height_count - 2, row + radius)
        col_min = max(1, col - radius)
        col_max = min(self.width - 2, col + radius)

        for nr in range(row_min, row_max + 1):
            for nc in range(col_min, col_max + 1):
                dist_sq = float((nr - row) ** 2 + (nc - col) ** 2)
                if dist_sq > radius * radius:
                    continue
                weight = math.exp(-dist_sq / (2.0 * sigma_sq))
                if event_type == "tremor":
                    delta = self._rng.uniform(-1.0, 1.0) * base_amplitude * intensity * weight
                else:
                    delta = base_amplitude * intensity * weight
                self.height[nr][nc] = max(0.0, min(2.0, self.height[nr][nc] + delta))
                if event_type == "erode":
                    self.area[nr][nc] += weight * intensity

        self._apply_boundary_drains(self.height)


class EventTranslator:
    """Translate dgov persistence events into terrain perturbations."""

    def __init__(self) -> None:
        self._last_ts = ""

    def translate(self, event: dict) -> tuple[str, float] | None:
        ts = str(event.get("ts", ""))
        if not ts or ts <= self._last_ts:
            return None

        self._last_ts = ts
        mapping = {
            "pane_created": ("uplift", 1.0),
            "pane_done": ("erode", 1.2),
            "pane_merged": ("erode", 1.2),
            "pane_closed": ("deposit", 0.8),
            "pane_timed_out": ("deposit", 0.8),
            "pane_circuit_breaker": ("deposit", 1.5),
            "mission_failed": ("deposit", 1.5),
            "dag_failed": ("deposit", 1.5),
            "checkpoint_created": ("tremor", 0.5),
            "pane_escalated": ("uplift", 0.6),
            "pane_retry_spawned": ("uplift", 0.6),
            "review_pass": ("erode", 0.4),
            "review_fail": ("deposit", 0.6),
        }
        return mapping.get(str(event.get("event", "")))


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


def render_terrain(model: ErosionModel, supersample: int = 1) -> Text:
    """Render 3D hillshaded terrain with half-block chars and river overlay."""
    h = model.height
    rows = model.height_count
    cols = model.width
    area = model.area

    display_rows = rows // supersample
    display_cols = cols // supersample

    river_thresh = max(display_cols * 0.4, 20.0) * (supersample**2)

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

    def _pixel(dr: int, dc: int) -> tuple[int, int, int]:
        if supersample == 1:
            elev = h[dr][dc]
            s = shade[dr][dc]
            flow = area[dr][dc]
        else:
            te = ts = tf = 0.0
            n = supersample * supersample
            for sr in range(supersample):
                for sc in range(supersample):
                    r2 = dr * supersample + sr
                    c2 = dc * supersample + sc
                    te += h[r2][c2]
                    ts += shade[r2][c2]
                    tf += area[r2][c2]
            elev, s, flow = te / n, ts / n, tf / n
        if flow > river_thresh and elev < 0.80:
            return _river_color(flow, s)
        return _elevation_color(elev, s)

    # Half-block rendering: ▀ with fg=top row, bg=bottom row
    text = Text()
    pair_count = display_rows // 2
    for pair in range(pair_count):
        r = pair * 2
        for c in range(display_cols):
            rt, gt, bt = _pixel(r, c)
            rb, gb, bb = _pixel(r + 1, c)
            text.append("▀", style=f"rgb({rt},{gt},{bt}) on rgb({rb},{gb},{bb})")
        if pair < pair_count - 1:
            text.append("\n")

    # Handle odd row count
    if display_rows % 2 == 1:
        text.append("\n")
        last = display_rows - 1
        for c in range(display_cols):
            rc, gc, bc = _pixel(last, c)
            text.append("▀", style=f"rgb({rc},{gc},{bc})")

    return text


# Pixel-art sprites: 5 cols x 3 char-rows per agent.
# [top_row, mid_row, bot_row]; each row = 5 cells.
# Each cell = (fg, bg) | (fg, bg, char) | None (transparent).
# 2-tuple -> default half-block.  3-tuple -> custom glyph.
# Layout: top = head/hat, mid = eyes+arms, bot = body/feet.
_AGENT_SPRITES: dict[str, list[list]] = {
    # Claude -- purple, friendly antenna, round eyes
    "claude": [
        [
            None,
            ((180, 80, 180), (130, 50, 130)),
            ((255, 200, 255), (180, 80, 180), "\u25b3"),  # △ antenna
            ((180, 80, 180), (130, 50, 130)),
            None,
        ],
        [
            ((180, 80, 180), (130, 50, 130), "\u2576"),  # ╶ left arm
            ((255, 255, 255), (200, 100, 200), "\u25ce"),  # ◎ eye
            ((200, 100, 200), (130, 50, 130)),
            ((255, 255, 255), (200, 100, 200), "\u25ce"),  # ◎ eye
            ((180, 80, 180), (130, 50, 130), "\u2574"),  # ╴ right arm
        ],
        [
            None,
            ((130, 50, 130), (80, 20, 80), "\u25be"),  # ▾ foot
            ((130, 50, 130), (80, 20, 80)),
            ((130, 50, 130), (80, 20, 80), "\u25be"),  # ▾ foot
            None,
        ],
    ],
    # Pi -- green, flower on head, nature spirit
    "pi": [
        [
            None,
            ((100, 220, 160), (30, 140, 80)),
            ((200, 255, 220), (60, 180, 120), "\u273f"),  # ✿ flower
            ((100, 220, 160), (30, 140, 80)),
            None,
        ],
        [
            ((60, 180, 120), (30, 140, 80), "\u2576"),  # ╶ arm
            ((20, 50, 30), (80, 200, 140), "\u25d2"),  # ◒ eye (dark on bright)
            ((80, 200, 140), (30, 140, 80)),
            ((20, 50, 30), (80, 200, 140), "\u25d3"),  # ◓ eye (mirrored)
            ((60, 180, 120), (30, 140, 80), "\u2574"),  # ╴ arm
        ],
        [
            None,
            ((30, 140, 80), (20, 100, 50), "\u25be"),  # ▾ foot
            ((30, 140, 80), (20, 100, 50)),
            ((30, 140, 80), (20, 100, 50), "\u25be"),  # ▾ foot
            None,
        ],
    ],
    # Cursor -- blue, sleek arrow head, precise target eyes
    "cursor": [
        [
            None,
            None,
            ((220, 240, 255), (80, 120, 220), "\u25bc"),  # ▼ cursor tip
            None,
            None,
        ],
        [
            ((120, 160, 255), (40, 60, 150), "\u25c1"),  # ◁ left wing
            ((255, 255, 255), (80, 120, 220), "\u25a3"),  # ▣ eye (reticle)
            ((160, 200, 255), (80, 120, 220)),
            ((255, 255, 255), (80, 120, 220), "\u25a3"),  # ▣ eye (reticle)
            ((120, 160, 255), (40, 60, 150), "\u25b7"),  # ▷ right wing
        ],
        [
            None,
            ((40, 60, 150), (20, 30, 100), "\u25be"),  # ▾ foot
            ((80, 120, 220), (40, 60, 150)),
            ((40, 60, 150), (20, 30, 100), "\u25be"),  # ▾ foot
            None,
        ],
    ],
    # Codex -- gold, scholar hat, wise eyes
    "codex": [
        [
            ((200, 160, 30), (160, 120, 10), "\u2581"),  # ▁ hat brim
            ((200, 160, 30), (160, 120, 10)),
            ((255, 240, 150), (200, 160, 30)),
            ((200, 160, 30), (160, 120, 10)),
            ((200, 160, 30), (160, 120, 10), "\u2581"),  # ▁ hat brim
        ],
        [
            ((200, 160, 30), (160, 120, 10), "\u2576"),  # ╶ arm
            ((70, 45, 0), (220, 180, 40), "\u25d0"),  # ◐ eye (dark on bright)
            ((220, 180, 40), (180, 140, 20)),
            ((70, 45, 0), (220, 180, 40), "\u25d1"),  # ◑ eye (mirrored)
            ((200, 160, 30), (160, 120, 10), "\u2574"),  # ╴ arm
        ],
        [
            None,
            ((160, 120, 10), (120, 90, 10), "\u25be"),  # ▾ foot
            ((180, 140, 20), (120, 90, 10)),
            ((160, 120, 10), (120, 90, 10), "\u25be"),  # ▾ foot
            None,
        ],
    ],
    # Gemini -- indigo/blue, twin stars, cosmic
    "gemini": [
        [
            None,
            ((200, 220, 255), (60, 80, 200), "\u2726"),  # ✦ star
            ((200, 220, 255), (100, 120, 200)),
            ((200, 220, 255), (60, 80, 200), "\u2726"),  # ✦ star
            None,
        ],
        [
            ((100, 120, 200), (30, 40, 130), "\u2576"),  # ╶ arm
            ((255, 255, 255), (60, 80, 200), "\u25c8"),  # ◈ eye (faceted)
            ((140, 170, 255), (60, 80, 200)),
            ((255, 255, 255), (60, 80, 200), "\u25c8"),  # ◈ eye (faceted)
            ((100, 120, 200), (30, 40, 130), "\u2574"),  # ╴ arm
        ],
        [
            None,
            ((30, 40, 130), (15, 20, 80), "\u25be"),  # ▾ foot
            ((60, 80, 200), (30, 40, 130)),
            ((30, 40, 130), (15, 20, 80), "\u25be"),  # ▾ foot
            None,
        ],
    ],
}

# Done -- green, sparkle crown, arms raised in celebration
_DONE_SPRITE: list[list[Any]] = [
    [
        None,
        None,
        ((180, 255, 180), (80, 220, 100), "\u2726"),  # ✦ sparkle
        None,
        None,
    ],
    [
        ((255, 255, 255), (80, 220, 100), "\u2572"),  # ╲ arm raised left
        ((255, 255, 255), (80, 220, 100), "\u25d5"),  # ◕ happy eye
        ((80, 220, 100), (40, 160, 50)),
        ((255, 255, 255), (80, 220, 100), "\u25d5"),  # ◕ happy eye
        ((255, 255, 255), (80, 220, 100), "\u2571"),  # ╱ arm raised right
    ],
    [
        None,
        ((40, 160, 50), (20, 100, 30), "\u25be"),  # ▾ foot
        ((40, 160, 50), (20, 100, 30)),
        ((40, 160, 50), (20, 100, 30), "\u25be"),  # ▾ foot
        None,
    ],
]

# Failed -- red, skull mark, x-eyes, collapsed
_FAILED_SPRITE: list[list[Any]] = [
    [
        None,
        ((220, 60, 60), (160, 30, 30)),
        ((255, 120, 120), (220, 60, 60), "\u2716"),  # ✖ skull mark
        ((220, 60, 60), (160, 30, 30)),
        None,
    ],
    [
        None,
        ((255, 80, 80), (220, 60, 60), "\u00d7"),  # × dead eye
        ((220, 60, 60), (160, 30, 30)),
        ((255, 80, 80), (220, 60, 60), "\u00d7"),  # × dead eye
        None,
    ],
    [
        None,
        None,
        ((160, 30, 30), (100, 15, 15)),
        None,
        None,
    ],
]

# LT-GOV -- gold/royal, crown, scepter sparkles
_LTGOV_SPRITE: list[list[Any]] = [
    [
        ((255, 220, 60), (220, 180, 30), "\u2727"),  # ✧ ornament
        ((255, 220, 60), (220, 180, 30)),
        ((255, 255, 220), (255, 220, 60), "\u265b"),  # ♛ crown
        ((255, 220, 60), (220, 180, 30)),
        ((255, 220, 60), (220, 180, 30), "\u2727"),  # ✧ ornament
    ],
    [
        ((220, 180, 30), (180, 140, 10), "\u2576"),  # ╶ arm
        ((70, 45, 0), (255, 220, 60), "\u25c8"),  # ◈ eye (regal, dark on gold)
        ((255, 220, 60), (220, 180, 30)),
        ((70, 45, 0), (255, 220, 60), "\u25c8"),  # ◈ eye (regal, dark on gold)
        ((220, 180, 30), (180, 140, 10), "\u2574"),  # ╴ arm
    ],
    [
        None,
        ((220, 180, 30), (180, 140, 10), "\u25be"),  # ▾ foot
        ((220, 180, 30), (180, 140, 10)),
        ((220, 180, 30), (180, 140, 10), "\u25be"),  # ▾ foot
        None,
    ],
]


class AgentSim:
    """Lightweight agent-based model overlay for the terrain display.

    Agents wander the terrain, follow slopes, repel each other,
    and LT-GOVs are attracted toward their child workers.
    """

    def __init__(self) -> None:
        self._pos: dict[str, list[float]] = {}  # slug -> [row, col]
        self._vel: dict[str, list[float]] = {}  # slug -> [vr, vc]
        self._tick = 0

    def update(
        self,
        agents: list[dict],
        rows: int,
        cols: int,
        terrain: ErosionModel | None,
    ) -> dict[tuple[int, int], tuple[str, str]]:
        """Advance one tick. Returns {(row, col): (glyph, style)}."""
        self._tick += 1
        active_slugs = {a.get("slug", "") for a in agents}

        # Prune departed agents
        for slug in list(self._pos):
            if slug not in active_slugs:
                del self._pos[slug]
                self._vel.pop(slug, None)

        # Spawn new agents at hash-derived positions
        for ag in agents:
            slug = ag.get("slug", "")
            if slug and slug not in self._pos:
                r, c = _spawn_position_from_slug(slug, rows, cols)
                self._pos[slug] = [r, c]
                self._vel[slug] = [0.0, 0.0]

        # Parent -> children map for LT-GOV attraction
        children_of: dict[str, list[str]] = {}
        for ag in agents:
            parent = ag.get("parent_slug", "")
            if parent:
                children_of.setdefault(parent, []).append(ag.get("slug", ""))

        stamps: dict[tuple[int, int], tuple[str, str]] = {}

        for ag in agents:
            slug = ag.get("slug", "")
            if not slug or slug not in self._pos:
                continue
            state = ag.get("state", "active")
            role = ag.get("role", "worker")
            r, c = self._pos[slug]
            vr, vc = self._vel[slug]

            if state in ("done", "merged"):
                # Settle: decelerate to a stop
                vr *= 0.4
                vc *= 0.4
            elif state == "failed":
                # Jitter in place
                vr = random.uniform(-0.3, 0.3)
                vc = random.uniform(-0.3, 0.3)
            else:
                # Wander: random nudge
                vr += random.uniform(-0.4, 0.4)
                vc += random.uniform(-0.4, 0.4)

                # Terrain gradient: drift downhill (follow water)
                if terrain:
                    ir, ic = int(r), int(c)
                    hr = terrain.height_count
                    wc = terrain.width
                    if 0 < ir < hr - 1 and 0 < ic < wc - 1:
                        dh_r = (
                            terrain.height[min(ir + 1, hr - 1)][ic]
                            - terrain.height[max(ir - 1, 0)][ic]
                        )
                        dh_c = (
                            terrain.height[ir][min(ic + 1, wc - 1)]
                            - terrain.height[ir][max(ic - 1, 0)]
                        )
                        vr -= dh_r * 0.6
                        vc -= dh_c * 0.6

                # LT-GOV: attract toward child centroid
                if role == "lt-gov" and slug in children_of:
                    cps = [self._pos[cs] for cs in children_of[slug] if cs in self._pos]
                    if cps:
                        cr = sum(p[0] for p in cps) / len(cps)
                        cc = sum(p[1] for p in cps) / len(cps)
                        vr += (cr - r) * 0.12
                        vc += (cc - c) * 0.12

                # Damping
                vr *= 0.65
                vc *= 0.65

                # Speed limit
                speed = math.sqrt(vr * vr + vc * vc)
                if speed > 1.5:
                    vr = vr / speed * 1.5
                    vc = vc / speed * 1.5

            # Separation: repel from nearby agents
            for other_slug, op in self._pos.items():
                if other_slug == slug:
                    continue
                dr = r - op[0]
                dc = c - op[1]
                dist = math.sqrt(dr * dr + dc * dc)
                if 0.1 < dist < 6.0:
                    repel = 0.8 / dist
                    vr += dr / dist * repel
                    vc += dc / dist * repel

            # Integrate
            r += vr
            c += vc

            # Boundary clamp (stay off edges for 5x3 sprites)
            r = max(2.0, min(rows - 2.0, r))
            c = max(3.0, min(cols - 3.0, c))

            self._pos[slug] = [r, c]
            self._vel[slug] = [vr, vc]

            # Pixel-art sprite: 5 wide x 3 tall, centered on agent position
            ir, ic = int(round(r)), int(round(c))
            sprite: list[list[Any]]
            if role == "lt-gov":
                sprite = _LTGOV_SPRITE
            elif state in ("done", "merged"):
                sprite = _DONE_SPRITE
            elif state == "failed":
                sprite = _FAILED_SPRITE
            else:
                agent_name = ag.get("agent", "").split("-")[0]
                sprite = _AGENT_SPRITES.get(agent_name) or _AGENT_SPRITES["claude"]

            sprite_h = len(sprite)
            sprite_w = len(sprite[0]) if sprite else 0
            for row_offset in range(sprite_h):
                sr = ir - 1 + row_offset
                if sr < 0 or sr >= rows:
                    continue
                sprite_row = sprite[row_offset]
                for dx in range(sprite_w):
                    sc = ic - sprite_w // 2 + dx
                    if sc < 0 or sc >= cols:
                        continue
                    cell = sprite_row[dx]
                    if cell is None:
                        continue
                    if len(cell) == 3:
                        fg, bg, char = cell
                    else:
                        fg, bg = cell
                        char = "\u2580"
                    # Head decoration pulse
                    if row_offset == 0 and dx == sprite_w // 2 and self._tick % 4 < 2:
                        fg = (min(fg[0] + 30, 255), min(fg[1] + 30, 255), min(fg[2] + 30, 255))
                    # Eye glow
                    if row_offset == 1 and dx in (1, sprite_w - 2) and self._tick % 8 < 2:
                        fg = (min(fg[0] + 40, 255), min(fg[1] + 40, 255), min(fg[2] + 40, 255))
                    stamps[(sr, sc)] = (
                        char,
                        f"rgb({fg[0]},{fg[1]},{fg[2]}) on rgb({bg[0]},{bg[1]},{bg[2]})",
                    )

        # Interaction sparks: adjacent agents get a lightning bolt between them
        pos_list = [
            (ag.get("slug", ""), self._pos.get(ag.get("slug", ""), [0, 0]))
            for ag in agents
            if ag.get("slug", "") in self._pos
        ]
        for i in range(len(pos_list)):
            s1, p1 = pos_list[i]
            for j in range(i + 1, len(pos_list)):
                s2, p2 = pos_list[j]
                dist = abs(int(round(p1[0])) - int(round(p2[0]))) + abs(
                    int(round(p1[1])) - int(round(p2[1]))
                )
                if dist <= 2:
                    mr = (int(round(p1[0])) + int(round(p2[0]))) // 2
                    mc = (int(round(p1[1])) + int(round(p2[1]))) // 2
                    if (mr, mc) not in stamps:
                        stamps[(mr, mc)] = ("\u26a1", "bold yellow")

        return stamps


def overlay_stamps(text: Text, stamps: dict[tuple[int, int], tuple[str, str]]) -> Text:
    """Apply stamp glyphs onto rendered terrain text, preserving original styles."""
    if not stamps:
        return text
    lines = text.plain.split("\n")
    if not lines:
        return text
    display_rows = len(lines)

    plain = text.plain
    chars = list(plain)
    offsets: dict[int, str] = {}
    for (row, col), (glyph, style_str) in stamps.items():
        if row >= display_rows or col >= len(lines[row]):
            continue
        offset = sum(len(lines[r]) + 1 for r in range(row)) + col
        if 0 <= offset < len(chars):
            chars[offset] = glyph
            offsets[offset] = style_str

    result = text.copy()
    result.plain = "".join(chars)
    for offset, style_str in offsets.items():
        result.stylize(style_str, offset, offset + 1)
    return result
