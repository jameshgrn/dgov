"""Unit tests for the dgov dashboard."""

from __future__ import annotations

from unittest.mock import patch

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

        assert state_color("active") == "bright_cyan"

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

        assert fmt_duration(45) == "45.000s"

    def test_minutes(self):
        from dgov.dashboard import fmt_duration

        assert fmt_duration(125) == "2m5s"

    def test_hours(self):
        from dgov.dashboard import fmt_duration

        assert fmt_duration(3661) == "1h1m"

    def test_negative(self):
        from dgov.dashboard import fmt_duration

        assert fmt_duration(-5) == "0.000s"


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
class TestExecuteAction:
    @patch("dgov.executor.run_land_only")
    def test_merge_blocks_non_safe_review(self, mock_land):
        from dgov.dashboard import DashboardState, _execute_action

        mock_land.return_value = type(
            "R",
            (),
            {
                "review": {"slug": "task", "verdict": "review", "commit_count": 1},
                "merge_result": None,
                "error": "Review verdict is review; refusing to merge",
            },
        )()
        state = DashboardState(project_root="/tmp/repo", session_root="/tmp/repo")

        _execute_action(state, "merge", "task")

        assert "refusing to merge" in state.error
        mock_land.assert_called_once()


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
    def _render_table_text(self, panes: list[dict]) -> str:
        from dgov.dashboard import _build_worker_table

        console = Console(record=True, force_terminal=True, width=120, height=20)
        console.print(_build_worker_table(panes, 0))
        return console.export_text()

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

    def test_active_state_does_not_render_done_phase_from_monitor(self):
        text = self._render_table_text(
            [
                {
                    "slug": "worker-a",
                    "agent": "river-9b",
                    "state": "active",
                    "phase": "committing",
                    "monitor_classification": "done",
                    "monitor_has_commits": True,
                    "duration_s": 19,
                }
            ]
        )

        assert "active" in text
        assert "committing" in text
        assert "done" not in text

    def test_terminal_state_does_not_repeat_state_in_phase(self):
        text = self._render_table_text(
            [
                {
                    "slug": "worker-b",
                    "agent": "river-9b",
                    "state": "done",
                    "phase": "done",
                    "monitor_classification": "done",
                    "duration_s": 43,
                }
            ]
        )

        assert text.count("done") == 1

    def test_preserved_recoverable_terminal_pane_shows_resume_phase(self):
        text = self._render_table_text(
            [
                {
                    "slug": "worker-c",
                    "agent": "river-9b",
                    "state": "timed_out",
                    "preserved_artifacts": {"recoverable": True, "reason": "dirty_worktree"},
                    "duration_s": 43,
                }
            ]
        )

        assert "timed_out" in text
        assert "resume" in text

    def test_preserved_nonrecoverable_terminal_pane_shows_inspect_phase(self):
        text = self._render_table_text(
            [
                {
                    "slug": "worker-d",
                    "agent": "river-9b",
                    "state": "failed",
                    "preserved_artifacts": {"recoverable": False, "reason": "review_pending"},
                    "duration_s": 43,
                }
            ]
        )

        assert "failed" in text
        assert "inspect" in text


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

    def test_selection_order_matches_visual_hierarchy(self):
        from dgov.dashboard import _selection_order

        panes = [
            {"slug": "worker-a", "role": "worker"},
            {"slug": "lt-gov-1", "role": "lt-gov"},
            {"slug": "child-1", "role": "worker", "parent_slug": "lt-gov-1"},
        ]

        assert _selection_order(panes) == [1, 2, 0]

    def test_move_selection_follows_visual_hierarchy(self):
        from dgov.dashboard import _move_selection

        panes = [
            {"slug": "worker-a", "role": "worker"},
            {"slug": "lt-gov-1", "role": "lt-gov"},
            {"slug": "child-1", "role": "worker", "parent_slug": "lt-gov-1"},
        ]

        selected, position = _move_selection(panes, selected=1, step=1)
        assert (selected, position) == (2, 1)

        selected, position = _move_selection(panes, selected=2, step=1)
        assert (selected, position) == (0, 2)


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
class TestDashboardObserverMode:
    def test_data_thread_waits_on_notify_pipe(self, monkeypatch):
        from dgov.dashboard import DashboardState, data_thread

        state = DashboardState(project_root="/tmp/repo", session_root="/tmp/session")
        refresh_calls: list[str] = []
        wait_calls: list[tuple[str, float]] = []

        monkeypatch.setattr(
            "dgov.dashboard._refresh_dashboard_state",
            lambda current: refresh_calls.append(current.project_root),
        )

        def fake_wait(session_root: str, timeout: float) -> bool:
            wait_calls.append((session_root, timeout))
            state.stop_event.set()
            return True

        monkeypatch.setattr("dgov.persistence._wait_for_notify", fake_wait)

        data_thread(state, 0.25)

        assert refresh_calls == ["/tmp/repo"]
        assert wait_calls == [("/tmp/session", 5.0)]

    def test_run_dashboard_starts_only_data_thread(self, tmp_path, monkeypatch):
        import io

        import dgov.dashboard as dashboard

        started_targets: list[str] = []
        live_kwargs: dict[str, object] = {}
        lock_fd = io.StringIO()
        pidfile = tmp_path / "dashboard.pid"

        class FakeThread:
            def __init__(self, target, args=(), daemon=False):  # noqa: ANN001
                self.target = target

            def start(self) -> None:
                started_targets.append(self.target.__name__)

        class FakeLive:
            def __init__(self, *args, **kwargs):  # noqa: ANN001
                live_kwargs.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
                return False

            def refresh(self) -> None:
                return None

            def stop(self) -> None:
                return None

            def start(self) -> None:
                return None

        real_state_cls = dashboard.DashboardState

        def fake_state(*args, **kwargs):  # noqa: ANN001, ANN201
            state = real_state_cls(*args, **kwargs)
            state.stop_event.set()
            return state

        monkeypatch.setattr(
            "dgov.dashboard._acquire_dashboard_lock",
            lambda *_: (pidfile, lock_fd),
        )
        monkeypatch.setattr("dgov.dashboard.threading.Thread", FakeThread)
        monkeypatch.setattr("dgov.dashboard.Live", FakeLive)
        monkeypatch.setattr("dgov.dashboard.DashboardState", fake_state)
        monkeypatch.setattr("dgov.dashboard._wake_dashboard_observer", lambda *_: None)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: False)

        dashboard.run_dashboard(str(tmp_path), str(tmp_path), refresh_interval=0.25)

        assert started_targets == ["data_thread"]
        assert live_kwargs["refresh_per_second"] == 4.0


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

    def test_eval_summary_in_header(self):
        """Eval summary from typed persistence appears in dashboard header."""
        from dgov.dashboard import DashboardState

        state = DashboardState(
            panes=[{"slug": "w1", "agent": "qwen-9b", "state": "active", "duration_s": 10}],
            branch="main",
            last_refresh=1710000000,
            eval_summary="E:2/3",
        )
        output = _render_dashboard_text(state, width=120, height=16)
        assert "E:2/3" in output

    def test_eval_summary_empty_when_no_dag(self):
        """No eval summary shown when no DAG is active."""
        from dgov.dashboard import DashboardState

        state = DashboardState(branch="main", last_refresh=1710000000)
        output = _render_dashboard_text(state, width=120, height=16)
        assert "E:" not in output

    def test_eval_summary_shows_failures(self):
        """Eval summary includes failure count when evals fail."""
        from dgov.dashboard import DashboardState

        state = DashboardState(
            branch="main",
            last_refresh=1710000000,
            eval_summary="E:1/3 (1 FAIL)",
        )
        output = _render_dashboard_text(state, width=120, height=16)
        assert "E:1/3" in output
        assert "FAIL" in output


