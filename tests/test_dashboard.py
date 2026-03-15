"""Unit tests for dgov.dashboard — data formatting and color logic."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from dgov.dashboard import (
    COLUMNS,
    STATE_COLORS,
    DashboardState,
    compute_col_widths,
    fetch_detail,
    fetch_panes,
    fmt_duration,
    format_row,
    state_color,
    truncate,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------


class TestTruncate:
    def test_empty_string(self) -> None:
        assert truncate("", 10) == ""

    def test_short_string(self) -> None:
        assert truncate("hello", 10) == "hello"

    def test_exact_fit(self) -> None:
        assert truncate("hello", 5) == "hello"

    def test_truncated_with_ellipsis(self) -> None:
        result = truncate("hello world", 8)
        assert len(result) == 8
        assert result.endswith("\u2026")
        assert result == "hello w\u2026"

    def test_very_narrow(self) -> None:
        assert truncate("hello", 2) == "h\u2026"

    def test_width_three(self) -> None:
        assert truncate("hello", 3) == "he\u2026"

    def test_none_input(self) -> None:
        assert truncate(None, 10) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# fmt_duration
# ---------------------------------------------------------------------------


class TestFmtDuration:
    def test_seconds(self) -> None:
        assert fmt_duration(42) == "42s"

    def test_minutes(self) -> None:
        assert fmt_duration(125) == "2m5s"

    def test_hours(self) -> None:
        assert fmt_duration(3661) == "1h1m"

    def test_zero(self) -> None:
        assert fmt_duration(0) == "0s"

    def test_negative(self) -> None:
        assert fmt_duration(-5) == "0s"

    def test_exact_minute(self) -> None:
        assert fmt_duration(60) == "1m0s"

    def test_exact_hour(self) -> None:
        assert fmt_duration(3600) == "1h0m"


# ---------------------------------------------------------------------------
# state_color
# ---------------------------------------------------------------------------


class TestStateColor:
    def test_known_states(self) -> None:
        assert state_color("active") == 1
        assert state_color("done") == 2
        assert state_color("merged") == 2
        assert state_color("failed") == 3
        assert state_color("abandoned") == 3
        assert state_color("reviewed_pass") == 4
        assert state_color("escalated") == 5
        assert state_color("closed") == 6

    def test_unknown_state(self) -> None:
        assert state_color("nonexistent") == 0

    def test_all_pane_states_have_color(self) -> None:
        from dgov.persistence import PANE_STATES

        for st in PANE_STATES:
            # Every state should map to something (even 0 is ok for unknown)
            assert isinstance(state_color(st), int)


# ---------------------------------------------------------------------------
# compute_col_widths
# ---------------------------------------------------------------------------


class TestComputeColWidths:
    def test_wide_terminal(self) -> None:
        widths = compute_col_widths(200)
        assert widths["slug"] == 20
        assert widths["agent"] == 10
        assert widths["prompt"] > 40  # prompt gets the extra space

    def test_narrow_terminal(self) -> None:
        widths = compute_col_widths(80)
        assert widths["prompt"] >= 10  # minimum prompt width

    def test_all_columns_present(self) -> None:
        widths = compute_col_widths(120)
        for name, _ in COLUMNS:
            assert name in widths


# ---------------------------------------------------------------------------
# format_row
# ---------------------------------------------------------------------------


class TestFormatRow:
    def test_basic_formatting(self) -> None:
        pane = {
            "slug": "fix-lint",
            "agent": "claude",
            "state": "active",
            "alive": True,
            "done": False,
            "freshness": "recent",
            "duration_s": 125,
            "prompt": "Fix the linting errors in cli.py",
        }
        widths = compute_col_widths(120)
        row = format_row(pane, widths)
        assert row["slug"] == "fix-lint"
        assert row["agent"] == "claude"
        assert row["alive"] == "\u2713"
        assert row["done"] == "\u2717"
        assert row["duration"] == "2m5s"

    def test_missing_fields(self) -> None:
        pane: dict = {}
        widths = compute_col_widths(120)
        row = format_row(pane, widths)
        assert row["slug"] == ""
        assert row["agent"] == "?"
        assert row["alive"] == "\u2717"
        assert row["done"] == "\u2717"

    def test_long_slug_truncated(self) -> None:
        pane = {
            "slug": "a" * 50,
            "agent": "pi",
            "state": "done",
            "alive": False,
            "done": True,
            "freshness": "stale",
            "duration_s": 0,
            "prompt": "",
        }
        widths = compute_col_widths(120)
        row = format_row(pane, widths)
        assert len(row["slug"]) <= widths["slug"]


# ---------------------------------------------------------------------------
# DashboardState
# ---------------------------------------------------------------------------


class TestDashboardState:
    def test_default_state(self) -> None:
        state = DashboardState()
        assert state.panes == []
        assert state.last_refresh == 0.0
        assert state.error == ""
        assert state.branch == ""

    def test_lock_is_independent(self) -> None:
        s1 = DashboardState()
        s2 = DashboardState()
        assert s1.lock is not s2.lock


# ---------------------------------------------------------------------------
# fetch_panes
# ---------------------------------------------------------------------------


class TestFetchPanes:
    @patch("dgov.status.list_worker_panes")
    @patch("dgov.dashboard._get_branch", return_value="main")
    def test_success(self, mock_branch, mock_list) -> None:
        mock_list.return_value = [
            {"slug": "fix-a", "agent": "pi", "state": "done", "alive": False, "done": True}
        ]
        state = DashboardState(project_root="/tmp/test")
        fetch_panes(state)
        assert len(state.panes) == 1
        assert state.panes[0]["slug"] == "fix-a"
        assert state.branch == "main"
        assert state.error == ""
        assert state.last_refresh > 0

    @patch("dgov.status.list_worker_panes", side_effect=RuntimeError("boom"))
    def test_error_handling(self, mock_list) -> None:
        state = DashboardState(project_root="/tmp/test")
        fetch_panes(state)
        assert state.error == "boom"
        assert state.last_refresh > 0


# ---------------------------------------------------------------------------
# fetch_detail
# ---------------------------------------------------------------------------


class TestFetchDetail:
    def test_pane_not_found(self) -> None:
        state = DashboardState(project_root="/tmp/test")
        state.panes = []
        fetch_detail(state, "nonexistent")
        assert "not found" in state.detail_text.lower()

    @patch("dgov.status.capture_worker_output", return_value="some output")
    @patch(
        "dgov.inspection.review_worker_pane",
        return_value={"stat": "1 file", "commit_count": 1},
    )
    def test_success(self, mock_review, mock_capture) -> None:
        state = DashboardState(project_root="/tmp/test")
        state.panes = [
            {
                "slug": "fix-a",
                "agent": "claude",
                "state": "done",
                "alive": True,
                "done": True,
                "branch": "fix-a",
                "worktree_path": "/tmp/wt",
                "pane_id": "%5",
                "duration_s": 60,
                "prompt": "Fix the thing",
            }
        ]
        fetch_detail(state, "fix-a")
        assert state.detail_slug == "fix-a"
        assert "Fix the thing" in state.detail_text
        assert "claude" in state.detail_text
        assert "1 file" in state.detail_text
        assert "some output" in state.detail_text

    @patch("dgov.status.capture_worker_output", return_value=None)
    @patch("dgov.inspection.review_worker_pane", side_effect=Exception("git error"))
    def test_graceful_on_errors(self, mock_review, mock_capture) -> None:
        state = DashboardState(project_root="/tmp/test")
        state.panes = [
            {
                "slug": "fix-a",
                "agent": "pi",
                "state": "active",
                "alive": True,
                "done": False,
                "prompt": "Do something",
                "branch": "fix-a",
                "worktree_path": "/tmp/wt",
                "pane_id": "%5",
                "duration_s": 10,
            }
        ]
        fetch_detail(state, "fix-a")
        assert state.detail_slug == "fix-a"
        # Should not raise, should contain error info
        assert "unavailable" in state.detail_text.lower() or "error" in state.detail_text.lower()


# ---------------------------------------------------------------------------
# STATE_COLORS coverage
# ---------------------------------------------------------------------------


class TestStateColorsMapping:
    def test_green_states(self) -> None:
        assert STATE_COLORS["done"] == STATE_COLORS["merged"]

    def test_red_states(self) -> None:
        assert STATE_COLORS["failed"] == STATE_COLORS["abandoned"]
        assert STATE_COLORS["failed"] == STATE_COLORS["timed_out"]

    def test_distinct_color_groups(self) -> None:
        green = STATE_COLORS["done"]
        yellow = STATE_COLORS["active"]
        red = STATE_COLORS["failed"]
        cyan = STATE_COLORS["reviewed_pass"]
        assert len({green, yellow, red, cyan}) == 4
