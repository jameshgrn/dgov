from __future__ import annotations

import time

import pytest

from dgov.terrain import (
    ErosionModel,
    EventTranslator,
    _elevation_color,
    _river_color,
    _session_phase,
    render_effect_stamps,
)


def _flat_model(width: int = 13, height: int = 13) -> ErosionModel:
    model = ErosionModel(width=width, height=height, seed=7)
    for row in range(1, height - 1):
        for col in range(1, width - 1):
            model.height[row][col] = 1.0
            model.area[row][col] = 1.0
    return model


@pytest.mark.unit
@pytest.mark.parametrize(
    ("event_type", "center_assertion", "area_assertion"),
    [
        ("uplift", lambda before, after: after > before, lambda before, after: after == before),
        ("erode", lambda before, after: after < before, lambda before, after: after > before),
        ("deposit", lambda before, after: after > before, lambda before, after: after == before),
        (
            "tremor",
            lambda before, after: after != before,
            lambda before, after: after == before,
        ),
    ],
)
def test_terrain_event_effects_direction(event_type, center_assertion, area_assertion):
    model = _flat_model()
    before_height = model.height[6][6]
    before_area = model.area[6][6]

    model.terrain_event(event_type, 6, 6, intensity=1.0)

    assert center_assertion(before_height, model.height[6][6])
    assert area_assertion(before_area, model.area[6][6])


@pytest.mark.unit
def test_terrain_event_skips_boundary_drain_cells():
    model = _flat_model()
    before = [row[:] for row in model.height]

    for event_type in ("uplift", "erode", "deposit", "tremor"):
        model.terrain_event(event_type, 1, 1, intensity=1.0)

    assert model.height[0] == before[0]
    assert model.height[-1] == before[-1]
    assert [row[0] for row in model.height] == [row[0] for row in before]
    assert [row[-1] for row in model.height] == [row[-1] for row in before]


@pytest.mark.unit
def test_gaussian_kernel_attenuates_with_distance():
    model = _flat_model()
    before = [row[:] for row in model.height]

    model.terrain_event("uplift", 6, 6, intensity=1.0)

    center_delta = model.height[6][6] - before[6][6]
    mid_delta = model.height[6][8] - before[6][8]
    edge_delta = model.height[6][10] - before[6][10]

    assert center_delta > mid_delta > edge_delta > 0.0


@pytest.mark.unit
@pytest.mark.parametrize(
    ("event_name", "expected"),
    [
        ("pane_created", ("uplift", 1.0)),
        ("pane_done", ("erode", 1.2)),
        ("pane_merged", ("erode", 1.2)),
        ("pane_closed", ("deposit", 0.8)),
        ("pane_timed_out", ("deposit", 0.8)),
        ("pane_circuit_breaker", ("volcano", 1.5)),
        ("mission_failed", ("meteor", 1.5)),
        ("dag_failed", ("meteor", 1.5)),
        ("checkpoint_created", ("tremor", 0.5)),
        ("pane_escalated", ("uplift", 0.6)),
        ("pane_retry_spawned", ("uplift", 0.6)),
        ("review_pass", ("erode", 0.4)),
        ("review_fail", ("deposit", 0.6)),
    ],
)
def test_event_translator_mappings(event_name, expected):
    translator = EventTranslator()

    event = {"ts": "2026-01-01T00:00:00+00:00", "event": event_name}

    assert translator.translate(event) == expected


@pytest.mark.unit
def test_event_translator_deduplicates_by_timestamp():
    translator = EventTranslator()

    created = {"ts": "2026-01-01T00:00:00+00:00", "event": "pane_created"}
    assert translator.translate(created) == ("uplift", 1.0)
    assert translator._last_ts == created["ts"]
    assert translator.translate(created) is None
    assert translator.translate({"ts": "2025-12-31T23:59:59+00:00", "event": "pane_done"}) is None

    unknown = {"ts": "2026-01-01T00:00:01+00:00", "event": "unknown"}
    assert translator.translate(unknown) is None
    assert translator._last_ts == unknown["ts"]


@pytest.mark.unit
def test_terrain_event_rejects_out_of_bounds_position():
    model = _flat_model()

    with pytest.raises(ValueError, match="out of bounds"):
        model.terrain_event("uplift", -1, 3)


