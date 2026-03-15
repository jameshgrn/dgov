"""Tests for dgov.metrics — aggregate statistics."""

from __future__ import annotations

import pytest

from dgov.metrics import compute_stats
from dgov.persistence import WorkerPane, add_pane, emit_event

pytestmark = pytest.mark.unit


@pytest.fixture()
def session_root(tmp_path):
    (tmp_path / ".dgov").mkdir()
    return str(tmp_path)


def _seed(session_root: str, slug: str, agent: str = "claude", state: str = "active") -> None:
    pane = WorkerPane(
        slug=slug,
        prompt="task",
        pane_id="%99",
        agent=agent,
        project_root=session_root,
        worktree_path=f"{session_root}/wt/{slug}",
        branch_name=slug,
        state=state,
    )
    add_pane(session_root, pane)


class TestComputeStatsEmpty:
    def test_compute_stats_empty(self, session_root):
        data = compute_stats(session_root)
        assert data["total_panes"] == 0
        assert data["by_state"] == {}
        assert data["by_agent"] == {}
        assert data["recent_failures"] == []
        assert data["event_count"] == 0


class TestComputeStatsWithPanes:
    def test_compute_stats_with_panes(self, session_root):
        _seed(session_root, "a", state="active")
        _seed(session_root, "b", state="merged")
        _seed(session_root, "c", state="failed")

        data = compute_stats(session_root)
        assert data["total_panes"] == 3
        assert data["by_state"]["active"] == 1
        assert data["by_state"]["merged"] == 1
        assert data["by_state"]["failed"] == 1


class TestComputeStatsByAgent:
    def test_compute_stats_by_agent(self, session_root):
        _seed(session_root, "a1", agent="claude", state="merged")
        _seed(session_root, "a2", agent="claude", state="failed")
        _seed(session_root, "b1", agent="pi", state="merged")
        _seed(session_root, "b2", agent="pi", state="merged")

        # Add events for duration calculation
        emit_event(session_root, "pane_created", "a1")
        emit_event(session_root, "pane_merged", "a1")
        emit_event(session_root, "pane_created", "b1")
        emit_event(session_root, "pane_merged", "b1")

        data = compute_stats(session_root)
        claude = data["by_agent"]["claude"]
        assert claude["total"] == 2
        assert claude["success_rate"] == 0.5
        assert claude["failures"] == 1

        pi = data["by_agent"]["pi"]
        assert pi["total"] == 2
        assert pi["success_rate"] == 1.0
        assert pi["failures"] == 0
        assert pi["avg_duration_s"] is not None


class TestComputeStatsRecentFailures:
    def test_compute_stats_recent_failures(self, session_root):
        for i in range(7):
            slug = f"fail-{i}"
            _seed(session_root, slug, state="failed")
            emit_event(session_root, "pane_created", slug)

        data = compute_stats(session_root)
        assert len(data["recent_failures"]) == 5
        for f in data["recent_failures"]:
            assert "slug" in f
            assert "agent" in f
            assert "state" in f
            assert f["state"] == "failed"
