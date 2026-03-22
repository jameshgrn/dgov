"""Tests for dgov.recovery — auto-retry engine."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.persistence import (
    STATE_DIR,
    WorkerPane,
    add_pane,
    emit_event,
    set_pane_metadata,
)
from dgov.recovery import (
    RetryPolicy,
    _count_retries,
    _detect_provider_failure,
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
    add_pane(session_root, pane)
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
        log_dir = tmp_path / STATE_DIR / "logs"
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

        done_dir = tmp_path / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / f"{slug}.exit").write_text("1")

        ctx = retry_context(slug, session_root)
        assert "Exit code: 1" in ctx

    def test_includes_events(self, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path)
        slug = "test-worker"

        emit_event(session_root, "pane_created", slug, agent="claude")
        emit_event(session_root, "pane_done", slug)

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

        emit_event(session_root, "pane_auto_retried", slug, attempt=1)
        emit_event(session_root, "pane_auto_retried", slug, attempt=2)
        # Different slug — should not count
        emit_event(session_root, "pane_auto_retried", "other-worker", attempt=1)

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

    @patch("dgov.recovery.load_registry")
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

    @patch("dgov.recovery.load_registry")
    def test_pane_override_takes_priority(self, mock_registry, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path, agent="test-agent")

        # Set per-pane override via SQLite metadata
        set_pane_metadata(session_root, "test-worker", max_retries=5)

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
    @patch("dgov.recovery.load_registry")
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

    @patch("dgov.recovery.retry_worker_pane")
    @patch("dgov.recovery.load_registry")
    def test_retries_under_max(self, mock_registry, mock_retry, tmp_path: Path) -> None:
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

    @patch("dgov.recovery.escalate_worker_pane")
    @patch("dgov.recovery.load_registry")
    def test_escalates_when_exhausted(self, mock_registry, mock_escalate, tmp_path: Path) -> None:
        session_root = _setup_pane(tmp_path, state="failed", agent="test-agent")

        # Pre-fill retry events to exhaust retries
        emit_event(session_root, "pane_auto_retried", "test-worker", attempt=1)

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

    @patch("dgov.recovery.load_registry")
    def test_returns_none_when_exhausted_no_escalation(
        self, mock_registry, tmp_path: Path
    ) -> None:
        session_root = _setup_pane(tmp_path, state="failed", agent="test-agent")

        emit_event(session_root, "pane_auto_retried", "test-worker", attempt=1)

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
    @patch("dgov.recovery.maybe_auto_retry")
    @patch("dgov.waiter._is_done")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.persistence.update_pane_state")
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

        # First call: original pane is already in failed state.
        # Second call: retried pane completes successfully.
        mock_is_done.side_effect = [True, True]

        # wait_worker_pane calls get_pane twice per loop iteration: once at the
        # top and once after _is_done returns True (to get the fresh state).
        mock_get_pane.side_effect = [
            {"slug": "w1", "agent": "pi", "pane_id": "%1", "state": "failed"},  # iter 1 top
            {"slug": "w1", "agent": "pi", "pane_id": "%1", "state": "failed"},  # iter 1 re-read
            {"slug": "w1-2", "agent": "pi", "pane_id": "%2", "state": "done"},  # iter 2 top
            {"slug": "w1-2", "agent": "pi", "pane_id": "%2", "state": "done"},  # iter 2 re-read
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

    @patch("dgov.waiter._is_done")
    @patch("dgov.persistence.get_pane")
    @patch("dgov.persistence.update_pane_state")
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
    @patch("dgov.status.list_worker_panes", return_value=[{"slug": "w1"}])
    @patch("dgov.executor.run_wait_only")
    def test_no_auto_retry_passed(self, mock_wait, mock_list, runner: CliRunner) -> None:
        from dgov.executor import WaitOnlyResult

        mock_wait.return_value = WaitOnlyResult(
            state="completed", slug="w1", wait_result={"done": "w1", "method": "signal_or_commit"}
        )

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

    @patch("dgov.status.list_worker_panes", return_value=[{"slug": "w1"}])
    @patch("dgov.executor.run_wait_only")
    def test_auto_retry_default_on(self, mock_wait, mock_list, runner: CliRunner) -> None:
        from dgov.executor import WaitOnlyResult

        mock_wait.return_value = WaitOnlyResult(
            state="completed", slug="w1", wait_result={"done": "w1", "method": "signal_or_commit"}
        )

        result = runner.invoke(cli, ["pane", "wait", "w1", "-r", "/fake"])
        assert result.exit_code == 0
        mock_wait.assert_called_once()
        call_kwargs = mock_wait.call_args
        assert call_kwargs.kwargs.get("auto_retry") is True


# ---------------------------------------------------------------------------
# _detect_provider_failure
# ---------------------------------------------------------------------------


class TestDetectProviderFailure:
    def test_detects_upstream_error_pattern(self) -> None:
        context = "Upstream error from openrouter: connection timeout"
        is_failure, provider = _detect_provider_failure(context)
        assert is_failure is True
        assert provider == "openrouter"

    def test_detects_anthropic_error(self) -> None:
        context = "Anthropic error: rate limit exceeded"
        is_failure, provider = _detect_provider_failure(context)
        assert is_failure is True
        assert provider == "anthropic"

    def test_detects_google_error(self) -> None:
        context = "Google error: service unavailable"
        is_failure, provider = _detect_provider_failure(context)
        assert is_failure is True
        assert provider == "google"

    def test_detects_azure_error(self) -> None:
        context = "Azure error: authentication failed"
        is_failure, provider = _detect_provider_failure(context)
        assert is_failure is True
        assert provider == "azure"

    def test_detects_bedrock_error(self) -> None:
        context = "Bedrock error: model not found"
        is_failure, provider = _detect_provider_failure(context)
        assert is_failure is True
        assert provider == "bedrock"

    def test_detects_rate_limit_pattern(self) -> None:
        context = "rate limit exceeded for OpenRouter"
        is_failure, provider = _detect_provider_failure(context)
        assert is_failure is True
        assert provider == "openrouter"

    def test_detects_connection_refused_with_provider(self) -> None:
        context = "connection refused to provider endpoint (Anthropic)"
        is_failure, provider = _detect_provider_failure(context)
        assert is_failure is True
        assert provider == "anthropic"

    def test_returns_false_for_non_provider_failure(self) -> None:
        context = "SyntaxError: invalid syntax at line 42"
        is_failure, provider = _detect_provider_failure(context)
        assert is_failure is False
        assert provider is None

    def test_returns_false_for_empty_context(self) -> None:
        is_failure, provider = _detect_provider_failure("")
        assert is_failure is False
        assert provider is None

    def test_returns_false_for_none_context(self) -> None:
        is_failure, provider = _detect_provider_failure(None)  # type: ignore
        assert is_failure is False
        assert provider is None


# ---------------------------------------------------------------------------
# maybe_auto_retry with provider failure detection
# ---------------------------------------------------------------------------


class TestMaybeAutoRetryProviderFailure:
    @patch("dgov.recovery.retry_worker_pane")
    @patch("dgov.recovery.retry_context")
    def test_retries_provider_failure_without_policy(
        self, mock_context, mock_retry, tmp_path: Path
    ) -> None:
        """Provider/runtime failures get one retry even without explicit policy."""
        session_root = _setup_pane(tmp_path, state="failed", agent="claude")
        slug = "test-worker"

        mock_context.return_value = "Upstream error from openrouter: connection timeout"
        mock_retry.return_value = {
            "retried": True,
            "new_slug": "test-worker-2",
            "attempt": 1,
        }

        result = maybe_auto_retry(session_root, slug, "/fake/project")

        assert result is not None
        assert result["retried"] == slug
        assert result["new_slug"] == "test-worker-2"
        assert result["attempt"] == 1
        assert result["failure_class"] == "provider_runtime"
        assert result["provider_name"] == "openrouter"
        # Verify original prompt was used (no advisory text)
        call_prompt = mock_retry.call_args.kwargs.get("prompt", "")
        assert "Avoid the same failure" not in call_prompt

    @patch("dgov.recovery.retry_worker_pane")
    @patch("dgov.recovery.retry_context")
    def test_provider_failure_emits_event_with_metadata(
        self, mock_context, mock_retry, tmp_path: Path
    ) -> None:
        """Provider failure retry emits pane_auto_retried with failure metadata."""
        from dgov.persistence import read_events

        session_root = _setup_pane(tmp_path, state="failed", agent="claude")
        slug = "test-worker"

        mock_context.return_value = "Upstream error from anthropic: rate limited"
        mock_retry.return_value = {
            "retried": True,
            "new_slug": "test-worker-2",
            "attempt": 1,
        }

        maybe_auto_retry(session_root, slug, "/fake/project")

        events = read_events(session_root)
        retried_events = [e for e in events if e.get("event") == "pane_auto_retried"]
        assert len(retried_events) == 1
        ev = retried_events[0]
        assert ev.get("failure_class") == "provider_runtime"
        assert ev.get("provider_name") == "anthropic"

    @patch("dgov.recovery.load_registry")
    @patch("dgov.recovery.retry_context")
    def test_provider_failure_respects_existing_policy(
        self, mock_context, mock_registry, tmp_path: Path
    ) -> None:
        """When policy exists, normal retry path is taken (with advisory text)."""
        session_root = _setup_pane(tmp_path, state="failed", agent="test-agent")
        slug = "test-worker"

        agent_def = MagicMock()
        agent_def.max_retries = 2
        agent_def.retry_escalate_to = None
        mock_registry.return_value = {"test-agent": agent_def}

        mock_context.return_value = "Upstream error from openrouter: connection timeout"

        with patch("dgov.recovery.retry_worker_pane") as mock_retry:
            mock_retry.return_value = {
                "retried": True,
                "new_slug": "test-worker-2",
                "attempt": 1,
            }

            result = maybe_auto_retry(session_root, slug, "/fake/project")

            assert result is not None
            assert result["retried"] == slug
            # When policy exists, advisory text IS added
            call_prompt = mock_retry.call_args.kwargs.get("prompt", "")
            assert "Avoid the same failure" in call_prompt