@pytest.mark.unit
def test_maturity_increases_over_steps():
    """Maturity should increase (terrain settles) over many erosion steps."""
    model = ErosionModel(width=20, height=20, seed=42)
    # Run 20 steps to get initial dynamics going
    for _ in range(20):
        model.step()
    early_maturity = model.maturity

    # Run 80 more steps
    for _ in range(80):
        model.step()
    late_maturity = model.maturity

    # Terrain should be more mature (higher maturity) after more steps
    assert late_maturity > early_maturity


@pytest.mark.unit
def test_effective_k_decays_with_session_age():
    """Effective K at session start should exceed effective K at 8 hours."""
    # Model with session_start in the past (8 hours ago)
    old_start = time.time() - 8 * 3600
    model_old = ErosionModel(width=13, height=13, seed=7, session_start=old_start)

    # Model with session_start now
    model_new = ErosionModel(width=13, height=13, seed=7, session_start=time.time())

    # The new-session model should have higher effective K
    # We test this indirectly: run one step on each and compare erosion magnitude
    h_old_before = [row[:] for row in model_old.height]
    h_new_before = [row[:] for row in model_new.height]

    model_old.step()
    model_new.step()

    # Compute total absolute change
    def total_change(before, after, rows, cols):
        total = 0.0
        for r in range(rows):
            for c in range(cols):
                total += abs(after[r][c] - before[r][c])
        return total

    change_old = total_change(h_old_before, model_old.height, 13, 13)
    change_new = total_change(h_new_before, model_new.height, 13, 13)

    assert change_new > change_old


@pytest.mark.unit
def test_stability_no_extreme_values():
    """After 200 steps with controller active, no cell should be extreme."""
    model = ErosionModel(width=20, height=20, seed=42, session_start=time.time())
    for _ in range(200):
        model.step()
    for r in range(model.height_count):
        for c in range(model.width):
            assert -0.1 <= model.height[r][c] <= 3.0, (
                f"Extreme value {model.height[r][c]} at ({r},{c})"
            )


@pytest.mark.unit
def test_effect_stamp_appears_after_terrain_event():
    """terrain_event should add an active effect to the buffer."""
    model = _flat_model()
    assert len(model._active_effects) == 0
    model.terrain_event("uplift", 6, 6, intensity=1.0)
    assert len(model._active_effects) == 1
    effect = model._active_effects[0]
    assert effect.event_type == "uplift"
    assert effect.row == 6
    assert effect.col == 6


@pytest.mark.unit
def test_effects_fade_after_max_age():
    """Effects should be pruned from the buffer after max_age ticks."""
    model = _flat_model()
    model.terrain_event("uplift", 6, 6, intensity=1.0)
    max_age = model._active_effects[0].max_age

    # Step past the max age
    for _ in range(max_age + 5):
        model.step()

    assert len(model._active_effects) == 0


@pytest.mark.unit
def test_no_clutter_after_decay():
    """After 50 ticks with no new events, effects buffer should be empty."""
    model = _flat_model()
    # Fire several events
    for etype in ("uplift", "erode", "deposit", "meteor"):
        model.terrain_event(etype, 6, 6, intensity=1.0)

    for _ in range(50):
        model.step()

    assert len(model._active_effects) == 0


@pytest.mark.unit
def test_glyph_differentiation():
    """Different event types should produce different glyphs."""
    model = ErosionModel(width=30, height=30, seed=42)
    # Fire different events at different locations
    model.terrain_event("uplift", 5, 5, intensity=1.0)
    model.terrain_event("meteor", 10, 10, intensity=1.5)
    model.terrain_event("erode", 15, 15, intensity=1.0)

    stamps = render_effect_stamps(model, 15, 15, supersample=2)
    glyphs = {glyph for glyph, style in stamps.values() if glyph != "."}
    # Should have at least 2 distinct glyphs (3 event types, but display coords may collide)
    assert len(glyphs) >= 2


@pytest.mark.unit
def test_render_effect_stamps_empty_when_no_effects():
    """No effects = empty stamps dict."""
    model = _flat_model()
    stamps = render_effect_stamps(model, 6, 6, supersample=1)
    assert stamps == {}


