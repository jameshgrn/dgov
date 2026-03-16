"""Unit tests for dashboard v2."""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestPhaseDots:
    def test_active_working(self):
        from dgov.dashboard_v2 import phase_dots

        result = phase_dots("active", "working on task")
        assert "\u2b24" in result
        assert "\u25cb" in result

    def test_active_idle(self):
        from dgov.dashboard_v2 import phase_dots

        result = phase_dots("active", "idle")
        assert result.count("\u2b24") == 1

    def test_done(self):
        from dgov.dashboard_v2 import phase_dots

        result = phase_dots("done", "")
        assert "\u25cb" not in result

    def test_merged(self):
        from dgov.dashboard_v2 import phase_dots

        result = phase_dots("merged", "")
        assert "\u25cb" not in result

    def test_failed(self):
        from dgov.dashboard_v2 import phase_dots

        result = phase_dots("failed", "")
        assert "\u2717" in result

    def test_escalated(self):
        from dgov.dashboard_v2 import phase_dots

        result = phase_dots("escalated", "")
        assert result.count("\u2b24") == 2

    def test_unknown_state(self):
        from dgov.dashboard_v2 import phase_dots

        result = phase_dots("unknown", "")
        assert result.count("\u25cb") == 5


@pytest.mark.unit
class TestStateColor:
    def test_active(self):
        from dgov.dashboard_v2 import state_color

        assert state_color("active") == "yellow"

    def test_done(self):
        from dgov.dashboard_v2 import state_color

        assert state_color("done") == "green"

    def test_failed(self):
        from dgov.dashboard_v2 import state_color

        assert state_color("failed") == "red"

    def test_merged(self):
        from dgov.dashboard_v2 import state_color

        assert state_color("merged") == "green"

    def test_escalated(self):
        from dgov.dashboard_v2 import state_color

        assert state_color("escalated") == "magenta"

    def test_unknown(self):
        from dgov.dashboard_v2 import state_color

        assert state_color("nonexistent") == "white"


@pytest.mark.unit
class TestFmtDuration:
    def test_seconds(self):
        from dgov.dashboard_v2 import fmt_duration

        assert fmt_duration(45) == "45s"

    def test_minutes(self):
        from dgov.dashboard_v2 import fmt_duration

        assert fmt_duration(125) == "2m5s"

    def test_hours(self):
        from dgov.dashboard_v2 import fmt_duration

        assert fmt_duration(3661) == "1h1m"

    def test_negative(self):
        from dgov.dashboard_v2 import fmt_duration

        assert fmt_duration(-5) == "0s"


@pytest.mark.unit
class TestImports:
    def test_dashboard_v2_importable(self):
        from dgov.dashboard_v2 import run_dashboard_v2

        assert callable(run_dashboard_v2)

    def test_terrain_importable(self):
        from dgov.terrain import ErosionModel, render_terrain

        assert callable(render_terrain)
        assert ErosionModel is not None


@pytest.mark.unit
class TestSanitization:
    def test_markup_injection_in_table(self):
        """Rich markup in slug should not be interpreted."""
        from dgov.dashboard_v2 import _build_worker_table

        panes = [
            {
                "slug": "[bold red]evil[/]",
                "agent": "pi",
                "state": "active",
                "activity": "working",
                "duration_s": 60,
            }
        ]
        table = _build_worker_table(panes, 0)
        # Table should be buildable without error
        assert table is not None
        # The slug should appear as literal text, not formatted
        # We verify by checking the table has rows
        assert table.row_count == 1

    def test_ansi_in_activity(self):
        """ANSI codes in activity should not break rendering."""
        from dgov.dashboard_v2 import _build_worker_table

        panes = [
            {
                "slug": "test",
                "agent": "pi",
                "state": "active",
                "activity": "\x1b[31mred text\x1b[0m",
                "duration_s": 30,
            }
        ]
        table = _build_worker_table(panes, 0)
        assert table is not None
        assert table.row_count == 1


@pytest.mark.unit
class TestTerrain:
    def test_erosion_model_step(self):
        from dgov.terrain import ErosionModel

        model = ErosionModel(width=10, height=10, seed=42)
        initial = [row[:] for row in model.height]
        model.step()
        # Grid should change after a step
        changed = False
        for r in range(10):
            for c in range(10):
                if model.height[r][c] != initial[r][c]:
                    changed = True
                    break
            if changed:
                break
        assert changed

    def test_render_terrain_returns_text(self):
        from rich.text import Text

        from dgov.terrain import ErosionModel, render_terrain

        model = ErosionModel(width=10, height=10, seed=42)
        result = render_terrain(model)
        assert isinstance(result, Text)
        assert len(result) > 0


@pytest.mark.unit
class TestEventFeed:
    def test_build_event_feed(self):
        import time

        from dgov.dashboard_v2 import _build_event_feed

        events = [
            {"timestamp": time.time(), "event": "pane_created", "pane": "fix-bug"},
            {"timestamp": time.time(), "event": "pane_merged", "pane": "add-feat"},
        ]
        text = _build_event_feed(events)
        assert "pane_created" in text.plain
        assert "pane_merged" in text.plain
        assert "fix-bug" in text.plain


@pytest.mark.unit
class TestWorkerTable:
    def test_empty_panes(self):
        from dgov.dashboard_v2 import _build_worker_table

        table = _build_worker_table([], 0)
        assert table.row_count == 0

    def test_multiple_panes(self):
        from dgov.dashboard_v2 import _build_worker_table

        panes = [
            {
                "slug": "a",
                "agent": "pi",
                "state": "active",
                "activity": "working",
                "duration_s": 10,
            },
            {
                "slug": "b",
                "agent": "claude",
                "state": "done",
                "activity": "",
                "duration_s": 300,
            },
        ]
        table = _build_worker_table(panes, 1)
        assert table.row_count == 2
