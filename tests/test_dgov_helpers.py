"""Supplemental unit tests for dgov models and pane helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dgov.models import ConflictDetails, MergeResult, TaskSpec
from dgov.panes import (
    WorkerPane,
    _build_pane_title,
    _emit_event,
    _update_pane_state,
    _validate_state,
    _write_state,
)

pytestmark = pytest.mark.unit


class TestModels:
    def test_task_spec_defaults(self) -> None:
        spec = TaskSpec(
            id="task-1",
            description="Fix the tests",
            exports=["artifact.json"],
            imports=["input.json"],
            touches=["src/app.py"],
            body="Do the work",
        )

        assert spec.after == []
        assert spec.expects_changes is False
        assert spec.permission_mode == "acceptEdits"
        assert spec.timeout is None

    def test_merge_related_dataclasses(self) -> None:
        conflict = ConflictDetails(
            file_path="src/app.py",
            base="base",
            head="head",
            branch="feature/test",
        )
        result = MergeResult(success=False, stderr="boom", conflicts=[conflict])

        assert result.success is False
        assert result.stderr == "boom"
        assert result.conflicts == [conflict]


class TestPaneHelpers:
    def test_emit_event_appends_jsonl_record(self, tmp_path: Path) -> None:
        _emit_event(str(tmp_path), "pane_created", "task-1", agent="claude")

        events_path = tmp_path / ".dgov" / "events.jsonl"
        record = json.loads(events_path.read_text().strip())

        assert record["event"] == "pane_created"
        assert record["pane"] == "task-1"
        assert record["agent"] == "claude"
        assert "ts" in record

    def test_emit_event_rejects_unknown_event(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown event"):
            _emit_event(str(tmp_path), "dmux_spawned", "task-1")

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
        title_a = _build_pane_title("audit", "/tmp/project")
        title_b = _build_pane_title("audit", "/tmp/project")
        title_c = _build_pane_title("audit", "/tmp/other-project")

        assert title_a == title_b
        assert title_a.startswith("audit@project-")
        assert title_a != title_c

    def test_update_pane_state_writes_state_and_updates_tmux(self, tmp_path: Path) -> None:
        _write_state(
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

        with patch("dgov.panes.tmux.update_pane_status") as mock_update_status:
            _update_pane_state(str(tmp_path), "task-1", "merged")

        from dgov.panes import _read_state

        state = _read_state(str(tmp_path))
        assert state["panes"][0]["state"] == "merged"
        mock_update_status.assert_called_once_with("%2", "claude", "task-1", "merged")

    def test_count_active_agent_workers_only_counts_live_panes(self, tmp_path: Path) -> None:
        from dgov.panes import _count_active_agent_workers

        _write_state(
            str(tmp_path),
            {
                "panes": [
                    {"slug": "pi-live", "agent": "pi", "pane_id": "%1"},
                    {"slug": "pi-dead", "agent": "pi", "pane_id": "%2"},
                    {"slug": "claude-live", "agent": "claude", "pane_id": "%3"},
                ]
            },
        )

        with patch(
            "dgov.panes.tmux.bulk_pane_info",
            return_value={
                "%1": {"title": "pi-live", "current_command": "pi"},
                "%3": {"title": "claude-live", "current_command": "claude"},
            },
        ):
            assert _count_active_agent_workers(str(tmp_path), "pi") == 1
            assert _count_active_agent_workers(str(tmp_path), "claude") == 1
