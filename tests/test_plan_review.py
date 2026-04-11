"""Tests for dgov plan_review — pure data layer for the review CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import pytest

from dgov.plan_review import (
    DiffStat,
    PlanReview,
    UnitReview,
    _build_unit_review,
    _find_run_start_id,
    _rollup_unit_events,
    load_review,
    synthesize_hint,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeDeploy:
    """Stand-in for deploy_log.DeployRecord that doesn't force an import chain."""

    plan: str
    unit: str
    sha: str
    ts: str


def _worker_log(
    event_id: int,
    task_slug: str,
    log_type: str,
    content,
    ts: str = "2026-04-10T12:00:00+00:00",
) -> dict:
    return {
        "id": event_id,
        "ts": ts,
        "event": "worker_log",
        "pane": f"pane-{task_slug}",
        "task_slug": task_slug,
        "log_type": log_type,
        "content": content,
    }


def _lifecycle(
    event_id: int,
    event: str,
    task_slug: str,
    plan_name: str,
    ts: str = "2026-04-10T12:00:00+00:00",
    **extra,
) -> dict:
    base = {
        "id": event_id,
        "ts": ts,
        "event": event,
        "pane": f"pane-{task_slug}",
        "task_slug": task_slug,
        "plan_name": plan_name,
    }
    base.update(extra)
    return base


def _make_compiled(tmp_path: Path, name: str, tasks: dict[str, dict]) -> Path:
    """Write a minimal _compiled.toml into a plan directory."""
    plan_dir = tmp_path / ".dgov" / "plans" / name
    plan_dir.mkdir(parents=True)
    lines = [
        "[plan]",
        f'name = "{name}"',
        "",
    ]
    for uid, data in tasks.items():
        summary = data.get("summary", "")
        lines.append(f'[tasks."{uid}"]')
        lines.append(f'summary = "{summary}"')
        lines.append("")
    compiled_path = plan_dir / "_compiled.toml"
    compiled_path.write_text("\n".join(lines))
    return compiled_path


# ---------------------------------------------------------------------------
# synthesize_hint
# ---------------------------------------------------------------------------


class TestSynthesizeHint:
    def test_iteration_budget_takes_priority(self):
        hint = synthesize_hint(
            verdict="scope_violation",
            error="unclaimed",
            iterations=30,
            iteration_budget=30,
        )
        assert hint is not None
        assert "30-iteration budget" in hint
        assert "split it" in hint

    def test_scope_violation_with_error_mentions_split(self):
        hint = synthesize_hint(
            verdict="scope_violation",
            error="worker touched: src/other.py",
            iterations=12,
            iteration_budget=30,
        )
        assert hint is not None
        assert "files.edit" in hint
        assert "split" in hint

    def test_scope_violation_without_error(self):
        hint = synthesize_hint(
            verdict="scope_violation",
            error=None,
            iterations=5,
            iteration_budget=30,
        )
        assert hint is not None
        assert "files.edit" in hint
        assert "split" not in hint

    def test_empty_diff_hint(self):
        hint = synthesize_hint(
            verdict="empty_diff",
            error=None,
            iterations=3,
            iteration_budget=30,
        )
        assert hint is not None
        assert "no changes" in hint
        assert "Orient/Edit/Verify" in hint

    def test_lint_fail_hint(self):
        hint = synthesize_hint("lint_fail", None, 10, 30)
        assert hint is not None
        assert "autofix" in hint

    def test_unknown_verdict_returns_none(self):
        assert synthesize_hint("mystery", None, 10, 30) is None

    def test_no_verdict_and_budget_not_hit_returns_none(self):
        assert synthesize_hint(None, None, 10, 30) is None


# ---------------------------------------------------------------------------
# _rollup_unit_events
# ---------------------------------------------------------------------------