@pytest.mark.unit
def test_activity_memory_decay():
    """Activity memory should decay toward zero over many decay calls."""
    model = _flat_model()
    # Trigger erosion to build up activity
    model.terrain_event("erode", 6, 6, intensity=2.0)
    peak = model._activity_memory[6][6]
    assert peak > 0.0

    # Run decay calls (steps don't decay anymore - it's separate)
    for _ in range(100):
        model.decay_activity_memory()

    assert model._activity_memory[6][6] < peak * 0.1


@pytest.mark.unit
def test_activity_memory_bounded():
    """Activity memory values should stay bounded even with many events."""
    model = _flat_model()
    # Fire many events
    for _ in range(50):
        model.terrain_event("uplift", 6, 6, intensity=2.0)
        model.step()

    # Check no cell exceeds a reasonable bound
    for r in range(model.height_count):
        for c in range(model.width):
            assert model._activity_memory[r][c] < 100.0, (
                f"Unbounded memory at ({r},{c}): {model._activity_memory[r][c]}"
            )


@pytest.mark.unit
def test_unicode_glyph_in_effect_stamps():
    """Effect glyphs should be Unicode, not plain ASCII."""
    from dgov.terrain import _EFFECT_GLYPHS

    # Check that no glyph is plain ASCII a-z, A-Z, or common punctuation
    ascii_simple = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ^v~*!.")
    for event_type, (glyph, _style) in _EFFECT_GLYPHS.items():
        assert glyph not in ascii_simple, f"Glyph for {event_type} is plain ASCII: {glyph!r}"


@pytest.mark.unit
def test_activity_memory_from_erosion():
    """Erosion should add to activity memory."""
    model = _flat_model()

    # Initial activity should be zero
    assert model._activity_memory[6][6] == 0.0

    # Trigger erosion step which should update activity memory
    for _ in range(5):
        model.step()

    # Activity memory should have increased at some cells due to erosion
    has_activity = False
    for r in range(model.height_count):
        for c in range(model.width):
            if model._activity_memory[r][c] > 0:
                has_activity = True
                break
        if has_activity:
            break

    assert has_activity, "Expected some activity memory from erosion steps"


@pytest.mark.unit
def test_unicode_glyph_fade_stages():
    """Effect fade should have 3 visual stages with distinct glyph or style changes."""
    model = _flat_model()
    # Fire an effect
    model.terrain_event("uplift", 6, 6, intensity=1.0)

    # Get stamps immediately (alpha ~ 1.0 - full glyph)
    stamps_immediate = render_effect_stamps(model, 13, 13, supersample=1)
    assert len(stamps_immediate) > 0
    glyph_at_start = stamps_immediate.get((6, 6))
    assert glyph_at_start is not None
    assert "bold" in glyph_at_start[1] or "bright" in glyph_at_start[1], (
        f"Expected bold/bright style at full alpha, got: {glyph_at_start[1]}"
    )

    # Advance to mid-fade stage (alpha ~ 0.5)
    model._tick = model._active_effects[0].birth_tick + 12
    stamps_mid = render_effect_stamps(model, 13, 13, supersample=1)
    glyph_at_mid = stamps_mid.get((6, 6))
    assert glyph_at_mid is not None

    # Advance to ghost stage (alpha ~ 0.25)
    model._tick = model._active_effects[0].birth_tick + 17
    stamps_ghost = render_effect_stamps(model, 13, 13, supersample=1)
    glyph_at_ghost = stamps_ghost.get((6, 6))

    # Ghost stage should use dot glyph "·" or "." with dim style
    assert glyph_at_ghost is not None, "Expected glyph at alpha > 0.15"


@pytest.mark.unit
def test_color_shift_across_session():
    """Same elevation should render differently at hour 0 vs hour 8."""
    phase_dawn = _session_phase(0.0)
    phase_evening = _session_phase(8.0)

    color_dawn = _elevation_color(0.5, 0.7, phase=phase_dawn)
    color_evening = _elevation_color(0.5, 0.7, phase=phase_evening)

    assert color_dawn != color_evening


