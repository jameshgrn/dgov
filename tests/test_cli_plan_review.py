"""Tests for `dgov plan review` CLI command."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_json_env():
    os.environ.pop("DGOV_JSON", None)
    yield
    os.environ.pop("DGOV_JSON", None)


@pytest.fixture
def runner():
    return CliRunner()


def _make_compiled_plan(
    project_root: Path, plan_name: str, unit_summaries: dict[str, str]
) -> Path:
    """Write a minimal _compiled.toml into a plan directory under project_root."""
    plan_dir = project_root / ".dgov" / "plans" / plan_name
    plan_dir.mkdir(parents=True)
    lines = [
        "[plan]",
        f'name = "{plan_name}"',
        'source_mtime_max = "2026-04-10T12:00:00.000000+00:00"',
        "",
    ]
    for uid, summary in unit_summaries.items():
        lines.append(f'[tasks."{uid}"]')
        lines.append(f'summary = "{summary}"')
        lines.append('prompt = "do it"')
        lines.append('commit_message = "c"')
        lines.append('files.create = ["a.py"]')
        lines.append("")
    compiled_path = plan_dir / "_compiled.toml"
    compiled_path.write_text("\n".join(lines))
    (plan_dir / "_root.toml").write_text(
        f'[plan]\nname = "{plan_name}"\nsummary = "t"\nsections = ["tasks"]\n'
    )
    return plan_dir


def _patched_load_review(monkeypatch, **overrides):
    """Return a helper that stubs load_review to return a fixed PlanReview."""
    from dgov.plan_review import DiffStat, PlanReview, UnitReview

    default_unit = UnitReview(
        unit="tasks/main.a",
        summary="do a",
        status="deployed",
        agent="kimi",
        commit_sha="abcd1234" + "0" * 32,
        commit_message="feat: did a",
        commit_ts="2026-04-10T12:00:00Z",
        diff_stat=DiffStat(files_changed=1, insertions=10, deletions=0),
        duration_s=12.5,
        iterations=4,
        attempts=1,
        settlement="ok",
        done_summary="Added the thing.",
    )
    default_review = PlanReview(
        plan_name="p",
        source_dir=Path("p"),
        last_run_ts="2026-04-10T12:00:00Z",
        last_run_duration_s=12.5,
        units=[default_unit],
    )
    review = overrides.get("review", default_review)

    def _fake(**kwargs):
        # Honor only-filter semantics for the `only=...` tests.
        only = kwargs.get("only")
        if only is not None:
            filtered = [u for u in review.units if u.unit == only]
            return PlanReview(
                plan_name=review.plan_name,
                source_dir=review.source_dir,
                last_run_ts=review.last_run_ts,
                last_run_duration_s=review.last_run_duration_s,
                units=filtered,
            )
        return review

    monkeypatch.setattr("dgov.plan_review.load_review", _fake)


# -- Happy path: deployed unit --


def test_review_renders_deployed_unit(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "do a"})
    _patched_load_review(monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["plan", "review", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert "Plan: p" in result.output
    assert "1/1 deployed" in result.output
    assert "tasks/main.a" in result.output
    assert "commit       abcd1234" in result.output
    assert "diff         1 file, +10 -0" in result.output
    assert "duration     12.5s" in result.output
    assert "iterations   4 tool calls" in result.output
    assert "settlement   ok (first try)" in result.output
    assert "Added the thing." in result.output


# -- Failed unit with hint --


def test_review_failed_unit_shows_hint_and_exits_nonzero(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import PlanReview, UnitReview

    failed = UnitReview(
        unit="tasks/main.risky",
        summary="a risky change",
        status="failed",
        agent="kimi",
        attempts=1,
        iterations=12,
        duration_s=45.0,
        settlement="rejected",
        reject_verdict="scope_violation",
        error="touched src/other.py",
        last_thought="Let me also fix an unrelated thing",
        hint="worker touched unclaimed files — add them to files.edit",
    )
    review = PlanReview(
        plan_name="p",
        source_dir=None,
        last_run_ts="2026-04-10T12:00:00Z",
        last_run_duration_s=45.0,
        units=[failed],
    )
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.risky": "x"})
    _patched_load_review(monkeypatch, review=review)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["plan", "review", str(plan_dir)])

    assert result.exit_code == 1
    assert "✗" in result.output
    assert "scope_violation" in result.output
    assert "touched src/other.py" in result.output
    assert "Let me also fix an unrelated thing" in result.output
    assert "hint" in result.output
    assert "files.edit" in result.output


# -- JSON output --


def test_review_json_output_is_valid(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "do a"})
    _patched_load_review(monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["--json", "plan", "review", str(plan_dir)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["plan"] == "p"
    assert data["deployed"] == 1
    assert data["failed"] == 0
    assert len(data["units"]) == 1
    unit = data["units"][0]
    assert unit["unit"] == "tasks/main.a"
    assert unit["status"] == "deployed"
    assert unit["diff_stat"]["files_changed"] == 1
    assert unit["settlement"] == "ok"


# -- --only flag --


def test_review_only_filters_to_matching_unit(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import PlanReview, UnitReview

    review = PlanReview(
        plan_name="p",
        source_dir=None,
        last_run_ts="2026-04-10T12:00:00Z",
        last_run_duration_s=10.0,
        units=[
            UnitReview(unit="tasks/main.a", summary="a", status="deployed"),
            UnitReview(unit="tasks/main.b", summary="b", status="not_run"),
        ],
    )
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a", "tasks/main.b": "b"})
    _patched_load_review(monkeypatch, review=review)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["plan", "review", str(plan_dir), "--only", "tasks/main.b"])

    assert result.exit_code == 0, result.output
    assert "tasks/main.b" in result.output
    assert "tasks/main.a" not in result.output


def test_review_only_nonexistent_errors_out(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import PlanReview

    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
    # Stub that returns empty units when filtered
    empty_review = PlanReview(
        plan_name="p",
        source_dir=None,
        last_run_ts=None,
        last_run_duration_s=None,
        units=[],
    )
    _patched_load_review(monkeypatch, review=empty_review)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["plan", "review", str(plan_dir), "--only", "tasks/main.missing"])

    assert result.exit_code == 1
    assert "no unit matches --only" in result.output


# -- --diff flag --


def test_review_diff_flag_shows_full_patch(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import DiffStat, PlanReview, UnitReview

    unit = UnitReview(
        unit="tasks/main.a",
        summary="do a",
        status="deployed",
        commit_sha="abcd1234",
        commit_message="feat: did a",
        diff_stat=DiffStat(files_changed=1, insertions=2, deletions=1),
        full_diff="diff --git a/a.py b/a.py\n+new line\n-old line\n",
        settlement="ok",
        attempts=1,
    )
    review = PlanReview(
        plan_name="p",
        source_dir=None,
        last_run_ts=None,
        last_run_duration_s=None,
        units=[unit],
    )
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
    _patched_load_review(monkeypatch, review=review)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["plan", "review", str(plan_dir), "--diff", "tasks/main.a"])

    assert result.exit_code == 0, result.output
    assert "diff --git a/a.py b/a.py" in result.output
    assert "+new line" in result.output
    assert "-old line" in result.output


# -- --events flag --


def test_review_events_flag_shows_tool_calls_and_thoughts(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import PlanReview, UnitReview

    unit = UnitReview(
        unit="tasks/main.a",
        summary="do a",
        status="deployed",
        settlement="ok",
        attempts=1,
        thoughts=("I will start by reading.", "Looks good."),
        activity=(
            {"tool": "read_file", "args": {"path": "a.py"}},
            {"tool": "edit_file", "args": {"path": "a.py", "old_text": "x"}},
        ),
    )
    review = PlanReview(
        plan_name="p",
        source_dir=None,
        last_run_ts=None,
        last_run_duration_s=None,
        units=[unit],
    )
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
    _patched_load_review(monkeypatch, review=review)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["plan", "review", str(plan_dir), "--events", "tasks/main.a"])

    assert result.exit_code == 0, result.output
    assert "activity for tasks/main.a" in result.output
    assert "read_file(" in result.output
    assert "edit_file(" in result.output
    assert "I will start by reading." in result.output


# -- --diff + --events combinable --


def test_review_diff_and_events_combinable(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import PlanReview, UnitReview

    unit = UnitReview(
        unit="tasks/main.a",
        summary="do a",
        status="deployed",
        commit_sha="abcd1234",
        settlement="ok",
        attempts=1,
        full_diff="diff --git a/a.py b/a.py\n+new\n",
        activity=({"tool": "read_file", "args": {}},),
    )
    review = PlanReview(
        plan_name="p",
        source_dir=None,
        last_run_ts=None,
        last_run_duration_s=None,
        units=[unit],
    )
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
    _patched_load_review(monkeypatch, review=review)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        cli,
        [
            "plan",
            "review",
            str(plan_dir),
            "--diff",
            "tasks/main.a",
            "--events",
            "tasks/main.a",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "diff --git a/a.py b/a.py" in result.output
    assert "read_file(" in result.output


# -- not_compiled error --


def test_review_not_compiled_exits_nonzero(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = tmp_path / ".dgov" / "plans" / "missing"
    plan_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "missing"\nsummary = ""\nsections = ["tasks"]\n'
    )
    (plan_dir / "tasks").mkdir()
    result = runner.invoke(cli, ["plan", "review", str(plan_dir)])
    assert result.exit_code != 0
    assert "Not compiled" in result.output


# -- Archive fallback + self_corrections --


def test_review_resolves_archived_plan_and_emits_note(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path that was auto-archived after success should redirect with a note."""
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "do a"})
    archive_root = plan_dir.parent / "archive"
    archive_root.mkdir()
    archived_dir = archive_root / plan_dir.name
    plan_dir.rename(archived_dir)

    _patched_load_review(monkeypatch)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["plan", "review", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert "resolved to archived plan" in result.output
    assert str(archived_dir) in result.output
    assert "tasks/main.a" in result.output


def test_review_missing_plan_reports_error(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No live path, no archive → non-zero exit with a helpful error."""
    _patched_load_review(monkeypatch)
    monkeypatch.chdir(tmp_path)
    ghost = tmp_path / ".dgov" / "plans" / "ghost"

    result = runner.invoke(cli, ["plan", "review", str(ghost)])

    assert result.exit_code != 0
    assert "plan path not found" in result.output


def test_review_renders_self_corrections_for_deployed_unit(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import DiffStat, PlanReview, UnitReview

    unit = UnitReview(
        unit="tasks/main.a",
        summary="do a",
        status="deployed",
        commit_sha="abcd1234",
        commit_message="feat: did a",
        diff_stat=DiffStat(files_changed=1, insertions=2, deletions=0),
        iterations=7,
        self_corrections=2,
        attempts=1,
        settlement="ok",
    )
    review = PlanReview(
        plan_name="p",
        source_dir=None,
        last_run_ts=None,
        last_run_duration_s=None,
        units=[unit],
    )
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
    _patched_load_review(monkeypatch, review=review)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["plan", "review", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert "self-correct" in result.output
    assert "2 failed tool call" in result.output


def test_review_json_includes_self_corrections(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dgov.plan_review import PlanReview, UnitReview

    unit = UnitReview(
        unit="tasks/main.a",
        summary="do a",
        status="deployed",
        self_corrections=3,
    )
    review = PlanReview(
        plan_name="p",
        source_dir=None,
        last_run_ts=None,
        last_run_duration_s=None,
        units=[unit],
    )
    plan_dir = _make_compiled_plan(tmp_path, "p", {"tasks/main.a": "a"})
    _patched_load_review(monkeypatch, review=review)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(cli, ["--json", "plan", "review", str(plan_dir)])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["units"][0]["self_corrections"] == 3