class TestRollupUnitEvents:
    def test_counts_tool_calls_as_iterations(self):
        events = [
            _worker_log(1, "t", "call", {"tool": "read_file", "args": {}}),
            _worker_log(2, "t", "call", {"tool": "edit_file", "args": {}}),
            _worker_log(3, "t", "call", {"tool": "done", "args": {}}),
        ]
        rollup = _rollup_unit_events(events)
        assert rollup["iterations"] == 3

    def test_collects_thoughts_in_order(self):
        events = [
            _worker_log(1, "t", "thought", "first"),
            _worker_log(2, "t", "thought", "second"),
            _worker_log(3, "t", "thought", "third"),
        ]
        rollup = _rollup_unit_events(events)
        assert rollup["thoughts"] == ["first", "second", "third"]

    def test_extracts_done_summary(self):
        events = [
            _worker_log(1, "t", "call", {"tool": "edit_file"}),
            _worker_log(2, "t", "done", "Applied the fix in 3 places."),
        ]
        rollup = _rollup_unit_events(events)
        assert rollup["done_summary"] == "Applied the fix in 3 places."

    def test_extracts_reject_verdict_from_review_fail(self):
        events = [
            _lifecycle(1, "dag_task_dispatched", "t", "plan"),
            _lifecycle(
                2,
                "review_fail",
                "t",
                "plan",
                verdict="scope_violation",
                error="touched src/other.py",
            ),
            _lifecycle(3, "task_merge_failed", "t", "plan"),
        ]
        rollup = _rollup_unit_events(events)
        assert rollup["reject_verdict"] == "scope_violation"
        assert "src/other.py" in rollup["error"]

    def test_counts_settlement_retries(self):
        events = [
            _lifecycle(1, "dag_task_dispatched", "t", "plan"),
            _lifecycle(2, "settlement_retry", "t", "plan"),
            _lifecycle(3, "settlement_retry", "t", "plan"),
            _lifecycle(4, "merge_completed", "t", "plan"),
        ]
        rollup = _rollup_unit_events(events)
        assert rollup["settlement_retries"] == 2

    def test_duration_from_dispatch_to_terminal(self):
        events = [
            _lifecycle(1, "dag_task_dispatched", "t", "plan", ts="2026-04-10T12:00:00+00:00"),
            _lifecycle(2, "merge_completed", "t", "plan", ts="2026-04-10T12:00:45+00:00"),
        ]
        rollup = _rollup_unit_events(events)
        assert rollup["duration_s"] == pytest.approx(45.0)

    def test_empty_events_yields_none_iterations(self):
        rollup = _rollup_unit_events([])
        assert rollup["iterations"] is None
        assert rollup["thoughts"] == []
        assert rollup["duration_s"] is None
        assert rollup["failed_tool_calls"] == 0

    def test_counts_failed_tool_results(self):
        events = [
            _worker_log(1, "t", "call", {"tool": "run_tests", "args": {}}),
            _worker_log(2, "t", "result", {"tool": "run_tests", "status": "failed"}),
            _worker_log(3, "t", "call", {"tool": "edit_file", "args": {}}),
            _worker_log(4, "t", "result", {"tool": "edit_file", "status": "success"}),
            _worker_log(5, "t", "call", {"tool": "run_tests", "args": {}}),
            _worker_log(6, "t", "result", {"tool": "run_tests", "status": "success"}),
        ]
        rollup = _rollup_unit_events(events)
        assert rollup["failed_tool_calls"] == 1

    def test_ignores_non_dict_result_content(self):
        events = [
            _worker_log(1, "t", "result", "not a dict"),
            _worker_log(2, "t", "result", {"tool": "x", "status": "success"}),
        ]
        rollup = _rollup_unit_events(events)
        assert rollup["failed_tool_calls"] == 0


# ---------------------------------------------------------------------------
# _find_run_start_id
# ---------------------------------------------------------------------------


class TestFindRunStartId:
    def test_returns_latest_when_multiple(self):
        events = [
            {"id": 10, "event": "run_start", "plan_name": "p"},
            {"id": 50, "event": "worker_log", "plan_name": None},
            {"id": 100, "event": "run_start", "plan_name": "p"},
            {"id": 150, "event": "run_start", "plan_name": "other"},
        ]
        assert _find_run_start_id(events, "p") == 100

    def test_returns_zero_when_absent(self):
        events = [{"id": 1, "event": "worker_log"}]
        assert _find_run_start_id(events, "p") == 0

    def test_ignores_other_plans(self):
        events = [
            {"id": 5, "event": "run_start", "plan_name": "other"},
            {"id": 10, "event": "run_start", "plan_name": "mine"},
        ]
        assert _find_run_start_id(events, "mine") == 10


