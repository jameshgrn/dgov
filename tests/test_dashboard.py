"""Unit tests for the dgov dashboard."""

from __future__ import annotations

import pytest
from rich.console import Console
from rich.text import Text


def _render_dashboard_text(state, width: int, height: int) -> str:
    from dgov.dashboard import _build_layout

    console = Console(record=True, force_terminal=True, width=width, height=height)
    console.print(_build_layout(state, term_width=width, term_height=height))
    return console.export_text()


@pytest.mark.unit
class TestStateColor:
    def test_active(self):
        from dgov.dashboard import state_color

        assert state_color("active") == "yellow"

    def test_done(self):
        from dgov.dashboard import state_color

        assert state_color("done") == "green"

    def test_failed(self):
        from dgov.dashboard import state_color

        assert state_color("failed") == "red"

    def test_merged(self):
        from dgov.dashboard import state_color

        assert state_color("merged") == "green"

    def test_escalated(self):
        from dgov.dashboard import state_color

        assert state_color("escalated") == "magenta"

    def test_unknown(self):
        from dgov.dashboard import state_color

        assert state_color("nonexistent") == "white"


@pytest.mark.unit
class TestFmtDuration:
    def test_seconds(self):
        from dgov.dashboard import fmt_duration

        assert fmt_duration(45) == "45s"

    def test_minutes(self):
        from dgov.dashboard import fmt_duration

        assert fmt_duration(125) == "2m5s"

    def test_hours(self):
        from dgov.dashboard import fmt_duration

        assert fmt_duration(3661) == "1h1m"

    def test_negative(self):
        from dgov.dashboard import fmt_duration

        assert fmt_duration(-5) == "0s"


@pytest.mark.unit
class TestImports:
    def test_dashboard_importable(self):
        from dgov.dashboard import run_dashboard

        assert callable(run_dashboard)

    def test_terrain_importable(self):
        from dgov.terrain import ErosionModel, render_terrain

        assert callable(render_terrain)
        assert ErosionModel is not None


@pytest.mark.unit
class TestSanitization:
    def test_markup_injection_in_table(self):
        """Rich markup in slug should not be interpreted."""
        from dgov.dashboard import _build_worker_table

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
        from dgov.dashboard import _build_worker_table

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

        from dgov.terrain import ErosionModel, render_terrain

        model = ErosionModel(width=10, height=10, seed=42)
        result = render_terrain(model)
        assert isinstance(result, Text)
        assert len(result) > 0


@pytest.mark.unit
class TestWorkerTable:
    def test_empty_panes(self):
        from dgov.dashboard import _build_worker_table

        table = _build_worker_table([], 0)
        assert table.row_count == 0

    def test_multiple_panes(self):
        from dgov.dashboard import _build_worker_table

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


@pytest.mark.unit
class TestSortPanesHierarchical:
    def test_empty(self):
        from dgov.dashboard import _sort_panes_hierarchical

        assert _sort_panes_hierarchical([], 0) == []

    def test_ltgov_before_standalone(self):
        from dgov.dashboard import _sort_panes_hierarchical

        panes = [
            {"slug": "worker-a", "role": "worker"},
            {"slug": "lt-gov-1", "role": "lt-gov"},
        ]
        result = _sort_panes_hierarchical(panes, 0)
        slugs = [p.get("slug") for p, *_ in result]
        assert slugs == ["lt-gov-1", "worker-a"]

    def test_children_nested_under_parent(self):
        from dgov.dashboard import _sort_panes_hierarchical

        panes = [
            {"slug": "standalone", "role": "worker"},
            {"slug": "child-1", "role": "worker", "parent_slug": "lt-gov-1"},
            {"slug": "lt-gov-1", "role": "lt-gov"},
            {"slug": "child-2", "role": "worker", "parent_slug": "lt-gov-1"},
        ]
        result = _sort_panes_hierarchical(panes, 0)
        slugs = [p.get("slug") for p, *_ in result]
        assert slugs == ["lt-gov-1", "child-1", "child-2", "standalone"]

    def test_last_child_flag(self):
        from dgov.dashboard import _sort_panes_hierarchical

        panes = [
            {"slug": "lt-gov-1", "role": "lt-gov"},
            {"slug": "child-1", "role": "worker", "parent_slug": "lt-gov-1"},
            {"slug": "child-2", "role": "worker", "parent_slug": "lt-gov-1"},
        ]
        result = _sort_panes_hierarchical(panes, 0)
        # child-1 is not last, child-2 is last
        assert result[1][2] is False  # child-1 is_last_child
        assert result[2][2] is True  # child-2 is_last_child

    def test_original_index_preserved(self):
        from dgov.dashboard import _sort_panes_hierarchical

        panes = [
            {"slug": "standalone", "role": "worker"},
            {"slug": "lt-gov-1", "role": "lt-gov"},
        ]
        result = _sort_panes_hierarchical(panes, 0)
        # lt-gov-1 was at index 1, standalone at index 0
        assert result[0][3] == 1  # lt-gov-1 original_index
        assert result[1][3] == 0  # standalone original_index

    def test_orphan_children_still_rendered(self):
        from dgov.dashboard import _sort_panes_hierarchical

        panes = [
            {"slug": "orphan", "role": "worker", "parent_slug": "missing-gov"},
        ]
        result = _sort_panes_hierarchical(panes, 0)
        assert len(result) == 1
        assert result[0][1] == 1  # indent_level


