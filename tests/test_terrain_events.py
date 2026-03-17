from __future__ import annotations

import pytest

from dgov.terrain import ErosionModel, EventTranslator


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
        ("pane_circuit_breaker", ("deposit", 1.5)),
        ("mission_failed", ("deposit", 1.5)),
        ("dag_failed", ("deposit", 1.5)),
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
