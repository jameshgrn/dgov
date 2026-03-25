"""Performance benchmarks for terrain step and render."""

from __future__ import annotations

import time

import pytest

from dgov.terrain import ErosionModel, render_terrain


def _typical_model() -> ErosionModel:
    """Create a model matching typical terminal dimensions (78x22 display, supersample=2)."""
    # 78 cols * 2 = 156 width, 22 rows * 2 = 44 height
    return ErosionModel(width=156, height=44, seed=42, session_start=time.time())


@pytest.mark.unit
def test_step_performance():
    """Single step() should complete within 100ms for typical terminal size.

    At 2 FPS with 2 substeps, step() runs 4x/sec. 100ms budget per step
    leaves headroom for render + overhead within a 500ms frame.
    """
    model = _typical_model()
    # Warm up (first steps build drainage network)
    for _ in range(5):
        model.step()

    # Measure 10 steps
    start = time.perf_counter()
    for _ in range(10):
        model.step()
    elapsed = time.perf_counter() - start

    avg_ms = (elapsed / 10) * 1000
    # Report timing for visibility
    print(f"step() avg: {avg_ms:.1f} ms (budget: 100ms)")
    assert avg_ms < 100, f"step() too slow: {avg_ms:.1f}ms > 100ms budget"


@pytest.mark.unit
def test_render_performance():
    """render_terrain() with supersample=2 should complete within 200ms.

    At 2 FPS (500ms frame), render is the other major cost alongside step().
    200ms budget leaves 300ms for 2 substeps + overhead.
    """
    model = _typical_model()
    for _ in range(10):
        model.step()

    # Measure 5 renders
    start = time.perf_counter()
    for _ in range(5):
        render_terrain(model, supersample=2)
    elapsed = time.perf_counter() - start

    avg_ms = (elapsed / 5) * 1000
    print(f"render_terrain(ss=2) avg: {avg_ms:.1f} ms (budget: 200ms)")
    assert avg_ms < 200, f"render_terrain() too slow: {avg_ms:.1f}ms > 200ms budget"


@pytest.mark.unit
def test_full_frame_budget():
    """A complete frame (2 substeps + decay + render) should complete within 500ms.

    This is the actual frame budget at 2 FPS.
    """
    model = _typical_model()
    for _ in range(10):
        model.step()

    start = time.perf_counter()
    # Simulate one frame: 2 substeps + decay + render
    for _ in range(2):
        model.step()
    model.decay_activity_memory()
    render_terrain(model, supersample=2)
    elapsed = time.perf_counter() - start

    frame_ms = elapsed * 1000
    print(f"full frame: {frame_ms:.1f} ms (budget: 500ms)")
    assert frame_ms < 500, f"Full frame too slow: {frame_ms:.1f}ms > 500ms budget"


@pytest.mark.unit
def test_decay_activity_memory_performance():
    """decay_activity_memory() should be trivial (< 5ms)."""
    model = _typical_model()
    for _ in range(10):
        model.step()

    start = time.perf_counter()
    for _ in range(100):
        model.decay_activity_memory()
    elapsed = time.perf_counter() - start

    avg_ms = (elapsed / 100) * 1000
    print(f"decay_activity_memory() avg: {avg_ms:.2f} ms (budget: 5ms)")
    assert avg_ms < 5, f"decay too slow: {avg_ms:.2f}ms > 5ms budget"