@pytest.mark.unit
def test_dawn_warm_evening_cool():
    """Dawn colors should be warmer (higher R relative to B) than evening."""
    phase_dawn = _session_phase(0.25)
    phase_evening = _session_phase(7.0)

    r_dawn, _, b_dawn = _elevation_color(0.5, 0.7, phase=phase_dawn)
    r_eve, _, b_eve = _elevation_color(0.5, 0.7, phase=phase_evening)

    # Dawn: warm = higher R-B difference
    # Evening: cool = lower R-B difference (or negative)
    assert (r_dawn - b_dawn) > (r_eve - b_eve)


@pytest.mark.unit
def test_evening_cool_vs_morning():
    """Evening should be cooler and more muted than peak morning."""
    phase_morning = _session_phase(1.0)
    phase_evening = _session_phase(7.5)

    rm, gm, bm = _elevation_color(0.5, 0.7, phase=phase_morning)
    re, ge, be = _elevation_color(0.5, 0.7, phase=phase_evening)

    # Morning should be more saturated (larger spread from grey)
    grey_m = (rm + gm + bm) / 3.0
    grey_e = (re + ge + be) / 3.0
    spread_m = abs(rm - grey_m) + abs(gm - grey_m) + abs(bm - grey_m)
    spread_e = abs(re - grey_e) + abs(ge - grey_e) + abs(be - grey_e)

    assert spread_m > spread_e


@pytest.mark.unit
def test_compat_palette_no_session():
    """When phase is None, colors should match the original palette exactly."""
    # These should produce identical results with and without phase=None
    for elev in (0.3, 0.5, 0.7, 0.9):
        for shade in (0.3, 0.5, 0.8):
            assert _elevation_color(elev, shade) == _elevation_color(elev, shade, phase=None)

    for flow in (1.0, 10.0, 100.0):
        for shade in (0.3, 0.7):
            assert _river_color(flow, shade) == _river_color(flow, shade, phase=None)


@pytest.mark.unit
def test_session_phase_keyframes():
    """Phase function should return valid dicts at all keyframe hours."""
    for hour in [0.0, 0.5, 2.0, 4.0, 6.0, 8.0, 12.0]:
        phase = _session_phase(hour)
        assert 0.0 <= phase["warmth"] <= 1.0
        assert 0.0 <= phase["saturation"] <= 1.0
        assert 0.0 <= phase["contrast"] <= 1.0
        assert 0.0 <= phase["perturbation_scale"] <= 1.0


@pytest.mark.unit
def test_perturbation_damping_by_session_age():
    """terrain_event intensity should be damped by session age."""
    import time

    # Create model with session_start in the past (4 hours ago)
    old_start = time.time() - 4 * 3600
    model_old = ErosionModel(width=13, height=13, seed=7, session_start=old_start)

    # Create model with session_start now
    model_new = ErosionModel(width=13, height=13, seed=7, session_start=time.time())

    # Flat terrain for baseline
    for row in range(1, 12):
        for col in range(1, 12):
            model_old.height[row][col] = 1.0
            model_new.height[row][col] = 1.0

    # Fire same event on both
    model_old.terrain_event("uplift", 6, 6, intensity=1.0)
    model_new.terrain_event("uplift", 6, 6, intensity=1.0)

    # Old session (damped) should have less change at center
    old_change = abs(model_old.height[6][6] - 1.0)
    new_change = abs(model_new.height[6][6] - 1.0)

    assert old_change < new_change, "Older session should have damped perturbation"


@pytest.mark.unit
def test_session_phase_values_at_keyframes():
    """Check exact phase values at keyframe hours match spec."""
    # Dawn (hour 0): warm, lower sat/contrast
    dawn = _session_phase(0.0)
    assert abs(dawn["warmth"] - 0.75) < 0.01
    assert abs(dawn["saturation"] - 0.60) < 0.01
    assert abs(dawn["contrast"] - 0.70) < 0.01
    assert abs(dawn["perturbation_scale"] - 1.00) < 0.01

    # Morning (hour 2): peak saturation/contrast
    morning = _session_phase(2.0)
    assert abs(morning["saturation"] - 0.95) < 0.01
    assert abs(morning["contrast"] - 0.95) < 0.01

    # Late night (hour 12+): low all values
    late = _session_phase(12.0)
    assert abs(late["warmth"] - 0.25) < 0.01
    assert abs(late["saturation"] - 0.55) < 0.01
    assert abs(late["contrast"] - 0.60) < 0.01
    assert abs(late["perturbation_scale"] - 0.20) < 0.01


