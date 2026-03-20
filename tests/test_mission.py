"""Tests for the mission primitive."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from dgov.mission import MissionPolicy, _has_blocking_findings, run_mission
from dgov.waiter import PaneTimeoutError


@dataclass
class _FakePane:
    slug: str = "test-slug"
    pane_id: str = "%0"
    prompt: str = "do stuff"
    agent: str = "claude"
    project_root: str = "/repo"
    worktree_path: str = "/repo/.dgov/worktrees/test-slug"
    branch_name: str = "test-slug"
    owns_worktree: bool = True
    base_sha: str = "abc123"


class _FakePreflight:
    passed: bool = True
    checks: list = []


class _FakePreflightFail:
    passed: bool = False

    @dataclass
    class _Check:
        passed: bool = False
        critical: bool = True
        message: str = "agent not found"

    checks = [_Check()]


# Patch targets
_P = "dgov.mission."


def _patches():
    """Return a dict of common mock patches for run_mission."""
    return {
        "create_worker_pane": MagicMock(return_value=_FakePane()),
        "wait_worker_pane": MagicMock(return_value={"done": "test-slug", "method": "signal"}),
        "review_worker_pane": MagicMock(
            return_value={"slug": "test-slug", "verdict": "safe", "commit_count": 1}
        ),
        "merge_worker_pane": MagicMock(
            return_value={"merged": "test-slug", "branch": "test-slug"}
        ),
        "close_worker_pane": MagicMock(return_value=True),
        "emit_event": MagicMock(),
        "run_preflight": MagicMock(return_value=_FakePreflight()),
        "_generate_slug": MagicMock(return_value="test-slug"),
    }


def _apply_patches(monkeypatch, overrides=None):
    """Apply all patches and return the mocks dict."""
    mocks = _patches()
    if overrides:
        mocks.update(overrides)

    # Patch at source modules (run_mission uses local imports)
    targets = {
        "create_worker_pane": "dgov.lifecycle.create_worker_pane",
        "wait_worker_pane": "dgov.waiter.wait_worker_pane",
        "review_worker_pane": "dgov.inspection.review_worker_pane",
        "merge_worker_pane": "dgov.merger.merge_worker_pane",
        "close_worker_pane": "dgov.lifecycle.close_worker_pane",
        "emit_event": "dgov.mission.emit_event",
        "run_preflight": "dgov.preflight.run_preflight",
        "_generate_slug": "dgov.strategy._generate_slug",
    }
    for key, target in targets.items():
        monkeypatch.setattr(target, mocks[key])

    return mocks


@pytest.mark.unit
class TestHappyPath:
    def test_create_wait_review_merge_complete(self, monkeypatch, tmp_path):
        mocks = _apply_patches(monkeypatch)
        result = run_mission(str(tmp_path), "fix the bug", slug="test-slug")

        assert result.state == "completed"
        assert result.slug == "test-slug"
        assert result.merge_result is not None
        assert result.error is None
        mocks["create_worker_pane"].assert_called_once()
        mocks["wait_worker_pane"].assert_called_once()
        mocks["review_worker_pane"].assert_called_once()
        mocks["merge_worker_pane"].assert_called_once()

    def test_emits_lifecycle_events(self, monkeypatch, tmp_path):
        mocks = _apply_patches(monkeypatch)
        run_mission(str(tmp_path), "fix the bug", slug="test-slug")

        event_calls = [c.args[1] for c in mocks["emit_event"].call_args_list]
        assert "mission_pending" in event_calls
        assert "mission_running" in event_calls
        assert "mission_waiting" in event_calls
        assert "mission_reviewing" in event_calls
        assert "mission_merging" in event_calls
        assert "mission_completed" in event_calls

    def test_preflight_uses_prompt_derived_touches(self, monkeypatch, tmp_path):
        mocks = _apply_patches(monkeypatch)
        extract_context = MagicMock(
            return_value={
                "primary_files": ["src/dgov/merger.py"],
                "also_check": ["src/dgov/inspection.py"],
                "tests": ["tests/test_merger_coverage.py"],
                "hints": [],
            }
        )
        monkeypatch.setattr("dgov.strategy.extract_task_context", extract_context)

        run_mission(str(tmp_path), "fix the bug", slug="test-slug")

        mocks["run_preflight"].assert_called_once_with(
            project_root=str(tmp_path),
            agent="claude",
            touches=[
                "src/dgov/merger.py",
                "src/dgov/inspection.py",
                "tests/test_merger_coverage.py",
            ],
            expected_branch=None,
            session_root=str(tmp_path),
            skip_deps=True,
        )

    def test_preflight_prefers_explicit_touches(self, monkeypatch, tmp_path):
        mocks = _apply_patches(monkeypatch)
        policy = MissionPolicy(touches=("src/exact.py", "tests/test_exact.py"))

        run_mission(str(tmp_path), "fix the bug", policy, slug="test-slug")

        mocks["run_preflight"].assert_called_once_with(
            project_root=str(tmp_path),
            agent="claude",
            touches=["src/exact.py", "tests/test_exact.py"],
            expected_branch=None,
            session_root=str(tmp_path),
            skip_deps=True,
        )


@pytest.mark.unit
class TestReviewPending:
    def test_safe_review_without_auto_merge_returns_reviewed_pass(self, monkeypatch, tmp_path):
        mocks = _apply_patches(monkeypatch)
        policy = MissionPolicy(auto_merge=False)

        result = run_mission(str(tmp_path), "fix the bug", policy, slug="test-slug")

        assert result.state == "reviewed_pass"
        assert result.findings is None
        mocks["merge_worker_pane"].assert_not_called()

    def test_review_issues_no_auto_merge(self, monkeypatch, tmp_path):
        review_with_issues = MagicMock(
            return_value={
                "slug": "test-slug",
                "verdict": "review",
                "issues": ["protected files touched: ['CLAUDE.md']"],
            }
        )
        mocks = _apply_patches(monkeypatch, {"review_worker_pane": review_with_issues})
        policy = MissionPolicy(auto_merge=False)
        result = run_mission(str(tmp_path), "fix the bug", policy, slug="test-slug")

        assert result.state == "review_pending"
        assert result.findings is not None
        mocks["merge_worker_pane"].assert_not_called()

    def test_review_issues_auto_merge_proceeds(self, monkeypatch, tmp_path):
        review_with_issues = MagicMock(
            return_value={
                "slug": "test-slug",
                "verdict": "review",
                "issues": ["uncommitted changes"],
            }
        )
        mocks = _apply_patches(monkeypatch, {"review_worker_pane": review_with_issues})
        policy = MissionPolicy(auto_merge=True)
        result = run_mission(str(tmp_path), "fix the bug", policy, slug="test-slug")

        assert result.state == "review_pending"
        mocks["merge_worker_pane"].assert_not_called()


@pytest.mark.unit
class TestTimeout:
    def test_timeout_with_retry(self, monkeypatch, tmp_path):
        call_count = {"n": 0}

        def _wait_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise PaneTimeoutError("test-slug", 600, "claude")
            return {"done": "test-slug-2", "method": "signal"}

        retry_mock = MagicMock(return_value={"retried": True, "new_slug": "test-slug-2"})
        _apply_patches(
            monkeypatch,
            {
                "wait_worker_pane": MagicMock(side_effect=_wait_side_effect),
            },
        )
        monkeypatch.setattr("dgov.recovery.retry_worker_pane", retry_mock)

        policy = MissionPolicy(max_retries=1, timeout=10)
        result = run_mission(str(tmp_path), "fix the bug", policy, slug="test-slug")

        assert result.state == "completed"
        retry_mock.assert_called_once()

    def test_timeout_with_escalation(self, monkeypatch, tmp_path):
        call_count = {"n": 0}

        def _wait_side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise PaneTimeoutError("test-slug", 600, "pi")
            return {"done": "test-slug-esc-1", "method": "signal"}

        escalate_mock = MagicMock(
            return_value={
                "escalated": True,
                "new_slug": "test-slug-esc-1",
                "original_slug": "test-slug",
            }
        )
        _apply_patches(
            monkeypatch,
            {
                "wait_worker_pane": MagicMock(side_effect=_wait_side_effect),
            },
        )
        monkeypatch.setattr("dgov.recovery.escalate_worker_pane", escalate_mock)

        policy = MissionPolicy(agent="pi", max_retries=1, escalate_to="claude", timeout=10)
        result = run_mission(str(tmp_path), "fix the bug", policy, slug="test-slug")

        assert result.state == "completed"
        escalate_mock.assert_called_once()

    def test_timeout_exhausted(self, monkeypatch, tmp_path):
        mocks = _apply_patches(
            monkeypatch,
            {
                "wait_worker_pane": MagicMock(
                    side_effect=PaneTimeoutError("test-slug", 600, "claude")
                ),
            },
        )
        policy = MissionPolicy(max_retries=0, timeout=10)
        result = run_mission(str(tmp_path), "fix the bug", policy, slug="test-slug")

        assert result.state == "failed"
        assert "timed out" in result.error
        mocks["close_worker_pane"].assert_called()

    def test_worker_failed_cleanup_is_executor_owned(self, monkeypatch, tmp_path):
        mocks = _apply_patches(monkeypatch)
        monkeypatch.setattr(
            "dgov.persistence.get_pane",
            MagicMock(return_value={"slug": "test-slug", "state": "failed"}),
        )

        result = run_mission(str(tmp_path), "fix the bug", slug="test-slug")

        assert result.state == "failed"
        assert "Worker exited with an error" in result.error
        mocks["close_worker_pane"].assert_called_once_with(
            str(tmp_path),
            "test-slug",
            session_root=str(tmp_path),
            force=True,
        )


@pytest.mark.unit
class TestMergeConflict:
    def test_merge_conflict_returns_failed(self, monkeypatch, tmp_path):
        mocks = _apply_patches(
            monkeypatch,
            {
                "merge_worker_pane": MagicMock(
                    return_value={
                        "error": "Merge conflict in test-slug",
                        "conflicts": ["src/foo.py"],
                    }
                ),
            },
        )
        result = run_mission(str(tmp_path), "fix the bug", slug="test-slug")

        assert result.state == "failed"
        assert "Merge failed" in result.error
        mocks["close_worker_pane"].assert_not_called()


@pytest.mark.unit
class TestCustomSlug:
    def test_custom_slug_used(self, monkeypatch, tmp_path):
        mocks = _apply_patches(monkeypatch)
        run_mission(str(tmp_path), "fix the bug", slug="my-custom-slug")

        create_call = mocks["create_worker_pane"].call_args
        assert create_call.kwargs.get("slug") == "my-custom-slug"


@pytest.mark.unit
class TestDefaultPolicy:
    def test_default_policy_works(self, monkeypatch, tmp_path):
        _apply_patches(monkeypatch)
        result = run_mission(str(tmp_path), "fix the bug", slug="test-slug")

        assert result.state == "completed"
        assert result.duration_s >= 0


@pytest.mark.unit
class TestPreflightFails:
    def test_preflight_failure_returns_failed(self, monkeypatch, tmp_path):
        mocks = _apply_patches(
            monkeypatch,
            {"run_preflight": MagicMock(return_value=_FakePreflightFail())},
        )
        result = run_mission(str(tmp_path), "fix the bug", slug="test-slug")

        assert result.state == "failed"
        assert "Preflight failed" in result.error
        mocks["create_worker_pane"].assert_not_called()


@pytest.mark.unit
class TestBlockingFindings:
    @pytest.mark.unit
    def test_critical_blocks(self):
        findings = [{"severity": "critical", "description": "bug"}]
        assert _has_blocking_findings(findings, "medium") is True

    @pytest.mark.unit
    def test_medium_blocks_at_medium(self):
        findings = [{"severity": "medium", "description": "style"}]
        assert _has_blocking_findings(findings, "medium") is True

    @pytest.mark.unit
    @pytest.mark.unit
    def test_low_does_not_block_at_medium(self):
        findings = [{"severity": "low", "description": "nit"}]
        assert _has_blocking_findings(findings, "medium") is False

    @pytest.mark.unit
    def test_empty_does_not_block(self):
        assert _has_blocking_findings([], "medium") is False


@pytest.mark.unit
class TestMissionCLI:
    """Smoke tests for the mission CLI command."""

    def test_mission_cmd_happy_path(self, monkeypatch, tmp_path):
        """Mission CLI outputs JSON and colored summary."""
        from click.testing import CliRunner

        from dgov.cli.mission_cmd import mission_cmd
        from dgov.mission import MissionResult

        fake_result = MissionResult(state="completed", slug="test-slug", duration_s=42.5)

        # Patch at the source where it's imported (dgov.mission)
        monkeypatch.setattr("dgov.mission.run_mission", lambda *a, **kw: fake_result)

        runner = CliRunner()
        result = runner.invoke(mission_cmd, ["test prompt", "-r", str(tmp_path)])
        assert result.exit_code == 0
        assert "test-slug" in result.output
        assert "completed" in result.output

    def test_mission_cmd_failed(self, monkeypatch, tmp_path):
        """Mission CLI shows red error on failure."""
        from click.testing import CliRunner

        from dgov.cli.mission_cmd import mission_cmd
        from dgov.mission import MissionResult

        fake_result = MissionResult(state="failed", slug="fail-slug", error="boom")

        # Patch at the source where it's imported (dgov.mission)
        monkeypatch.setattr("dgov.mission.run_mission", lambda *a, **kw: fake_result)

        runner = CliRunner()
        result = runner.invoke(mission_cmd, ["test prompt", "-r", str(tmp_path)])
        assert result.exit_code == 0
        assert "fail-slug" in result.output
        assert "boom" in result.output

    def test_mission_cmd_passes_explicit_touches(self, monkeypatch, tmp_path):
        from click.testing import CliRunner

        from dgov.cli.mission_cmd import mission_cmd
        from dgov.mission import MissionResult

        calls: list[object] = []

        def fake_run_mission(*args, **kwargs):  # noqa: ANN001, ANN201
            calls.append((args, kwargs))
            return MissionResult(state="completed", slug="test-slug", duration_s=1.0)

        monkeypatch.setattr("dgov.mission.run_mission", fake_run_mission)

        runner = CliRunner()
        result = runner.invoke(
            mission_cmd,
            [
                "test prompt",
                "-r",
                str(tmp_path),
                "--touches",
                "src/a.py",
                "--touches",
                "tests/test_a.py",
            ],
        )

        assert result.exit_code == 0
        policy = calls[0][0][2]
        assert policy.touches == ("src/a.py", "tests/test_a.py")