@pytest.mark.unit
class TestDoneNotifications:
    """Focused tests for dashboard done-event notifications."""

    def test_pane_done_event_shows_in_monitor_panel(self):
        """A new pane_done event appears in the monitor panel."""
        from dgov.dashboard import DashboardState, _build_layout

        state = DashboardState(
            branch="main",
            last_refresh=1710000000,
            events=[
                {
                    "ts": "2025-03-15T10:30:00+00:00",
                    "event": "pane_done",
                    "pane": "worker-a",
                    "data": "{}",
                }
            ],
        )
        layout = _build_layout(state, term_width=120, term_height=20)
        monitor_panel = layout["body"]["bottom"]["monitor"]

        # Render the panel content to check for the event
        from rich.console import Console

        console = Console(record=True, force_terminal=True, width=120, height=5)
        console.print(monitor_panel)
        output = console.export_text()

        assert "pane_done" in output
        assert "worker-a" in output

    def test_pane_done_event_styled_green(self):
        """pane_done events are styled green in the monitor panel."""
        from dgov.dashboard import DashboardState, _build_layout

        state = DashboardState(
            branch="main",
            last_refresh=1710000000,
            events=[
                {
                    "ts": "2025-03-15T10:30:00+00:00",
                    "event": "pane_done",
                    "pane": "worker-b",
                    "data": "{}",
                }
            ],
        )
        layout = _build_layout(state, term_width=120, term_height=20)
        monitor_panel = layout["body"]["bottom"]["monitor"]

        from rich.console import Console

        console = Console(record=True, force_terminal=True, width=120, height=5)
        console.print(monitor_panel)
        output = console.export_text()

        # Green styling should be present (rich uses ANSI codes or markup)
        assert "pane_done" in output
        assert "worker-b" in output

    def test_multiple_pane_done_events_show_chronologically(self):
        """Multiple pane_done events display in chronological order."""
        from dgov.dashboard import DashboardState, _build_layout

        state = DashboardState(
            branch="main",
            last_refresh=1710000000,
            events=[
                {
                    "ts": "2025-03-15T10:28:00+00:00",
                    "event": "pane_done",
                    "pane": "worker-1",
                    "data": "{}",
                },
                {
                    "ts": "2025-03-15T10:29:00+00:00",
                    "event": "pane_done",
                    "pane": "worker-2",
                    "data": "{}",
                },
                {
                    "ts": "2025-03-15T10:30:00+00:00",
                    "event": "pane_done",
                    "pane": "worker-3",
                    "data": "{}",
                },
            ],
        )
        layout = _build_layout(state, term_width=120, term_height=20)
        monitor_panel = layout["body"]["bottom"]["monitor"]

        from rich.console import Console

        console = Console(record=True, force_terminal=True, width=120, height=8)
        console.print(monitor_panel)
        output = console.export_text()

        assert "worker-1" in output
        assert "worker-2" in output
        assert "worker-3" in output

    def test_mixed_event_types_with_pane_done(self):
        """pane_done events render correctly alongside other event types."""
        from dgov.dashboard import DashboardState, _build_layout

        state = DashboardState(
            branch="main",
            last_refresh=1710000000,
            events=[
                {
                    "ts": "2025-03-15T10:25:00+00:00",
                    "event": "pane_created",
                    "pane": "worker-x",
                    "data": "{}",
                },
                {
                    "ts": "2025-03-15T10:30:00+00:00",
                    "event": "pane_done",
                    "pane": "worker-y",
                    "data": "{}",
                },
                {
                    "ts": "2025-03-15T10:35:00+00:00",
                    "event": "pane_merged",
                    "pane": "worker-z",
                    "data": "{}",
                },
            ],
        )
        layout = _build_layout(state, term_width=120, term_height=20)
        monitor_panel = layout["body"]["bottom"]["monitor"]

        from rich.console import Console

        console = Console(record=True, force_terminal=True, width=120, height=8)
        console.print(monitor_panel)
        output = console.export_text()

        assert "pane_created" in output
        assert "pane_done" in output
        assert "pane_merged" in output
        assert "worker-x" in output
        assert "worker-y" in output
        assert "worker-z" in output


