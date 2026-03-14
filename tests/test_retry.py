"""Tests for dgov.retry — auto-retry engine."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.persistence import (
    _STATE_DIR,
    WorkerPane,
    _add_pane,
    _emit_event,
    _set_pane_metadata,
)
from dgov.retry import (
    RetryPolicy,
    _count_retries,
    get_retry_policy,
    maybe_auto_retry,
    retry_context,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def skip_governor_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")


def _setup_pane(
    tmp_path: Path,
    slug: str = "test-worker",
    state: str = "failed",
    agent: str = "claude",
    prompt: str = "do stuff",
) -> str:
    session_root = str(tmp_path)
    pane = WorkerPane(
        slug=slug,
        prompt=prompt,
        pane_id="%99",
        agent=agent,
        project_root="/fake/project",
        worktree_path=str(tmp_path / "wt" / slug),
        branch_name=slug,
        state=state,
    )
    _add_pane(session_root, pane)
    return session_root


# ---------------------------------------------------------------------------
# RetryPolicy defaults
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    def test_defaults(self) -> None:
        p = RetryPolicy()
        assert p.max_retries == 0
        assert p.escalate_to is None
        assert p.backoff_base == 5.0

    def test_custom(self) -> None:
        p = RetryPolicy(max_retries=3, escalate_to="claude", backoff_base=2.0)
        assert p.max_retries == 3
        assert p.escalate_to == "claude"
        assert p.backoff_base == 2.0


# ---------------------------------------------------------------------------
# retry_context
# ---------------------------------------------------------------------------


class TestRetryContext:
    def test_builds_context_from_log(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        slug = "test-worker"

        # Create a fake log file
        log_dir = tmp_path / _STATE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{slug}.log"
        lines = [f"line {i}" for i in range(30)]
        log_file.write_text("\n".join(lines))

        ctx = retry_context(slug, session_root)
        assert "Last output:" in ctx
        # Should have last 20 lines
        assert "line 29" in ctx
        assert "line 10" in ctx
        # line 9 should not be in the tail
        assert "line 9" not in ctx

    def test_includes_exit_code(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        slug = "test-worker"

        done_dir = tmp_path / _STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / f"{slug}.exit").write_text("1")

        ctx = retry_context(slug, session_root)
        assert "Exit code: 1" in ctx

    def test_includes_events(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        slug = "test-worker"

        _emit_event(session_root, "pane_created", slug, agent="claude")
        _emit_event(session_root, "pane_done", slug)

        ctx = retry_context(slug, session_root)
        assert "Recent events:" in ctx
        assert "pane_created" in ctx

    def test_empty_for_missing_pane(self, tmp_path: Path) -> None:
        ctx = retry_context("nonexistent", str(tmp_path))
        assert ctx == ""


# ---------------------------------------------------------------------------
# _count_retries
# ---------------------------------------------------------------------------


class TestCountRetries:
    def test_zero_with_no_events(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        assert _count_retries(session_root, "test-worker") == 0

    def test_counts_auto_retried_events(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        slug = "test-worker"

        _emit_event(session_root, "pane_auto_retried", slug, attempt=1)
        _emit_event(session_root, "pane_auto_retried", slug, attempt=2)
        # Different slug — should not count
        _emit_event(session_root, "pane_auto_retried", "other-worker", attempt=1)

        assert _count_retries(session_root, slug) == 2


# ---------------------------------------------------------------------------
# get_retry_policy
# ---------------------------------------------------------------------------


class TestGetRetryPolicy:
    def test_returns_none_for_no_retries(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path, agent="claude")
        # claude has max_retries=0 by default
        policy = get_retry_policy(session_root, "test-worker")
        assert policy is None

    def test_returns_none_for_missing_pane(self, tmp_path: Path) -> None:
        policy = get_retry_policy(str(tmp_path), "nonexistent")
        assert policy is None

    @patch("dgov.retry.load_registry")
    def test_returns_policy_from_agent(self, mock_registry, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path, agent="test-agent")
        agent_def = MagicMock()
        agent_def.max_retries = 3
        agent_def.retry_escalate_to = "claude"
        mock_registry.return_value = {"test-agent": agent_def}

        policy = get_retry_policy(session_root, "test-worker")
        assert policy is not None
        assert policy.max_retries == 3
        assert policy.escalate_to == "claude"

    @patch("dgov.retry.load_registry")
    def test_pane_override_takes_priority(self, mock_registry, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path, agent="test-agent")

        # Set per-pane override via SQLite metadata
        _set_pane_metadata(session_root, "test-worker", max_retries=5)

        agent_def = MagicMock()
        agent_def.max_retries = 2
        agent_def.retry_escalate_to = None
        mock_registry.return_value = {"test-agent": agent_def}

        policy = get_retry_policy(session_root, "test-worker")
        assert policy is not None
        assert policy.max_retries == 5


# ---------------------------------------------------------------------------
# maybe_auto_retry
# ---------------------------------------------------------------------------


class TestMaybeAutoRetry:
    @patch("dgov.retry.load_registry")
    def test_returns_none_when_no_policy(self, mock_registry, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path, state="failed")
        mock_registry.return_value = {"claude": MagicMock(max_retries=0)}

        result = maybe_auto_retry(session_root, "test-worker", "/fake/project")
        assert result is None

    def test_returns_none_when_not_failed(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path, state="done")
        result = maybe_auto_retry(session_root, "test-worker", "/fake/project")
        assert result is None

    def test_returns_none_for_missing_pane(self, tmp_path: Path) -> None:
        result = maybe_auto_retry(str(tmp_path), "nonexistent", "/fake/project")
        assert result is None

    @patch("dgov.panes.retry_worker_pane")
    @patch("dgov.retry.load_registry")
    @patch("dgov.retry.time.sleep")
    def test_retries_under_max(
        self, mock_sleep, mock_registry, mock_retry, tmp_path: Path
    ) -> None:
        session_root = _setup_pane(tmp_path, state="failed", agent="test-agent")

        agent_def = MagicMock()
        agent_def.max_retries = 2
        agent_def.retry_escalate_to = None
        mock_registry.return_value = {"test-agent": agent_def}

        mock_retry.return_value = {"retried": True, "new_slug": "test-worker-2", "attempt": 2}

        result = maybe_auto_retry(session_root, "test-worker", "/fake/project")
        assert result is not None
        assert result["retried"] == "test-worker"
        assert result["new_slug"] == "test-worker-2"
        assert result["attempt"] == 1
        mock_sleep.assert_called_once()

    @patch("dgov.panes.escalate_worker_pane")
    @patch("dgov.retry.load_registry")
    @patch("dgov.retry.time.sleep")
    def test_escalates_when_exhausted(
        self, mock_sleep, mock_registry, mock_escalate, tmp_path: Path
    ) -> None:
        session_root = _setup_pane(tmp_path, state="failed", agent="test-agent")

        # Pre-fill retry events to exhaust retries
        _emit_event(session_root, "pane_auto_retried", "test-worker", attempt=1)

        agent_def = MagicMock()
        agent_def.max_retries = 1
        agent_def.retry_escalate_to = "claude"
        mock_registry.return_value = {"test-agent": agent_def}

        mock_escalate.return_value = {
            "escalated": True,
            "new_slug": "test-worker-esc",
        }

        result = maybe_auto_retry(session_root, "test-worker", "/fake/project")
        assert result is not None
        assert result["escalated"] == "test-worker"
        assert result["to"] == "claude"

    @patch("dgov.retry.load_registry")
    def test_returns_none_when_exhausted_no_escalation(
        self, mock_registry, tmp_path: Path
    ) -> None:
        session_root = _setup_pane(tmp_path, state="failed", agent="test-agent")

        _emit_event(session_root, "pane_auto_retried", "test-worker", attempt=1)

        agent_def = MagicMock()
        agent_def.max_retries = 1
        agent_def.retry_escalate_to = None
        mock_registry.return_value = {"test-agent": agent_def}

        result = maybe_auto_retry(session_root, "test-worker", "/fake/project")
        assert result is None


# ---------------------------------------------------------------------------
# Integration: wait_worker_pane with auto-retry (mocked)
# ---------------------------------------------------------------------------


class TestWaitWithAutoRetry:
    @patch("dgov.retry.maybe_auto_retry")
    @patch("dgov.panes._is_done")
    @patch("dgov.panes._get_pane")
    @patch("dgov.panes._update_pane_state")
    def test_auto_retry_on_failure(
        self,
        mock_update,
        mock_get_pane,
        mock_is_done,
        mock_maybe_retry,
        tmp_path: Path,
    ) -> None:
        """When _is_done returns True and state is 'failed', auto-retry is invoked."""
        from dgov.waiter import wait_worker_pane

        session_root = str(tmp_path)

        # First call: original pane is done (failed)
        # Second call: retry pane is done (done)
        mock_is_done.side_effect = [True, True]

        # First get_pane: failed state; second: for retry check; third: retried pane done
        mock_get_pane.side_effect = [
            {"slug": "w1", "agent": "pi", "pane_id": "%1", "state": "active"},
            {"slug": "w1", "agent": "pi", "pane_id": "%1", "state": "failed"},
            {"slug": "w1-2", "agent": "pi", "pane_id": "%2", "state": "active"},
            {"slug": "w1-2", "agent": "pi", "pane_id": "%2", "state": "done"},
        ]

        mock_maybe_retry.return_value = {
            "retried": "w1",
            "new_slug": "w1-2",
            "attempt": 1,
        }

        result = wait_worker_pane(
            "/fake",
            "w1",
            session_root=session_root,
            timeout=30,
            poll=0,
            stable=15,
            auto_retry=True,
        )
        assert result["done"] == "w1-2"
        mock_maybe_retry.assert_called_once()

    @patch("dgov.panes._is_done")
    @patch("dgov.panes._get_pane")
    @patch("dgov.panes._update_pane_state")
    def test_no_auto_retry_flag(
        self, mock_update, mock_get_pane, mock_is_done, tmp_path: Path
    ) -> None:
        """With auto_retry=False, failed panes are returned immediately."""
        from dgov.waiter import wait_worker_pane

        session_root = str(tmp_path)
        mock_is_done.return_value = True
        mock_get_pane.return_value = {
            "slug": "w1",
            "agent": "pi",
            "pane_id": "%1",
            "state": "failed",
        }

        result = wait_worker_pane(
            "/fake",
            "w1",
            session_root=session_root,
            timeout=30,
            poll=0,
            stable=15,
            auto_retry=False,
        )
        assert result["done"] == "w1"


# ---------------------------------------------------------------------------
# CLI --no-auto-retry flag
# ---------------------------------------------------------------------------


class TestCLINoAutoRetry:
    @patch("dgov.panes.list_worker_panes", return_value=[{"slug": "w1"}])
    @patch("dgov.panes.wait_worker_pane")
    def test_no_auto_retry_passed(self, mock_wait, mock_list, runner: CliRunner) -> None:
        mock_wait.return_value = {"done": "w1", "method": "signal_or_commit"}

        result = runner.invoke(
            cli,
            ["pane", "wait", "w1", "--no-auto-retry", "-r", "/fake"],
        )
        assert result.exit_code == 0
        mock_wait.assert_called_once()
        call_kwargs = mock_wait.call_args
        assert call_kwargs.kwargs.get("auto_retry") is False or (
            len(call_kwargs.args) > 6 and call_kwargs.args[6] is False
        )

    @patch("dgov.panes.list_worker_panes", return_value=[{"slug": "w1"}])
    @patch("dgov.panes.wait_worker_pane")
    def test_auto_retry_default_on(self, mock_wait, mock_list, runner: CliRunner) -> None:
        mock_wait.return_value = {"done": "w1", "method": "signal_or_commit"}

        result = runner.invoke(cli, ["pane", "wait", "w1", "-r", "/fake"])
        assert result.exit_code == 0
        mock_wait.assert_called_once()
        # auto_retry should default to True
        call_kwargs = mock_wait.call_args
        assert call_kwargs.kwargs.get("auto_retry") is True
