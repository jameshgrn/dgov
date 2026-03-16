"""Smoke tests for dgov.dashboard — curses draw functions, data thread, actions."""

from __future__ import annotations

import threading
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from dgov.dashboard import (
    COLUMNS,
    DashboardState,
    _draw_confirmation,
    _draw_detail,
    _draw_footer,
    _draw_header,
    _draw_pane_row,
    _draw_table_header,
    _execute_action,
    _get_branch,
    _show_capture,
    _show_diff,
    compute_col_widths,
    data_thread,
    run_dashboard,
)

pytestmark = pytest.mark.unit

SAMPLE_PANE = {
    "slug": "fix-lint",
    "agent": "claude",
    "state": "active",
    "alive": True,
    "done": False,
    "freshness": "recent",
    "duration_s": 125,
    "prompt": "Fix the linting errors",
    "branch": "fix-lint",
    "worktree_path": "/tmp/wt",
    "pane_id": "%5",
}

SAMPLE_PANES = [
    SAMPLE_PANE,
    {
        **SAMPLE_PANE,
        "slug": "add-tests",
        "branch": "add-tests",
        "pane_id": "%6",
    },
    {
        **SAMPLE_PANE,
        "slug": "ship-release",
        "branch": "ship-release",
        "pane_id": "%7",
    },
]

TEST_KEY_DOWN = 258
TEST_KEY_UP = 259
TEST_KEY_ENTER = 343
TEST_KEY_RESIZE = 410


class _NoopThread:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def start(self) -> None:
        pass


def _make_dashboard_state(panes: list[dict] | None = None) -> DashboardState:
    state = DashboardState(project_root="/tmp/project", session_root="/tmp/session")
    state.panes = list(panes or [])
    return state


def _run_dashboard_loop(stdscr: MagicMock, state: DashboardState) -> None:
    def _wrapper(
        func: Callable[..., None],
        *,
        project_root: str,
        session_root: str | None,
        refresh_interval: float,
    ) -> None:
        assert project_root == "/tmp/project"
        assert session_root == "/tmp/session"
        assert refresh_interval == 1.0
        func(
            stdscr,
            project_root=project_root,
            session_root=session_root,
            refresh_interval=refresh_interval,
        )

    with (
        patch("dgov.dashboard.curses.wrapper", side_effect=_wrapper),
        patch("dgov.dashboard.DashboardState", return_value=state),
        patch("dgov.dashboard._init_colors"),
        patch("dgov.dashboard.data_thread"),
        patch("dgov.dashboard.threading.Thread", _NoopThread),
        patch("dgov.dashboard.curses.curs_set"),
        patch("dgov.dashboard.curses.KEY_DOWN", TEST_KEY_DOWN),
        patch("dgov.dashboard.curses.KEY_UP", TEST_KEY_UP),
        patch("dgov.dashboard.curses.KEY_ENTER", TEST_KEY_ENTER),
        patch("dgov.dashboard.curses.KEY_RESIZE", TEST_KEY_RESIZE),
        patch("dgov.dashboard.curses.A_DIM", 1),
        patch("dgov.dashboard.curses.A_BOLD", 2),
        patch("dgov.dashboard.curses.A_REVERSE", 4),
        patch("dgov.dashboard.curses.color_pair", side_effect=lambda idx: idx),
        patch("dgov.status.list_worker_panes", return_value=list(state.panes)),
    ):
        run_dashboard(project_root="/tmp/project", session_root="/tmp/session")


def _make_stdscr(max_y: int = 40, max_x: int = 120) -> MagicMock:
    """Create a mock curses window."""
    scr = MagicMock()
    scr.getmaxyx.return_value = (max_y, max_x)
    scr.addnstr = MagicMock()
    return scr


# ---------------------------------------------------------------------------
# DashboardState instantiation with session_root / project_root
# ---------------------------------------------------------------------------


class TestDashboardStateInstantiation:
    def test_with_mock_paths(self) -> None:
        state = DashboardState(
            project_root="/tmp/project",
            session_root="/tmp/session",
        )
        assert state.project_root == "/tmp/project"
        assert state.session_root == "/tmp/session"
        assert state.panes == []
        assert state.error == ""

    def test_stop_and_refresh_events(self) -> None:
        state = DashboardState()
        assert not state.stop_event.is_set()
        assert not state.force_refresh.is_set()
        state.stop_event.set()
        assert state.stop_event.is_set()