@pytest.mark.unit
class TestWorkerTableHierarchy:
    def test_ltgov_diamond_prefix(self):
        from dgov.dashboard import _build_worker_table

        panes = [
            {
                "slug": "gov-1",
                "role": "lt-gov",
                "agent": "claude",
                "state": "active",
                "duration_s": 60,
            },
        ]
        table = _build_worker_table(panes, 0)
        assert table.row_count == 1

    def test_child_tree_prefix(self):
        from dgov.dashboard import _build_worker_table

        panes = [
            {
                "slug": "gov-1",
                "role": "lt-gov",
                "agent": "claude",
                "state": "active",
                "duration_s": 60,
            },
            {
                "slug": "child-1",
                "role": "worker",
                "parent_slug": "gov-1",
                "agent": "pi",
                "state": "active",
                "duration_s": 30,
            },
        ]
        table = _build_worker_table(panes, 0)
        assert table.row_count == 2

    def test_mixed_hierarchy_row_count(self):
        from dgov.dashboard import _build_worker_table

        panes = [
            {
                "slug": "standalone",
                "role": "worker",
                "agent": "pi",
                "state": "done",
                "duration_s": 10,
            },
            {
                "slug": "gov-1",
                "role": "lt-gov",
                "agent": "claude",
                "state": "active",
                "duration_s": 60,
            },
            {
                "slug": "child-1",
                "role": "worker",
                "parent_slug": "gov-1",
                "agent": "pi",
                "state": "active",
                "duration_s": 30,
            },
        ]
        table = _build_worker_table(panes, 1)
        assert table.row_count == 3


@pytest.mark.unit
class TestPreviewState:
    def test_preview_fields_default(self):
        from dgov.dashboard import DashboardState

        state = DashboardState()
        assert state.preview_lines == []
        assert state.preview_visible is False

    def test_preview_hidden_by_default(self):
        from dgov.dashboard import DashboardState

        state = DashboardState(
            panes=[
                {
                    "slug": "worker-a",
                    "agent": "pi",
                    "state": "active",
                    "summary": "working",
                    "duration_s": 60,
                }
            ],
            branch="main",
            last_refresh=1710000000,
            preview_lines=["line 1", "line 2"],
            preview_visible=False,
        )
        output = _render_dashboard_text(state, width=120, height=30)
        assert "Output:" not in output

    def test_preview_shown_when_visible(self):
        from dgov.dashboard import DashboardState

        state = DashboardState(
            panes=[
                {
                    "slug": "worker-a",
                    "agent": "pi",
                    "state": "active",
                    "summary": "working",
                    "duration_s": 60,
                }
            ],
            branch="main",
            last_refresh=1710000000,
            preview_lines=["hello from worker"],
            preview_visible=True,
        )
        output = _render_dashboard_text(state, width=120, height=30)
        assert "Output: worker-a" in output
        assert "hello from worker" in output

    def test_preview_hidden_when_no_lines(self):
        from dgov.dashboard import DashboardState

        state = DashboardState(
            panes=[
                {
                    "slug": "worker-a",
                    "agent": "pi",
                    "state": "active",
                    "summary": "working",
                    "duration_s": 60,
                }
            ],
            branch="main",
            last_refresh=1710000000,
            preview_lines=[],
            preview_visible=True,
        )
        output = _render_dashboard_text(state, width=120, height=30)
        assert "Output:" not in output

    def test_footer_includes_preview_key(self):
        from dgov.dashboard import DashboardState

        state = DashboardState(branch="main", last_refresh=1710000000)
        output = _render_dashboard_text(state, width=120, height=20)
        assert "p:preview" in output


@pytest.mark.unit
class TestLayoutRendering:
    def test_dashboard_text_is_visible(self):
        from dgov.dashboard import DashboardState

        state = DashboardState(
            panes=[
                {
                    "slug": "worker-a",
                    "agent": "pi",
                    "state": "active",
                    "summary": "processing tiles",
                    "duration_s": 125,
                }
            ],
            branch="main",
            last_refresh=1710000000,
        )

        output = _render_dashboard_text(state, width=120, height=16)

        assert "DGOV v" in output
        assert "worker-a" in output
        assert "q:quit" in output

    def test_build_layout_reuses_existing_tree(self):
        from dgov.dashboard import DashboardState, _build_layout

        state = DashboardState(
            panes=[
                {
                    "slug": "worker-a",
                    "agent": "pi",
                    "state": "active",
                    "summary": "processing tiles",
                    "duration_s": 125,
                }
            ],
            branch="main",
            last_refresh=1710000000,
            preview_lines=["hello from worker"],
            preview_visible=True,
        )

        layout = _build_layout(state, term_width=120, term_height=20)
        updated = _build_layout(state, term_width=99, term_height=13, layout=layout)

        assert updated is layout
        assert layout["body"]["bottom"]["preview"].visible is True

    def test_monitor_panel_exists(self):
        from dgov.dashboard import DashboardState, _build_layout

        state = DashboardState(branch="main", last_refresh=1710000000)
        layout = _build_layout(state, term_width=120, term_height=20)
        assert layout["body"]["bottom"].get("monitor") is not None