# ---------------------------------------------------------------------------
# _build_unit_review — the assembly layer
# ---------------------------------------------------------------------------


class TestBuildUnitReview:
    def test_deployed_unit_populates_commit_info(self, tmp_path: Path):
        deploy = _FakeDeploy(plan="p", unit="t", sha="abcd1234", ts="2026-04-10T12:00:00Z")
        events = [
            _worker_log(10, "t", "call", {"tool": "edit_file"}),
            _worker_log(11, "t", "done", "did the thing"),
            _lifecycle(12, "merge_completed", "t", "p", merge_sha="abcd1234"),
        ]
        with (
            patch("dgov.plan_review._git_show_message", return_value="feat: did it"),
            patch(
                "dgov.plan_review._git_show_stat",
                return_value=DiffStat(files_changed=2, insertions=40, deletions=5),
            ),
        ):
            review = _build_unit_review(
                unit_id="t",
                task_data={"summary": "do a thing"},
                deploy_record=deploy,
                unit_events=events,
                project_root=str(tmp_path),
                include_full_diff=False,
                iteration_budget=30,
            )
        assert review.status == "deployed"
        assert review.commit_sha == "abcd1234"
        assert review.commit_message == "feat: did it"
        assert review.diff_stat is not None
        assert review.diff_stat.summary() == "2 files, +40 -5"
        assert review.settlement == "ok"
        assert review.attempts == 1
        assert review.iterations == 1
        assert review.done_summary == "did the thing"
        assert review.hint is None

    def test_deployed_with_retry_marks_settlement_retried(self, tmp_path: Path):
        deploy = _FakeDeploy(plan="p", unit="t", sha="abcd1234", ts="2026-04-10T12:00:00Z")
        events = [
            _worker_log(10, "t", "call", {"tool": "edit_file"}),
            _lifecycle(11, "settlement_retry", "t", "p"),
            _worker_log(12, "t", "call", {"tool": "edit_file"}),
            _worker_log(13, "t", "done", "retried cleanly"),
            _lifecycle(14, "merge_completed", "t", "p"),
        ]
        with (
            patch("dgov.plan_review._git_show_message", return_value="feat: x"),
            patch("dgov.plan_review._git_show_stat", return_value=None),
        ):
            review = _build_unit_review(
                unit_id="t",
                task_data={"summary": "x"},
                deploy_record=deploy,
                unit_events=events,
                project_root=str(tmp_path),
                include_full_diff=False,
                iteration_budget=30,
            )
        assert review.settlement == "ok_retried"
        assert review.attempts == 2

    def test_failed_unit_synthesizes_hint(self, tmp_path: Path):
        events = [
            _lifecycle(10, "dag_task_dispatched", "t", "p"),
            _worker_log(11, "t", "call", {"tool": "edit_file"}),
            _worker_log(12, "t", "thought", "Let me also fix an unrelated thing"),
            _lifecycle(
                13,
                "review_fail",
                "t",
                "p",
                verdict="scope_violation",
                error="touched src/other.py",
            ),
            _lifecycle(14, "task_merge_failed", "t", "p"),
        ]
        review = _build_unit_review(
            unit_id="t",
            task_data={"summary": "risky change"},
            deploy_record=None,
            unit_events=events,
            project_root=str(tmp_path),
            include_full_diff=False,
            iteration_budget=30,
        )
        assert review.status == "failed"
        assert review.reject_verdict == "scope_violation"
        assert review.error is not None and "src/other.py" in review.error
        assert review.last_thought == "Let me also fix an unrelated thing"
        assert review.hint is not None
        assert "files.edit" in review.hint

    def test_not_run_unit_has_empty_activity(self, tmp_path: Path):
        review = _build_unit_review(
            unit_id="t",
            task_data={"summary": "pending"},
            deploy_record=None,
            unit_events=[],
            project_root=str(tmp_path),
            include_full_diff=False,
            iteration_budget=30,
        )
        assert review.status == "not_run"
        assert review.attempts == 0
        assert review.iterations is None
        assert review.hint is None

    def test_stale_deploy_with_failed_current_run_shows_failed(self, tmp_path: Path):
        """Regression: a unit that merged in a prior run but failed this run.

        deploy_log is append-only so the old sha persists, but the current
        run should reflect the current outcome. Review was briefly showing
        status='deployed' + a reject_verdict populated on the same unit,
        which is incoherent.
        """
        stale_deploy = _FakeDeploy(plan="p", unit="t", sha="oldsha12", ts="2026-04-09T12:00:00Z")
        # Current run: dispatched, worker called done, review rejected empty_diff
        events = [
            _lifecycle(100, "dag_task_dispatched", "t", "p", ts="2026-04-10T12:00:00+00:00"),
            _worker_log(101, "t", "call", {"tool": "read_file"}),
            _worker_log(102, "t", "done", "Nothing to change"),
            _lifecycle(103, "task_done", "t", "p", ts="2026-04-10T12:00:10+00:00"),
            _lifecycle(
                104,
                "review_fail",
                "t",
                "p",
                verdict="empty_diff",
                error="No changes produced",
                ts="2026-04-10T12:00:11+00:00",
            ),
        ]
        review = _build_unit_review(
            unit_id="t",
            task_data={"summary": "x"},
            deploy_record=stale_deploy,
            unit_events=events,
            project_root=str(tmp_path),
            include_full_diff=False,
            iteration_budget=30,
        )
        assert review.status == "failed"
        assert review.reject_verdict == "empty_diff"
        assert review.hint is not None
        assert "no changes" in review.hint
        # Duration survives review_fail as terminal event
        assert review.duration_s == pytest.approx(11.0)

    def test_terminal_event_review_fail_yields_duration(self, tmp_path: Path):
        """review_fail alone (no merge_completed) should still close the duration."""
        events = [
            _lifecycle(1, "dag_task_dispatched", "t", "p", ts="2026-04-10T12:00:00+00:00"),
            _worker_log(2, "t", "call", {"tool": "edit_file"}),
            _lifecycle(
                3,
                "review_fail",
                "t",
                "p",
                verdict="scope_violation",
                error="touched x",
                ts="2026-04-10T12:00:30+00:00",
            ),
        ]
        review = _build_unit_review(
            unit_id="t",
            task_data={"summary": "x"},
            deploy_record=None,
            unit_events=events,
            project_root=str(tmp_path),
            include_full_diff=False,
            iteration_budget=30,
        )
        assert review.status == "failed"
        assert review.duration_s == pytest.approx(30.0)

    def test_settlement_retry_that_merges_ends_deployed(self, tmp_path: Path):
        """settlement_retry resets failure, then merge_completed lands the unit."""
        deploy = _FakeDeploy(plan="p", unit="t", sha="retrysha", ts="2026-04-10T12:05:00Z")
        events = [
            _lifecycle(1, "dag_task_dispatched", "t", "p"),
            _lifecycle(2, "review_fail", "t", "p", verdict="lint_fail", error="ruff"),
            _lifecycle(3, "settlement_retry", "t", "p"),
            _worker_log(4, "t", "call", {"tool": "edit_file"}),
            _lifecycle(5, "merge_completed", "t", "p", merge_sha="retrysha"),
        ]
        with (
            patch("dgov.plan_review._git_show_message", return_value="feat: retry"),
            patch("dgov.plan_review._git_show_stat", return_value=None),
        ):
            review = _build_unit_review(
                unit_id="t",
                task_data={"summary": "x"},
                deploy_record=deploy,
                unit_events=events,
                project_root=str(tmp_path),
                include_full_diff=False,
                iteration_budget=30,
            )
        assert review.status == "deployed"
        assert review.settlement == "ok_retried"
        assert review.attempts == 2