# ---------------------------------------------------------------------------
# _get_branch
# ---------------------------------------------------------------------------


class TestGetBranch:
    @patch("dgov.dashboard.subprocess.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="main\n")
        assert _get_branch("/tmp/project") == "main"

    @patch("dgov.dashboard.subprocess.run")
    def test_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _get_branch("/tmp/project") == "?"

    @patch("dgov.dashboard.subprocess.run", side_effect=OSError("no git"))
    def test_os_error(self, mock_run: MagicMock) -> None:
        assert _get_branch("/tmp/project") == "?"


# ---------------------------------------------------------------------------
# data_thread
# ---------------------------------------------------------------------------


class TestDataThread:
    @patch("dgov.dashboard.fetch_panes")
    def test_fetches_and_stops(self, mock_fetch: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project")
        # Stop immediately after first fetch
        mock_fetch.side_effect = lambda s: s.stop_event.set()

        t = threading.Thread(target=data_thread, args=(state, 0.01))
        t.start()
        t.join(timeout=2)
        assert not t.is_alive()
        mock_fetch.assert_called_once_with(state)

    @patch("dgov.dashboard.fetch_panes")
    def test_force_refresh_wakes_thread(self, mock_fetch: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project")
        call_count = 0

        def counting_fetch(s: DashboardState) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                s.stop_event.set()
                # Also wake the wait() so thread exits promptly
                s.force_refresh.set()

        mock_fetch.side_effect = counting_fetch
        state.force_refresh.set()

        t = threading.Thread(target=data_thread, args=(state, 10.0))
        t.start()
        t.join(timeout=5)
        assert not t.is_alive()
        assert call_count >= 2


# ---------------------------------------------------------------------------
# Draw functions (mock curses window)
# ---------------------------------------------------------------------------


class TestDrawHeader:
    def test_returns_row_after_header(self) -> None:
        scr = _make_stdscr()
        state = DashboardState(project_root="/tmp/project")
        state.branch = "main"
        state.last_refresh = 1000000.0
        state.panes = [SAMPLE_PANE]
        row = _draw_header(scr, state, 120)
        assert row >= 1

    def test_error_adds_row(self) -> None:
        scr = _make_stdscr()
        state = DashboardState(project_root="/tmp/project")
        state.error = "something broke"
        state.last_refresh = 1000000.0
        row_with_error = _draw_header(scr, state, 120)

        scr2 = _make_stdscr()
        state2 = DashboardState(project_root="/tmp/project")
        state2.last_refresh = 1000000.0
        row_no_error = _draw_header(scr2, state2, 120)

        assert row_with_error == row_no_error + 1


class TestDrawTableHeader:
    def test_draws_all_columns(self) -> None:
        scr = _make_stdscr()
        widths = compute_col_widths(120)
        row = _draw_table_header(scr, 0, widths, 120)
        assert row == 1
        # Labels + │ separators between columns
        expected_calls = len(COLUMNS) + (len(COLUMNS) - 1)
        assert scr.addnstr.call_count == expected_calls


class TestDrawPaneRow:
    @patch("dgov.dashboard.curses.color_pair", return_value=0)
    def test_draws_without_error(self, _mock_cp: MagicMock) -> None:
        scr = _make_stdscr()
        widths = compute_col_widths(120)
        _draw_pane_row(scr, 5, SAMPLE_PANE, widths, selected=False, max_x=120)
        assert scr.addnstr.called

    @patch("dgov.dashboard.curses.color_pair", return_value=0)
    def test_selected_row(self, _mock_cp: MagicMock) -> None:
        scr = _make_stdscr()
        widths = compute_col_widths(120)
        _draw_pane_row(scr, 5, SAMPLE_PANE, widths, selected=True, max_x=120)
        assert scr.addnstr.called


class TestDrawFooter:
    def test_list_mode(self) -> None:
        scr = _make_stdscr()
        _draw_footer(scr, 38, 120, mode="list")
        assert scr.addnstr.called

    def test_detail_mode(self) -> None:
        scr = _make_stdscr()
        _draw_footer(scr, 38, 120, mode="detail")
        assert scr.addnstr.called


class TestDrawDetail:
    def test_renders_detail_text(self) -> None:
        scr = _make_stdscr()
        state = DashboardState()
        state.detail_slug = "fix-lint"
        state.detail_text = "== Prompt ==\nFix stuff\n\n== Info ==\nAgent: claude"
        _draw_detail(scr, state, scroll_offset=0, max_y=40, max_x=120)
        assert scr.addnstr.called

    def test_scroll_offset(self) -> None:
        scr = _make_stdscr()
        state = DashboardState()
        state.detail_slug = "fix-lint"
        state.detail_text = "\n".join(f"line {i}" for i in range(50))
        _draw_detail(scr, state, scroll_offset=10, max_y=40, max_x=120)
        assert scr.addnstr.called


class TestDrawConfirmation:
    @patch("dgov.dashboard.curses.color_pair", return_value=0)
    def test_renders_message(self, _mock_cp: MagicMock) -> None:
        scr = _make_stdscr()
        _draw_confirmation(scr, "MERGE 'fix-lint'? y/n", max_y=40, max_x=120)
        assert scr.addnstr.called


# ---------------------------------------------------------------------------
# _show_diff / _show_capture
# ---------------------------------------------------------------------------


class TestShowDiff:
    @patch("dgov.inspection.diff_worker_pane", return_value={"diff": "+added line"})
    def test_success(self, mock_diff: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project", session_root="/tmp/session")
        _show_diff(state, "fix-lint")
        assert state.detail_slug == "fix-lint"
        assert "+added line" in state.detail_text

    @patch("dgov.inspection.diff_worker_pane", return_value={"error": "no worktree"})
    def test_error_result(self, mock_diff: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project")
        _show_diff(state, "fix-lint")
        assert "no worktree" in state.detail_text.lower()

    @patch("dgov.inspection.diff_worker_pane", side_effect=RuntimeError("git died"))
    def test_exception(self, mock_diff: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project")
        _show_diff(state, "fix-lint")
        assert "git died" in state.detail_text


class TestShowCapture:
    @patch("dgov.status.capture_worker_output", return_value="some output text")
    def test_success(self, mock_capture: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project", session_root="/tmp/session")
        _show_capture(state, "fix-lint")
        assert state.detail_slug == "fix-lint"
        assert "some output text" in state.detail_text

    @patch("dgov.status.capture_worker_output", return_value=None)
    def test_no_output(self, mock_capture: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project")
        _show_capture(state, "fix-lint")
        assert "not found" in state.detail_text.lower() or "dead" in state.detail_text.lower()

    @patch("dgov.status.capture_worker_output", side_effect=RuntimeError("pane gone"))
    def test_exception(self, mock_capture: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project")
        _show_capture(state, "fix-lint")
        assert "pane gone" in state.detail_text


# ---------------------------------------------------------------------------
# _execute_action
# ---------------------------------------------------------------------------


class TestExecuteAction:
    @patch("dgov.merger.merge_worker_pane")
    def test_merge(self, mock_merge: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project", session_root="/tmp/session")
        _execute_action(state, "merge", "fix-lint")
        mock_merge.assert_called_once_with("/tmp/project", "fix-lint", session_root="/tmp/session")

    @patch("dgov.merger.merge_worker_pane", side_effect=RuntimeError("conflict"))
    def test_merge_failure(self, mock_merge: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project")
        _execute_action(state, "merge", "fix-lint")
        assert "failed" in state.detail_text.lower()

    @patch("dgov.lifecycle.close_worker_pane")
    def test_close(self, mock_close: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project", session_root="/tmp/session")
        _execute_action(state, "close", "fix-lint")
        mock_close.assert_called_once_with("/tmp/project", "fix-lint", session_root="/tmp/session")

    @patch("dgov.lifecycle.close_worker_pane", side_effect=RuntimeError("tmux error"))
    def test_close_failure(self, mock_close: MagicMock) -> None:
        state = DashboardState(project_root="/tmp/project")
        _execute_action(state, "close", "fix-lint")
        assert "failed" in state.detail_text.lower()

    def test_unknown_action_is_noop(self) -> None:
        state = DashboardState(project_root="/tmp/project")
        _execute_action(state, "restart", "fix-lint")
        assert state.detail_text == ""


# ---------------------------------------------------------------------------
# run_dashboard (just verify it delegates to curses.wrapper)
# ---------------------------------------------------------------------------


class TestRunDashboard:
    @patch("dgov.dashboard.curses.wrapper")
    def test_delegates_to_curses_wrapper(self, mock_wrapper: MagicMock) -> None:
        run_dashboard(project_root="/tmp/project", session_root="/tmp/session")
        mock_wrapper.assert_called_once()
        args, kwargs = mock_wrapper.call_args
        assert kwargs["project_root"] == "/tmp/project"
        assert kwargs["session_root"] == "/tmp/session"


class TestRunDashboardLoop:
    def test_empty_panes_shows_empty_state(self) -> None:
        stdscr = _make_stdscr()
        stdscr.getch.side_effect = [ord("q")]

        _run_dashboard_loop(stdscr, _make_dashboard_state([]))

        assert any(
            "No active panes" in str(call.args[2]) for call in stdscr.addnstr.call_args_list
        )

    def test_navigation_updates_selected_row(self) -> None:
        stdscr = _make_stdscr()
        stdscr.getch.side_effect = [
            TEST_KEY_DOWN,
            TEST_KEY_DOWN,
            TEST_KEY_UP,
            ord("q"),
        ]
        state = _make_dashboard_state(SAMPLE_PANES)
        selected_slugs: list[str] = []

        def _record_selected(
            _stdscr: MagicMock,
            _row: int,
            pane: dict,
            _col_widths: dict[str, int],
            *,
            selected: bool,
            max_x: int,
            frame: int = 0,
        ) -> None:
            assert max_x == 120
            if selected:
                selected_slugs.append(pane["slug"])

        with patch("dgov.dashboard._draw_pane_row", side_effect=_record_selected):
            _run_dashboard_loop(stdscr, state)

        assert selected_slugs == [
            "fix-lint",
            "add-tests",
            "ship-release",
            "add-tests",
        ]

    def test_detail_view_enters_and_returns_to_list(self) -> None:
        stdscr = _make_stdscr()
        stdscr.getch.side_effect = [TEST_KEY_ENTER, ord("q"), ord("q")]
        state = _make_dashboard_state([SAMPLE_PANE])

        def _populate_detail(current_state: DashboardState, slug: str) -> None:
            current_state.detail_slug = slug
            current_state.detail_text = "detail text"

        with (
            patch("dgov.dashboard.fetch_detail", side_effect=_populate_detail) as mock_detail,
            patch("dgov.dashboard._draw_detail") as mock_draw_detail,
            patch("dgov.dashboard._draw_pane_row") as mock_draw_row,
        ):
            _run_dashboard_loop(stdscr, state)

        mock_detail.assert_called_once_with(state, "fix-lint")
        assert mock_draw_detail.call_count == 1
        assert mock_draw_row.call_count == 2

    def test_refresh_sets_force_refresh_event(self) -> None:
        stdscr = _make_stdscr()
        stdscr.getch.side_effect = [ord("r"), ord("q")]
        state = _make_dashboard_state([SAMPLE_PANE])
        state.force_refresh = MagicMock()

        _run_dashboard_loop(stdscr, state)

        state.force_refresh.set.assert_called_once_with()

    def test_terminal_too_small_shows_message(self) -> None:
        stdscr = _make_stdscr(max_y=3, max_x=30)
        stdscr.getch.side_effect = [ord("q")]

        _run_dashboard_loop(stdscr, _make_dashboard_state([SAMPLE_PANE]))

        assert any(
            call.args[:3] == (0, 0, "Terminal too small") for call in stdscr.addnstr.call_args_list
        )

    def test_merge_confirmation_can_be_canceled(self) -> None:
        stdscr = _make_stdscr()
        stdscr.getch.side_effect = [ord("m"), ord("n"), ord("q")]
        state = _make_dashboard_state([SAMPLE_PANE])

        with (
            patch("dgov.dashboard._draw_confirmation") as mock_confirmation,
            patch("dgov.dashboard._execute_action") as mock_execute,
        ):
            _run_dashboard_loop(stdscr, state)

        mock_confirmation.assert_called_once()
        assert mock_confirmation.call_args.args[1] == " MERGE 'fix-lint'? y/n "
        mock_execute.assert_not_called()
