from __future__ import annotations

import time

import pytest

from dgov.terrain import ErosionModel, EventTranslator, render_effect_stamps


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