# ---------------------------------------------------------------------------
# load_review — end-to-end with stubbed event/deploy boundaries
# ---------------------------------------------------------------------------


class TestLoadReview:
    def test_scopes_to_last_run_start(self, tmp_path: Path):
        compiled = _make_compiled(
            tmp_path,
            name="p",
            tasks={"tasks/main.a": {"summary": "do a"}},
        )

        # Two runs: prior run produced events 1-5, current run starts at 100.
        prior_run_events = [
            _lifecycle(1, "run_start", "_", "p"),
            _worker_log(2, "tasks/main.a", "thought", "PRIOR thought"),
            _worker_log(3, "tasks/main.a", "call", {"tool": "edit_file"}),
            _lifecycle(4, "review_fail", "tasks/main.a", "p", verdict="empty_diff", error=""),
            _lifecycle(5, "task_merge_failed", "tasks/main.a", "p"),
        ]
        current_run_events = [
            _lifecycle(100, "run_start", "_", "p"),
            _lifecycle(101, "dag_task_dispatched", "tasks/main.a", "p"),
            _worker_log(102, "tasks/main.a", "thought", "CURRENT thought"),
            _worker_log(103, "tasks/main.a", "call", {"tool": "edit_file"}),
            _worker_log(104, "tasks/main.a", "done", "CURRENT summary"),
            _lifecycle(105, "merge_completed", "tasks/main.a", "p", merge_sha="abc1234"),
        ]
        all_events = prior_run_events + current_run_events

        def _fake_read_events(session_root, **kwargs):
            plan_name = kwargs.get("plan_name")
            task_slug = kwargs.get("task_slug")
            after_id = kwargs.get("after_id", 0)
            filtered = all_events
            if plan_name is not None:
                filtered = [ev for ev in filtered if ev.get("plan_name") == plan_name]
            if task_slug is not None:
                filtered = [ev for ev in filtered if ev.get("task_slug") == task_slug]
            return [ev for ev in filtered if ev.get("id", 0) > after_id]

        deploy = _FakeDeploy(
            plan="p", unit="tasks/main.a", sha="abc1234", ts="2026-04-10T12:05:00Z"
        )

        with (
            patch("dgov.plan_review.read_events", side_effect=_fake_read_events),
            patch("dgov.plan_review.read_deploy_log", return_value=[deploy]),
            patch("dgov.plan_review._git_show_message", return_value="feat: did a"),
            patch("dgov.plan_review._git_show_stat", return_value=None),
        ):
            review = load_review(
                project_root=str(tmp_path),
                compiled_path=compiled,
                iteration_budget=30,
            )

        assert isinstance(review, PlanReview)
        assert review.plan_name == "p"
        assert len(review.units) == 1
        unit = review.units[0]
        assert unit.status == "deployed"
        # Only CURRENT-run thoughts survive the run_start scoping
        assert "CURRENT thought" in unit.thoughts
        assert "PRIOR thought" not in unit.thoughts
        assert unit.done_summary == "CURRENT summary"

    def test_only_filters_to_single_unit(self, tmp_path: Path):
        compiled = _make_compiled(
            tmp_path,
            name="p",
            tasks={
                "tasks/main.a": {"summary": "do a"},
                "tasks/main.b": {"summary": "do b"},
            },
        )

        with (
            patch("dgov.plan_review.read_events", return_value=[]),
            patch("dgov.plan_review.read_deploy_log", return_value=[]),
        ):
            review = load_review(
                project_root=str(tmp_path),
                compiled_path=compiled,
                only="tasks/main.b",
                iteration_budget=30,
            )
        assert len(review.units) == 1
        assert review.units[0].unit == "tasks/main.b"

    def test_missing_compiled_returns_unknown(self, tmp_path: Path):
        review = load_review(
            project_root=str(tmp_path),
            compiled_path=tmp_path / "missing.toml",
            iteration_budget=30,
        )
        assert review.plan_name == "(unknown)"
        assert review.units == []


# ---------------------------------------------------------------------------
# PlanReview properties
# ---------------------------------------------------------------------------


class TestPlanReviewCounts:
    def test_counts_by_status(self):
        units = [
            UnitReview(unit="a", summary="", status="deployed"),
            UnitReview(unit="b", summary="", status="deployed"),
            UnitReview(unit="c", summary="", status="failed"),
            UnitReview(unit="d", summary="", status="pending"),
            UnitReview(unit="e", summary="", status="not_run"),
        ]
        review = PlanReview(
            plan_name="p",
            source_dir=None,
            last_run_ts=None,
            last_run_duration_s=None,
            units=units,
        )
        assert review.deployed_count == 2
        assert review.failed_count == 1
        assert review.pending_count == 2


class TestDiffStatSummary:
    def test_singular_file(self):
        assert DiffStat(files_changed=1, insertions=10, deletions=0).summary() == "1 file, +10 -0"

    def test_plural_files(self):
        assert DiffStat(files_changed=3, insertions=40, deletions=5).summary() == "3 files, +40 -5"