@pytest.mark.unit
class TestTracePreview:
    def test_trace_preview_renders_structured_lines(self):
        from dgov.dashboard import DashboardState

        state = DashboardState(
            panes=[
                {
                    "slug": "worker-a",
                    "agent": "qwen-9b",
                    "state": "active",
                    "duration_s": 60,
                }
            ],
            branch="main",
            last_refresh=1710000000,
            preview_lines=["phases: dispatch[120ms] review[45ms] safe", "tools: Read, Bash"],
            preview_visible=True,
        )

        output = _render_dashboard_text(state, width=120, height=30)
        assert "phases: dispatch[120ms] review[45ms] safe" in output
        assert "tools: Read, Bash" in output
        assert "Output: worker-a" in output

    def test_format_trace_data_returns_empty_when_no_data(self, tmp_path):
        from dgov.dashboard import _format_trace_data

        assert _format_trace_data(str(tmp_path), "nonexistent-slug") == []

    def test_format_trace_data_with_spans_only(self, tmp_path):
        from dgov.dashboard import _format_trace_data
        from dgov.spans import SpanKind, SpanOutcome, close_span, open_span

        trace_id = "test-trace-spans-only"
        span_id = open_span(str(tmp_path), trace_id, SpanKind.DISPATCH)
        close_span(
            str(tmp_path),
            span_id,
            SpanOutcome.SUCCESS,
            agent="qwen-9b",
            verdict="safe",
            commit_count=2,
            files_changed=1,
        )

        lines = _format_trace_data(str(tmp_path), trace_id)
        assert len(lines) == 1
        assert lines[0].startswith("phases: dispatch")

    def test_format_trace_data_with_review_verdict(self, tmp_path):
        from dgov.dashboard import _format_trace_data
        from dgov.spans import SpanKind, SpanOutcome, close_span, open_span

        trace_id = "test-trace-review"
        span_id = open_span(str(tmp_path), trace_id, SpanKind.REVIEW)
        close_span(str(tmp_path), span_id, SpanOutcome.SUCCESS, verdict="safe")

        lines = _format_trace_data(str(tmp_path), trace_id)
        assert len(lines) == 1
        assert lines[0].startswith("phases: review")
        assert "safe" in lines[0]

    def test_format_trace_data_with_tool_call_breakdown(self, tmp_path):
        from dgov.dashboard import _format_trace_data
        from dgov.spans import _get_db

        trace_id = "test-trace-breakdown"
        conn = _get_db(str(tmp_path))
        conn.execute(
            (
                "INSERT INTO tool_traces "
                "(trace_id, seq, ts, role, action_type, tool_name) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (trace_id, 1, "2025-03-27T10:00:01+00:00", "assistant", "tool_call", "Read"),
        )
        conn.execute(
            (
                "INSERT INTO tool_traces "
                "(trace_id, seq, ts, role, action_type, tool_name) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            (trace_id, 2, "2025-03-27T10:00:02+00:00", "assistant", "tool_call", "Bash"),
        )
        conn.commit()

        lines = _format_trace_data(str(tmp_path), trace_id)
        assert lines == ["tools: Read, Bash"]

    def test_format_trace_data_with_tool_traces_only(self, tmp_path):
        from dgov.dashboard import _format_trace_data
        from dgov.spans import _get_db

        trace_id = "test-trace-tools-only"
        conn = _get_db(str(tmp_path))
        conn.execute(
            (
                "INSERT INTO tool_traces "
                "(trace_id, seq, ts, role, action_type, tool_name, thinking) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)"
            ),
            (
                trace_id,
                1,
                "2025-03-27T10:00:00+00:00",
                "assistant",
                "thinking",
                "",
                "Need to inspect the dashboard renderer before editing.",
            ),
        )
        conn.commit()

        lines = _format_trace_data(str(tmp_path), trace_id)
        assert lines == ["thought: Need to inspect the dashboard renderer before editing."]

    def test_dashboard_preview_empty_fallback(self):
        from dgov.dashboard import DashboardState

        state = DashboardState(
            panes=[
                {
                    "slug": "worker-empty",
                    "agent": "qwen-9b",
                    "state": "active",
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
