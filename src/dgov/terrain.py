"""SPIM erosion terrain model for dgov dashboard."""

# Adapted from scilint/tui/erosion.py (stream-power-law incision model)

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass

from rich.text import Text

# D8 neighbor offsets: (dy, dx)
_D8 = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
_D8_DIST = [math.sqrt(2), 1.0, math.sqrt(2), 1.0, 1.0, math.sqrt(2), 1.0, math.sqrt(2)]

# Keyframes: (session_age_hrs, warmth, sat, contrast, perturbation_scale)
# Dawn: warm golden light (high warmth, low-medium sat)
# Morning: vibrant and punchy (lower warmth, high sat/contrast)
# Midday: bright but less intense (moderate warmth/sat/contrast)
# Afternoon: gentle warmth building (higher warmth)
# Evening: calm cool tones (low warmth/sat/contrast)
# Late night: subdued and grey (very low values)
_PHASE_KEYS = [
    (0.0, 0.75, 0.60, 0.70, 1.00),
    (0.5, 0.45, 1.00, 1.00, 0.90),
    (2.0, 0.50, 0.95, 0.95, 0.70),
    (4.0, 0.60, 0.85, 0.85, 0.50),
    (5.0, 0.48, 0.75, 0.78, 0.40),
    (6.0, 0.35, 0.65, 0.70, 0.30),
    (8.0, 0.25, 0.55, 0.60, 0.20),
]