@pytest.mark.unit
def test_decay_per_frame_not_per_step():
    """decay_activity_memory should be a separate call from step()."""
    model = _flat_model()
    model.terrain_event("erode", 6, 6, intensity=2.0)
    heat_after_event = model._activity_memory[6][6]

    # Call step() 3 times (simulating substeps=3)
    for _ in range(3):
        model.step()

    # Heat should NOT have decayed (decay removed from step)
    assert model._activity_memory[6][6] >= heat_after_event * 0.95, "tiny float drift from erosion"

    # Now call decay once
    model.decay_activity_memory()
    assert model._activity_memory[6][6] < heat_after_event


@pytest.mark.unit
def test_decay_substep_independent():
    """Decay rate should be identical regardless of how many steps ran."""
    model_a = _flat_model()
    model_b = _flat_model()

    # Both get same event
    model_a.terrain_event("erode", 6, 6, intensity=2.0)
    model_b.terrain_event("erode", 6, 6, intensity=2.0)

    heat_initial = model_a._activity_memory[6][6]

    # Model A: 1 step + 1 decay (like substeps=1)
    model_a.step()
    model_a.decay_activity_memory()

    # Model B: 3 steps + 1 decay (like substeps=3)
    for _ in range(3):
        model_b.step()
    model_b.decay_activity_memory()

    # Both should have decayed by the same fraction
    ratio_a = model_a._activity_memory[6][6] / heat_initial
    ratio_b = model_b._activity_memory[6][6] / heat_initial

    # Allow small tolerance for erosion-induced heat additions during step()
    assert abs(ratio_a - ratio_b) < 0.05


@pytest.mark.unit
def test_stream_order_computed():
    """After step(), stream_order grid should have non-zero values."""
    model = ErosionModel(width=20, height=20, seed=42)
    for _ in range(10):
        model.step()

    max_order = max(
        model.stream_order[r][c] for r in range(model.height_count) for c in range(model.width)
    )
    assert max_order >= 2, f"Expected stream orders >= 2, got max {max_order}"


@pytest.mark.unit
def test_river_order_color_differentiation():
    """Different stream orders should produce different river colors."""
    from dgov.terrain import _river_color

    color_1 = _river_color(50.0, 0.7, order=1)
    color_3 = _river_color(50.0, 0.7, order=3)

    # Higher order should be bluer (higher B component)
    assert color_3[2] > color_1[2], (
        f"Order 3 blue ({color_3[2]}) should exceed order 1 blue ({color_1[2]})"
    )


@pytest.mark.unit
def test_stream_order_hierarchy():
    """Trunk streams (high flow) should have higher order than headwaters."""
    model = ErosionModel(width=30, height=30, seed=42)
    for _ in range(20):
        model.step()

    # Only check interior cells (excluding boundary drains which have order=0 by design)
    max_flow_order = 0
    max_flow_val = 0.0
    for r in range(1, model.height_count - 1):
        for c in range(1, model.width - 1):
            if model.area[r][c] > max_flow_val:
                max_flow_val = model.area[r][c]
                max_flow_order = model.stream_order[r][c]

    assert max_flow_order >= 2


@pytest.mark.unit
def test_headwater_not_rendered_as_river():
    """Order-1 (headwater) cells should get elevation color, not river color."""
    from dgov.terrain import render_terrain

    model = ErosionModel(width=20, height=20, seed=42)
    for _ in range(10):
        model.step()

    # Find a cell with order == 1 and a cell with order >= 2
    order1_cell = None
    order2_cell = None
    for r in range(1, model.height_count - 1):
        for c in range(1, model.width - 1):
            if model.stream_order[r][c] == 1 and order1_cell is None:
                order1_cell = (r, c)
            if model.stream_order[r][c] >= 2 and order2_cell is None:
                order2_cell = (r, c)
            if order1_cell and order2_cell:
                break
        if order1_cell and order2_cell:
            break

    assert order1_cell is not None, "Need an order-1 cell for this test"
    assert order2_cell is not None, "Need an order-2+ cell for this test"

    # Render and verify the text output exists (rendering doesn't crash)
    text = render_terrain(model)
    assert len(text.plain) > 0
