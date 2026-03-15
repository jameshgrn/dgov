"""Supplemental unit tests for dgov models and pane helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dgov.backend import set_backend
from dgov.lifecycle import _build_pane_title, _state_icon
from dgov.models import MergeResult
from dgov.persistence import (
    WorkerPane,
    _validate_state,
    all_panes,
    emit_event,
    read_events,
    replace_all_panes,
    update_pane_state,
)


@pytest.fixture(autouse=True)
def mock_backend(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    # Default return values for common methods
    mock.create_pane.return_value = "%1"
    mock.is_alive.return_value = True
    mock.bulk_info.return_value = {}
    set_backend(mock)
    return mock


pytestmark = pytest.mark.unit


class TestModels:
    def test_merge_result_defaults(self) -> None:
        result = MergeResult(success=True)
        assert result.stdout == ""
        assert result.stderr == ""
        assert result.conflicts == []

    def test_merge_result_with_conflicts(self) -> None:
        conflict = {
            "file_path": "src/app.py",
            "base": "base",
            "head": "head",
            "branch": "feature/test",
        }
        result = MergeResult(success=False, stderr="boom", conflicts=[conflict])

        assert result.success is False
        assert result.stderr == "boom"
        assert result.conflicts == [conflict]


class TestPaneHelpers:
    def testemit_event_appends_record(self, tmp_path: Path) -> None:
        emit_event(str(tmp_path), "pane_created", "task-1", agent="claude")

        events = read_events(str(tmp_path))
        assert len(events) == 1
        assert events[0]["event"] == "pane_created"
        assert events[0]["pane"] == "task-1"
        assert events[0]["agent"] == "claude"
        assert "ts" in events[0]

    def testemit_event_rejects_unknown_event(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown event"):
            emit_event(str(tmp_path), "dmux_spawned", "task-1")

    def test_validate_state_and_worker_pane_post_init(self) -> None:
        assert _validate_state("active") == "active"
        with pytest.raises(ValueError, match="Unknown pane state"):
            _validate_state("dmux")
        with pytest.raises(ValueError, match="Unknown pane state"):
            WorkerPane(
                slug="bad",
                prompt="Nope",
                pane_id="%1",
                agent="claude",
                project_root="/repo",
                worktree_path="/repo/.dgov/worktrees/bad",
                branch_name="bad",
                state="dmux",
            )

    def test_build_pane_title_is_deterministic(self) -> None:
        title_a = _build_pane_title("claude", "audit", "/tmp/project")
        title_b = _build_pane_title("claude", "audit", "/tmp/other-project")
        title_c = _build_pane_title("pi", "audit", "/tmp/project")
        title_done = _build_pane_title("claude", "audit", "/tmp/project", state="done")

        assert title_a == title_b  # project_root no longer affects title
        assert title_a == "[claude] audit"
        assert title_a != title_c  # different agent produces different title
        assert title_done == "[claude] audit ok"

    def test_state_icon_maps_expected_states(self) -> None:
        assert _state_icon("active") == "~"
        assert _state_icon("done") == "ok"
        assert _state_icon("merged") == "+"
        assert _state_icon("timed_out") == "!"
        assert _state_icon("failed") == "X"
        assert _state_icon("reviewed_pass") == ""

    def testupdate_pane_state_writes_state_and_updates_tmux(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "task-1",
                        "pane_id": "%2",
                        "agent": "claude",
                        "state": "active",
                    }
                ]
            },
        )

        update_pane_state(str(tmp_path), "task-1", "done")

        panes = all_panes(str(tmp_path))
        assert panes[0]["state"] == "done"
        mock_backend.set_title.assert_called_once_with("%2", "[claude] task-1 ok")

    def test_count_active_agent_workers_only_counts_live_panes(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.status import _count_active_agent_workers

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {"slug": "pi-live", "agent": "pi", "pane_id": "%1"},
                    {"slug": "pi-dead", "agent": "pi", "pane_id": "%2"},
                    {"slug": "claude-live", "agent": "claude", "pane_id": "%3"},
                ]
            },
        )

        mock_backend.bulk_info.return_value = {
            "%1": {"title": "pi-live", "current_command": "pi"},
            "%3": {"title": "claude-live", "current_command": "claude"},
        }
        assert _count_active_agent_workers(str(tmp_path), "pi") == 1
        assert _count_active_agent_workers(str(tmp_path), "claude") == 1