def _clamp(v: int) -> int:
    clamped = max(0, min(255, v))
    return (clamped // _Q) * _Q


def _session_phase(session_age_hours: float) -> dict:
    """Compute visual parameters from session age for workday dramaturgy.

    Returns dict with:
        warmth: float 0.0 (cool blue) to 1.0 (warm amber)
        saturation: float 0.0 (grey) to 1.0 (vivid)
        contrast: float 0.5 (flat) to 1.0 (punchy)
        perturbation_scale: float 0.0 to 1.0 (event intensity multiplier)
    """
    if session_age_hours <= 0.0:
        return _phase_at_hour(0.0)
    if session_age_hours >= _PHASE_KEYS[-1][0]:
        return _phase_at_hour(_PHASE_KEYS[-1][0])

    # Find the two keyframes to lerp between
    for i in range(len(_PHASE_KEYS) - 1):
        hour_a, _, _, _, _ = _PHASE_KEYS[i]
        hour_b, _, _, _, _ = _PHASE_KEYS[i + 1]
        if hour_a <= session_age_hours <= hour_b:
            return _lerp_between(session_age_hours, _PHASE_KEYS[i], _PHASE_KEYS[i + 1])

    # Should never reach here due to clamp above
    return _phase_at_hour(0.0)


def _phase_at_hour(hour: float) -> dict:
    """Return phase dict for an exact hour (used as helper for clamping)."""
    for h, warmth, saturation, contrast, perturbation in _PHASE_KEYS:
        if abs(h - hour) < 1e-9:
            return {
                "warmth": warmth,
                "saturation": saturation,
                "contrast": contrast,
                "perturbation_scale": perturbation,
            }
    # Fallback to last keyframe
    h, warmth, saturation, contrast, perturbation = _PHASE_KEYS[-1]
    return {
        "warmth": warmth,
        "saturation": saturation,
        "contrast": contrast,
        "perturbation_scale": perturbation,
    }


def _lerp_between(age_hours: float, key_a: tuple, key_b: tuple) -> dict:
    """Lerp between two phase keyframes."""
    hour_a, w_a, s_a, c_a, p_a = key_a
    hour_b, w_b, s_b, c_b, p_b = key_b

    if abs(hour_b - hour_a) < 1e-9:
        return {
            "warmth": w_a,
            "saturation": s_a,
            "contrast": c_a,
            "perturbation_scale": p_a,
        }

    t = (age_hours - hour_a) / (hour_b - hour_a)
    return {
        "warmth": w_a + (w_b - w_a) * t,
        "saturation": s_a + (s_b - s_a) * t,
        "contrast": c_a + (c_b - c_a) * t,
        "perturbation_scale": p_a + (p_b - p_a) * t,
    }


_LETTER_D = ["11110", "10001", "10001", "10001", "10001", "10001", "11110"]
_LETTER_G = ["01110", "10001", "10000", "10110", "10001", "10001", "01110"]
_LETTER_O = ["01110", "10001", "10001", "10001", "10001", "10001", "01110"]
_LETTER_V = ["10001", "10001", "10001", "10001", "01010", "01010", "00100"]


@dataclass
class ActiveEffect:
    """A transient visual effect from a terrain event."""

    event_type: str
    row: int
    col: int
    intensity: float
    birth_tick: int
    max_age: int = 20


# Event type to glyph/color mapping for visual feedback (Unicode glyphs)
_EFFECT_GLYPHS = {
    "uplift": ("△", "bold bright_green"),  # tectonic rise
    "erode": ("≋", "bold bright_cyan"),  # water/incision
    "deposit": ("▽", "bold yellow"),  # sediment settling
    "tremor": ("∿", "dim white"),  # seismic wave
    "meteor": ("✸", "bold bright_red"),  # impact burst
    "volcano": ("◉", "bold bright_magenta"),  # eruptive center
}


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


def _stamp_dgov(grid, erodibility, rows, cols):
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
                            erodibility[gr][gc] = 5.0
                            # Ensure stamp area is fully submerged/engraved initially
                            # so the shape is very distinct from the start
                            grid[gr][gc] = 0.35


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
    """Stream-power-law erosion on a 2D heightfield with session-scale pacing.

    Session controller parameters (tunable):
    - tau: decay time constant in hours, default 3.0
      Determines how quickly the simulation transitions from youthful to mature state.
    - k_floor_fraction: floor fraction for erodibility K, default 0.3
      Effective K decays to this percentage of original as session ages.
    - uplift_floor_fraction: floor fraction for uplift rate, default 0.2
      Effective uplift decays to this percentage of original as session ages.
    - reference_delta: initial mean |delta_z| for maturity normalization
      Used to compute the maturity metric from current delta statistics.
    - ring_buffer_size: number of recent delta samples for smoothing, default 50
    """

    # Controller tunables
    TAU_HOURS = 3.0
    K_FLOOR_FRACTION = 0.3
    UPLIFT_FLOOR_FRACTION = 0.2
    REFERENCE_DELTA = 0.015  # typical initial mean |delta_z| after first ~10 steps
    RING_BUFFER_SIZE = 50

    def __init__(
        self,
        width: int = 60,
        height: int = 32,
        K: float = 0.003,
        m: float = 0.5,
        n: float = 1.0,
        uplift: float = 0.001,
        seed: int | None = None,
        session_start: float | None = None,
    ) -> None:
        self.width = width
        self.height_count = height
        self.K = K
        self.m = m
        self.n = n
        self.uplift = uplift
        self.session_start: float | None = session_start
        self._rng = random.Random(seed)

        # Effect tracking buffer (max 100 active effects)
        self._active_effects: list[ActiveEffect] = []
        self._tick: int = 0

        # Activity memory field for channel scars / perturbation traces
        self._activity_memory: list[list[float]] = [[0.0] * width for _ in range(height)]

        # Session-scale pacing state
        self._ring_buffer: list[float] = []  # last N mean |delta_z| values
        self.erodibility = [[1.0] * width for _ in range(height)]

        # Stream order grid for Strahler-like river classification
        self.stream_order: list[list[int]] = [[0] * width for _ in range(height)]

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

        # Composite tectonic base-relief generator for geologically plausible terrain
        # All structure parameters sampled once, then applied as smooth spatial fields

        # 1. Warped regional tilt for basin-scale drainage asymmetry
        tilt_angle = self._rng.uniform(0, 2 * math.pi)
        tilt_amp = self._rng.uniform(0.08, 0.15)
        warp_freq_r = self._rng.uniform(0.3, 0.6) / height
        warp_freq_c = self._rng.uniform(0.3, 0.6) / width
        warp_phase_r = self._rng.uniform(0, 2 * math.pi)
        warp_phase_c = self._rng.uniform(0, 2 * math.pi)
        for r in range(height):
            for c in range(width):
                tilt_dir = c * math.cos(tilt_angle) + r * math.sin(tilt_angle)
                warp = math.sin(r * warp_freq_r + warp_phase_r) * math.sin(
                    c * warp_freq_c + warp_phase_c
                )
                grid[r][c] += tilt_amp * tilt_dir / max(height, width) + 0.03 * warp

        # 2. Fold belt(s): anticline/syncline style ridge-valley patterns
        num_fold_belts = self._rng.randint(1, 3)
        for _ in range(num_fold_belts):
            fold_angle = self._rng.uniform(0, math.pi)  # folds are bidirectional
            fold_nx, fold_ny = math.cos(fold_angle), math.sin(fold_angle)
            fold_cx = self._rng.uniform(width * 0.2, width * 0.8)
            fold_cy = self._rng.uniform(height * 0.2, height * 0.8)
            fold_width = self._rng.uniform(6.0, 12.0)  # across-strike width
            fold_length = self._rng.uniform(10.0, 20.0)  # along-strike extent
            fold_amp = self._rng.uniform(0.15, 0.35)
            fold_wave_len = self._rng.uniform(4.0, 8.0)  # wavelength of ridge-valley repeats

            for r in range(height):
                for c in range(width):
                    # Perpendicular distance from fold axis (across strike)
                    dist_across = abs((c - fold_cx) * fold_nx + (r - fold_cy) * fold_ny)
                    # Parallel distance along fold axis
                    dist_along = (c - fold_cx) * (-fold_ny) + (r - fold_cy) * fold_nx

                    # Across-strike Gaussian falloff
                    across_falloff = math.exp(-0.5 * (dist_across / fold_width) ** 2)
                    # Along-strike cosine taper (finite length)
                    along_taper = math.cos(math.pi * dist_along / (2 * fold_length))
                    along_taper = max(0.0, along_taper)

                    # Anticline-syncline pattern: alternating ridges and valleys
                    fold_pattern = math.sin(dist_across * math.pi / fold_wave_len)

                    combined = fold_amp * across_falloff * along_taper * fold_pattern
                    grid[r][c] += combined

        # 3. Fault-controlled relief: scarps and offset blocks with finite extent
        num_faults = self._rng.randint(1, 3)
        for _ in range(num_faults):
            fault_angle = self._rng.uniform(0, 2 * math.pi)
            fault_nx, fault_ny = math.cos(fault_angle), math.sin(fault_angle)
            fault_cx = self._rng.uniform(width * 0.1, width * 0.9)
            fault_cy = self._rng.uniform(height * 0.1, height * 0.9)
            fault_width = self._rng.uniform(2.0, 5.0)  # scarp width
            fault_length = self._rng.uniform(8.0, 18.0)  # along-fault extent
            fault_scarp = self._rng.uniform(0.12, 0.28)  # uplift on one side
            fault_offset_var = self._rng.uniform(0.02, 0.08)  # local roughness

            for r in range(height):
                for c in range(width):
                    # Distance perpendicular to fault trace
                    dist_perp = (c - fault_cx) * fault_nx + (r - fault_cy) * fault_ny
                    # Distance parallel to fault trace
                    dist_parallel = (c - fault_cx) * (-fault_ny) + (r - fault_cy) * fault_ny

                    # Along-fault taper (finite length)
                    along_taper = math.cos(math.pi * dist_parallel / (2 * fault_length))
                    along_taper = max(0.0, along_taper)

                    # Smooth scarp transition (sigmoid-like)
                    scarp_profile = math.tanh(dist_perp / fault_width)

                    # Finite-width block with localized roughness
                    fault_field = fault_scarp * scarp_profile * along_taper
                    # Add small-scale roughness (deterministic via position, not RNG per cell)
                    roughness = (
                        fault_offset_var
                        * math.sin(dist_parallel * 0.5)
                        * math.sin(dist_perp * 0.3)
                    )
                    grid[r][c] += fault_field + roughness

        # 4. Secondary features: domes, basins, subdued volcanic cones, island-like highs
        feature_choice = self._rng.choice(["domes", "basins", "cones", "islands", "mixed"])

        if feature_choice in ("domes", "mixed"):
            num_domes = self._rng.randint(1, 3)
            for _ in range(num_domes):
                dome_r = self._rng.randint(3, height - 4)
                dome_c = self._rng.randint(3, width - 4)
                dome_radius = self._rng.uniform(5.0, 10.0)
                dome_amp = self._rng.uniform(0.1, 0.25)
                for r in range(height):
                    for c in range(width):
                        dsq = (r - dome_r) ** 2 + (c - dome_c) ** 2
                        if dsq < dome_radius * dome_radius:
                            dome = dome_amp * math.exp(-dsq / (2.0 * (dome_radius / 2.5) ** 2))
                            grid[r][c] += dome

        if feature_choice in ("basins", "mixed"):
            num_basins = self._rng.randint(1, 2)
            for _ in range(num_basins):
                basin_r = self._rng.randint(3, height - 4)
                basin_c = self._rng.randint(3, width - 4)
                basin_radius = self._rng.uniform(6.0, 12.0)
                basin_depth = self._rng.uniform(0.08, 0.18)
                for r in range(height):
                    for c in range(width):
                        dsq = (r - basin_r) ** 2 + (c - basin_c) ** 2
                        if dsq < basin_radius * basin_radius:
                            basin = -basin_depth * (1.0 - (dsq / basin_radius**2) ** 0.5)
                            grid[r][c] += basin

        if feature_choice in ("cones", "mixed"):
            num_cones = self._rng.randint(1, 3)
            for _ in range(num_cones):
                cone_r = self._rng.randint(3, height - 4)
                cone_c = self._rng.randint(3, width - 4)
                cone_radius = self._rng.uniform(3.0, 7.0)
                cone_amp = self._rng.uniform(0.08, 0.18)
                for r in range(height):
                    for c in range(width):
                        dist = math.sqrt((r - cone_r) ** 2 + (c - cone_c) ** 2)
                        if dist < cone_radius:
                            # Subdued cone (not dramatic volcano)
                            cone = cone_amp * (1.0 - dist / cone_radius) ** 1.5
                            grid[r][c] += cone

        if feature_choice in ("islands", "mixed"):
            # Lower background slightly for contrast
            for r in range(height):
                for c in range(width):
                    grid[r][c] -= 0.05
            num_highs = self._rng.randint(3, 6)
            for _ in range(num_highs):
                high_r = self._rng.randint(2, height - 3)
                high_c = self._rng.randint(2, width - 3)
                high_radius = self._rng.uniform(2.5, 5.5)
                high_amp = self._rng.uniform(0.15, 0.35)
                for r in range(height):
                    for c in range(width):
                        dsq = (r - high_r) ** 2 + (c - high_c) ** 2
                        if dsq < high_radius * high_radius:
                            high = high_amp * math.exp(-dsq / (2.0 * (high_radius / 2.0) ** 2))
                            grid[r][c] += high

        # Clamp to reasonable values so it doesn't get totally extreme
        for r in range(height):
            for c in range(width):
                grid[r][c] = max(0.05, min(1.5, grid[r][c]))

        # 2 passes of 3x3 box blur
        for _ in range(2):
            grid = self._box_blur(grid, height, width)

        # All outer edges are drains at 0.0.
        _stamp_dgov(grid, self.erodibility, height, width)
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
        """Advance one erosion step with session-age pacing and maturity tracking.

        Applies exponential decay to K and uplift based on session age,
        tracks mean |delta_z| in ring buffer for maturity metric.
        Prunes effects older than max_age.
        """
        # Increment tick counter and prune expired effects
        self._tick += 1
        self._active_effects = [
            e for e in self._active_effects if (self._tick - e.birth_tick) <= e.max_age
        ]
        # Enforce max buffer size (FIFO drop oldest on overflow)
        if len(self._active_effects) > 100:
            self._active_effects = self._active_effects[-100:]
        h = self.height
        rows = self.height_count
        cols = self.width

        # Compute session age and effective parameters
        age_factor: float = 1.0
        if self.session_start is not None:
            import time

            session_age_hours = (time.time() - self.session_start) / 3600.0
            age_factor = math.exp(-session_age_hours / self.TAU_HOURS)

        effective_K = self.K * (self.K_FLOOR_FRACTION + (1.0 - self.K_FLOOR_FRACTION) * age_factor)
        effective_uplift = self.uplift * (
            self.UPLIFT_FLOOR_FRACTION + (1.0 - self.UPLIFT_FLOOR_FRACTION) * age_factor
        )

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

        # 4. Compute Strahler-like stream order for river rendering
        # Process cells from HIGH to LOW elevation (headwaters at high elevations first)
        order = [[0] * cols for _ in range(rows)]
        for _, r, c in cells:  # Already sorted HIGH to LOW by construction
            if r in (0, rows - 1) or c in (0, cols - 1):
                continue
            # Count how many neighbors flow INTO this cell via receiver tuple comparison
            incoming_orders = []
            for k in range(8):
                nr = r + _D8[k][0]
                nc = c + _D8[k][1]
                if 0 <= nr < rows and 0 <= nc < cols:
                    # receiver[nr][nc] is a tuple (row, col) - check if flows into us
                    if receiver[nr][nc] == (r, c):
                        incoming_orders.append(order[nr][nc])

            if not incoming_orders:
                # Headwater: every cell with no incoming neighbors is a headwater
                order[r][c] = 1
            else:
                max_incoming = max(incoming_orders)
                count_at_max = incoming_orders.count(max_incoming)
                if count_at_max >= 2 and max_incoming > 0:
                    # Two or more streams of same max order -> increase order
                    order[r][c] = max_incoming + 1
                elif max_incoming > 0:
                    # Single stream carries its order forward
                    order[r][c] = max_incoming

        self.stream_order = order

        # 5. Capture pre-erosion height state for delta computation
        pre_step_height = [[h[r][c] for c in range(cols)] for r in range(rows)]

        # 6. Erosion: E = K * Erodibility * A^m * S^n, subtract, clamp at 0
        m, n = self.m, self.n
        for _, r, c in cells:
            if r in (0, rows - 1) or c in (0, cols - 1):
                continue  # skip drain row
            a = area[r][c]
            s = slope[r][c]
            if s > 0:
                erosion_mag = (effective_K * self.erodibility[r][c]) * (a**m) * (s**n)
                h[r][c] = max(0.0, h[r][c] - erosion_mag)

        # Update activity memory from erosion magnitude (after erosion loop)
        for _, r, c in cells:
            if r in (0, rows - 1) or c in (0, cols - 1):
                continue
            a = area[r][c]
            s = slope[r][c]
            if s > 0:
                erosion_mag = (effective_K * self.erodibility[r][c]) * (a**m) * (s**n)
                self._activity_memory[r][c] += erosion_mag * 2.0

        # 7. Uplift: add scaled constant to all non-drain cells
        if effective_uplift > 0:
            for r in range(1, max(rows - 1, 1)):
                row = h[r]
                for c in range(1, max(cols - 1, 1)):
                    row[c] += effective_uplift

        self._apply_boundary_drains(h)

        # 8. Compute mean |delta_z| and update ring buffer
        total_delta = 0.0
        for r in range(rows):
            for c in range(cols):
                total_delta += abs(h[r][c] - pre_step_height[r][c])
        mean_delta = total_delta / (rows * cols)

        # Update ring buffer with new mean delta
        self._ring_buffer.append(mean_delta)
        if len(self._ring_buffer) > self.RING_BUFFER_SIZE:
            self._ring_buffer.pop(0)

    def decay_activity_memory(self) -> None:
        """Apply one frame's worth of activity memory decay.

        Call once per render frame, NOT per erosion substep.
        """
        _MEMORY_DECAY = 0.97
        for r in range(self.height_count):
            for c in range(self.width):
                self._activity_memory[r][c] *= _MEMORY_DECAY
                if self._activity_memory[r][c] < 0.001:
                    self._activity_memory[r][c] = 0.0

    @property
    def maturity(self) -> float:
        """Return terrain maturity in [0, 1].

        0.0 = youthful (high change), 1.0 = settled (low change).
        Uses current mean |delta_z| normalized against reference_delta from early steps.
        """
        if not self._ring_buffer:
            return 0.0

        current_mean = sum(self._ring_buffer) / len(self._ring_buffer)
        # Clamp the ratio and invert: higher delta -> lower maturity
        ratio = current_mean / self.REFERENCE_DELTA
        normalized = max(0.0, min(1.0, ratio))
        return 1.0 - normalized

    @property
    def substeps(self) -> int:
        """Return recommended substeps per render based on session age.

        Early session (high activity): 2-3 substeps for faster visual response.
        Late session (settled): 1 substep as changes are minimal.
        """
        if self.session_start is None or not self._ring_buffer:
            return 1

        # Compute current age_factor
        import time

        session_age_hours = (time.time() - self.session_start) / 3600.0
        age_factor = math.exp(-session_age_hours / self.TAU_HOURS)

        # Map age_factor to substeps: ~2.5 at start, ~1 at end
        return max(1, round(2.5 * age_factor))

    def terrain_event(self, event_type: str, row: int, col: int, intensity: float = 1.0) -> None:
        """Apply a localized terrain perturbation around an interior cell.

        Perturbation intensity is damped by session phase when session_start is set.
        Events later in the day have less impact — the landscape becomes calmer.
        """
        if not (0 <= row < self.height_count and 0 <= col < self.width):
            raise ValueError(
                f"terrain_event position out of bounds: row={row}, col={col}, "
                f"size={self.height_count}x{self.width}"
            )

        # Apply perturbation damping based on session age
        if self.session_start is not None:
            import time

            age_hours = (time.time() - self.session_start) / 3600.0
            phase = _session_phase(age_hours)
            intensity = intensity * phase["perturbation_scale"]

        event_specs = {
            "uplift": (5, 0.08),
            "erode": (3, -0.06),
            "deposit": (3, 0.04),
            "tremor": (4, 0.02),
            "meteor": (6, -0.15),
            "volcano": (4, 0.12),
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

        # Boost activity memory at perturbation site
        for nr in range(row_min, row_max + 1):
            for nc in range(col_min, col_max + 1):
                dist_sq = float((nr - row) ** 2 + (nc - col) ** 2)
                if dist_sq > radius * radius:
                    continue
                weight = math.exp(-dist_sq / (2.0 * sigma_sq))
                self._activity_memory[nr][nc] += abs(base_amplitude) * intensity * weight * 3.0

        self._apply_boundary_drains(self.height)

        # Record the active effect for visual overlay
        max_age = 20  # ticks before removal
        effect = ActiveEffect(
            event_type=event_type,
            row=row,
            col=col,
            intensity=intensity,
            birth_tick=self._tick,
            max_age=max_age,
        )
        self._active_effects.append(effect)


class EffectBufferOverflowError(Exception):
    """Raised when the effect buffer exceeds its capacity."""

    pass


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
            "pane_circuit_breaker": ("volcano", 1.5),
            "mission_failed": ("meteor", 1.5),
            "dag_failed": ("meteor", 1.5),
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


def _elevation_color(elev: float, shade: float, phase: dict | None = None) -> tuple[int, int, int]:
    """Map elevation to RGB, modulated by hillshade for 3D effect.

    When phase is provided, applies session-based color adjustments:
        warmth: shifts temperature (warmth > 0.5 adds red/amber)
        saturation: scales deviation from grey
        contrast: scales deviation from midpoint (128)
    """
    s = 0.3 + 0.7 * shade
    if elev > 0.85:
        # Snow/peaks
        r, g, b = _clamp(int(230 * s)), _clamp(int(225 * s)), _clamp(int(215 * s))
    elif elev > 0.65:
        # Rocky/tan ridges
        r, g, b = _clamp(int(165 * s)), _clamp(int(130 * s)), _clamp(int(85 * s))
    elif elev > 0.40:
        # Green slopes
        r, g, b = _clamp(int(65 * s)), _clamp(int(140 * s)), _clamp(int(55 * s))
    else:
        # Dark green lowland
        r, g, b = _clamp(int(45 * s)), _clamp(int(100 * s)), _clamp(int(40 * s))

    if phase is not None:
        # Apply warmth shift (temperature)
        w = phase["warmth"]
        warm_shift = (w - 0.5) * 40  # -20 to +20 range
        r = _clamp(int(r + warm_shift))
        b = _clamp(int(b - warm_shift))

        # Apply saturation scaling from grey
        sat = phase["saturation"]
        grey = (r + g + b) // 3
        r = _clamp(int(grey + (r - grey) * sat))
        g = _clamp(int(grey + (g - grey) * sat))
        b = _clamp(int(grey + (b - grey) * sat))

        # Apply contrast scaling from midpoint
        con = phase["contrast"]
        r = _clamp(int(128 + (r - 128) * con))
        g = _clamp(int(128 + (g - 128) * con))
        b = _clamp(int(128 + (b - 128) * con))

    return (r, g, b)


def _river_color(
    flow: float, shade: float, phase: dict | None = None, order: int = 0
) -> tuple[int, int, int]:
    """Blue river, brighter with more flow and higher stream order.

    When phase is provided, applies session-based color adjustments same as elevation.
    Order parameter scales the blue channel intensity to distinguish stream hierarchy:
      - Order 0 (backward compat): original behavior
      - Order 1: faint tributaries (~70% intensity)
      - Order 2: medium streams (~90% intensity)
      - Order 3+: full brightness (100%)
    """
    s = 0.4 + 0.6 * shade
    intensity = min(1.0, 0.5 + 0.5 * math.log(max(flow, 1)) / 6.0)
    r, g, b = (
        _clamp(int(30 * s)),
        _clamp(int(80 * intensity * s)),
        _clamp(int(200 * intensity * s)),
    )

    # Apply stream order factor to blue channel primarily
    if order > 0:
        # order 1 = 0.7, order 2 = 0.9, order 3+ = 1.0
        order_factor = min(1.0, 0.5 + order * 0.2)
        b = _clamp(int(b * order_factor))

    if phase is not None:
        # Apply warmth shift (temperature)
        w = phase["warmth"]
        warm_shift = (w - 0.5) * 40  # -20 to +20 range
        r = _clamp(int(r + warm_shift))
        b = _clamp(int(b - warm_shift))

        # Apply saturation scaling from grey
        sat = phase["saturation"]
        grey = (r + g + b) // 3
        r = _clamp(int(grey + (r - grey) * sat))
        g = _clamp(int(grey + (g - grey) * sat))
        b = _clamp(int(grey + (b - grey) * sat))

        # Apply contrast scaling from midpoint
        con = phase["contrast"]
        r = _clamp(int(128 + (r - 128) * con))
        g = _clamp(int(128 + (g - 128) * con))
        b = _clamp(int(128 + (b - 128) * con))

    return (r, g, b)


def render_terrain(model: ErosionModel, supersample: int = 1) -> Text:
    """Render 3D hillshaded terrain with half-block chars and river overlay."""
    h = model.height
    rows = model.height_count
    cols = model.width
    area = model.area
    stream_order = model.stream_order

    display_rows = rows // supersample
    display_cols = cols // supersample

    river_thresh = max(display_cols * 0.4, 20.0) * (supersample**2)

    # Compute session phase for visual adjustments
    phase: dict | None = None
    if model.session_start is not None:
        import time

        age_hours = (time.time() - model.session_start) / 3600.0
        phase = _session_phase(age_hours)

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
            cell_order = stream_order[dr][dc]
        else:
            te = ts = tf = 0.0
            n = supersample * supersample
            max_order = 0
            for sr in range(supersample):
                for sc in range(supersample):
                    r2 = dr * supersample + sr
                    c2 = dc * supersample + sc
                    te += h[r2][c2]
                    ts += shade[r2][c2]
                    tf += area[r2][c2]
                    if stream_order[r2][c2] > max_order:
                        max_order = stream_order[r2][c2]
            elev, s, flow = te / n, ts / n, tf / n
            cell_order = max_order

        # Check activity memory for this cell
        if supersample == 1:
            heat = model._activity_memory[dr][dc] if model._activity_memory else 0.0
        else:
            heat = 0.0
            for sr in range(supersample):
                for sc in range(supersample):
                    r2 = dr * supersample + sr
                    c2 = dc * supersample + sc
                    heat += model._activity_memory[r2][c2]
            heat /= supersample * supersample

        # Use stream order as primary river detection signal
        if cell_order >= 2 and elev < 0.80:
            base_color = _river_color(flow, s, phase=phase, order=cell_order)
        elif flow > river_thresh and elev < 0.80:
            base_color = _river_color(flow, s, phase=phase)
        else:
            base_color = _elevation_color(elev, s, phase=phase)

        # Blend activity heat into color (warm amber tint for recent activity)
        r_val, g_val, b_val = base_color
        heat_clamped = min(5.0, heat)  # clamp to prevent blowout
        if heat_clamped > 0.01:
            heat_weight = min(1.0, heat_clamped / 5.0)
            r_out = _clamp(int(r_val + heat_weight * 60))
            g_out = _clamp(int(g_val + heat_weight * 20))
            b_out = _clamp(int(b_val - heat_weight * 30))
            return (r_out, g_out, b_out)

        return base_color

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

            # Boundary clamp (stay off edges)
            r = max(2.0, min(rows - 2.0, r))
            c = max(3.0, min(cols - 3.0, c))

            self._pos[slug] = [r, c]
            self._vel[slug] = [vr, vc]

            # Render agent as a single character
            ir, ic = int(round(r)), int(round(c))
            if ir >= 0 and ir < rows and ic >= 0 and ic < cols:
                if role == "lt-gov":
                    char, color = "★", "bold yellow"
                elif state in ("done", "merged"):
                    char, color = "●", "dim white"
                elif state == "failed":
                    char, color = "✖", "bold red"
                else:
                    char, color = "@", "bold green"
                stamps[(ir, ic)] = (char, color)

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


def render_effect_stamps(
    model: ErosionModel,
    display_rows: int,
    display_cols: int,
    supersample: int = 1,
) -> dict[tuple[int, int], tuple[str, str]]:
    """Render transient effect glyphs with fade and splash radius.

    For each active effect:
    - Map grid coords to display coords (divide by supersample)
    - Compute fade alpha. Skip if < 0.15
    - Alpha > 0.7: full intensity glyph (bold bright style)
    - Alpha 0.4-0.7: dimmed glyph (muted color, no bold/bright)
    - Alpha 0.15-0.4: ghost dot "·" with faint color
    - Alpha < 0.15: invisible

    High-intensity events (intensity > 1.0) get a small splash radius when alpha > 0.5:
    glyph at center, "·" (middle dot) at 4 cardinal neighbors if within bounds.

    Returns dict in same format as AgentSim.update() stamps.
    """
    stamps: dict[tuple[int, int], tuple[str, str]] = {}

    for effect in model._active_effects:
        # Compute fade alpha
        age = model._tick - effect.birth_tick
        alpha = 1.0 - (age / float(effect.max_age))

        if alpha < 0.1:
            continue

        # Map grid coords to display coords
        dr = effect.row // supersample
        dc = effect.col // supersample

        # Skip if outside display bounds
        if dr >= display_rows or dc >= display_cols:
            continue

        # Get glyph and base style for this event type
        if effect.event_type not in _EFFECT_GLYPHS:
            continue
        glyph, base_style = _EFFECT_GLYPHS[effect.event_type]

        # Determine visibility based on alpha (3 visual stages)
        if alpha > 0.7:
            # Full intensity — bold glyph
            visible_glyph = glyph
            visible_style = base_style
        elif alpha > 0.4:
            # Dimming — same glyph, muted color
            visible_glyph = glyph
            visible_style = base_style.replace("bold ", "").replace("bright_", "")
        elif alpha > 0.15:
            # Ghost — small dot with faint color
            visible_glyph = "·"
            visible_style = f"dim {base_style.replace('bold ', '').replace('bright_', '')}"
        else:
            continue  # invisible

        # Don't overwrite existing stamps (agent stamps render on top)
        if (dr, dc) not in stamps:
            stamps[(dr, dc)] = (visible_glyph, visible_style)

        # Splash radius for high-intensity events (> 1.0) - only when alpha > 0.5
        if effect.intensity > 1.0 and alpha > 0.5:
            cardinal_offsets = [(-1, 0), (1, 0), (0, -1), (0, 1)]
            for dr_off, dc_off in cardinal_offsets:
                nr, nc = dr + dr_off, dc + dc_off
                if 0 <= nr < display_rows and 0 <= nc < display_cols:
                    if (nr, nc) not in stamps:
                        stamps[(nr, nc)] = ("·", visible_style)

    return stamps
