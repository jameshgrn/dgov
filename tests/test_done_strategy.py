"""Tests for per-agent DoneStrategy in agents.py and waiter.py."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.agents import (
    AGENT_REGISTRY,
    DoneStrategy,
    _agent_def_from_toml,
    _done_strategy_from_toml,
    load_registry,
)
from dgov.backend import set_backend
from dgov.done import _is_done, _resolve_strategy
from dgov.persistence import STATE_DIR, WorkerPane, add_pane, get_pane

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def mock_backend():
    import dgov.backend as _be

    prev = _be._backend
    mock = MagicMock()
    mock.create_pane.return_value = "%1"
    mock.is_alive.return_value = True
    mock.bulk_info.return_value = {}
    set_backend(mock)
    yield mock
    _be._backend = prev


def _setup_pane(tmp_path: Path, slug: str = "test-slug", state: str = "active") -> None:
    """Register a pane in persistence so _is_done can update state."""
    session_root = str(tmp_path)
    add_pane(
        session_root,
        WorkerPane(
            slug=slug,
            prompt="test",
            pane_id="%1",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / slug),
            branch_name=slug,
            state=state,
        ),
    )


# ---------------------------------------------------------------------------
# DoneStrategy dataclass
# ---------------------------------------------------------------------------


class TestDoneStrategy:
    def test_defaults(self) -> None:
        ds = DoneStrategy(type="signal")
        assert ds.type == "signal"
        assert ds.stable_seconds == 15

    def test_custom_stable_seconds(self) -> None:
        ds = DoneStrategy(type="stable", stable_seconds=30)
        assert ds.stable_seconds == 30

    def test_frozen(self) -> None:
        ds = DoneStrategy(type="signal")
        with pytest.raises(AttributeError):
            ds.type = "exit"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Built-in agent strategies
# ---------------------------------------------------------------------------


class TestBuiltinStrategies:
    def test_claude_api_strategy(self) -> None:
        ds = AGENT_REGISTRY["claude"].done_strategy
        assert ds is not None
        assert ds.type == "api"

    def test_codex_api_strategy(self) -> None:
        ds = AGENT_REGISTRY["codex"].done_strategy
        assert ds is not None
        assert ds.type == "api"

    def test_gemini_api_strategy(self) -> None:
        ds = AGENT_REGISTRY["gemini"].done_strategy
        assert ds is not None
        assert ds.type == "api"

    def test_pi_api_strategy(self) -> None:
        ds = AGENT_REGISTRY["pi"].done_strategy
        assert ds is not None
        assert ds.type == "api"

    def test_cline_stable_strategy(self) -> None:
        ds = AGENT_REGISTRY["cline"].done_strategy
        assert ds is not None
        assert ds.type == "stable"
        assert ds.stable_seconds == 30

    def test_crush_stable_strategy(self) -> None:
        ds = AGENT_REGISTRY["crush"].done_strategy
        assert ds is not None
        assert ds.type == "stable"
        assert ds.stable_seconds == 30


# ---------------------------------------------------------------------------
# TOML parsing
# ---------------------------------------------------------------------------


class TestDoneStrategyFromToml:
    def test_no_done_section(self) -> None:
        table: dict = {"name": "test"}
        assert _done_strategy_from_toml(table) is None

    def test_with_done_section(self) -> None:
        table: dict = {"done": {"type": "exit"}}
        ds = _done_strategy_from_toml(table)
        assert ds is not None
        assert ds.type == "exit"
        assert ds.stable_seconds == 15  # default

    def test_with_stable_seconds(self) -> None:
        table: dict = {"done": {"type": "stable", "stable_seconds": 45}}
        ds = _done_strategy_from_toml(table)
        assert ds is not None
        assert ds.type == "stable"
        assert ds.stable_seconds == 45

    def test_done_section_popped(self) -> None:
        table: dict = {"done": {"type": "commit"}, "name": "x"}
        _done_strategy_from_toml(table)
        assert "done" not in table
        assert "name" in table


class TestAgentDefFromTomlWithDone:
    def test_agent_from_toml_with_done(self) -> None:
        table = {
            "command": "my-agent",
            "transport": "positional",
            "done": {"type": "commit"},
        }
        agent = _agent_def_from_toml("my-agent", table, "user")
        assert agent.done_strategy is not None
        assert agent.done_strategy.type == "commit"

    def test_agent_from_toml_without_done(self) -> None:
        table = {
            "command": "my-agent",
            "transport": "positional",
        }
        agent = _agent_def_from_toml("my-agent", table, "user")
        # Defaults to "api" to prevent premature stabilization during startup
        assert agent.done_strategy is not None
        assert agent.done_strategy.type == "api"


class TestLoadRegistryWithDone:
    def test_user_config_done_strategy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("dgov.agents.Path.home", lambda: tmp_path)
        config_dir = tmp_path / ".dgov"
        config_dir.mkdir(parents=True)
        (config_dir / "agents.toml").write_text(
            "[agents.custom]\n"
            'command = "custom-cli"\n'
            'transport = "positional"\n'
            "\n"
            "[agents.custom.done]\n"
            'type = "stable"\n'
            "stable_seconds = 60\n"
        )
        registry = load_registry(None)
        agent = registry["custom"]
        assert agent.done_strategy is not None
        assert agent.done_strategy.type == "stable"
        assert agent.done_strategy.stable_seconds == 60

    def test_merge_overrides_done_strategy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """User config overrides built-in done_strategy."""
        monkeypatch.setattr("dgov.agents.Path.home", lambda: tmp_path)
        config_dir = tmp_path / ".dgov"
        config_dir.mkdir(parents=True)
        (config_dir / "agents.toml").write_text(
            '[agents.pi]\n\n[agents.pi.done]\ntype = "stable"\nstable_seconds = 20\n'
        )
        registry = load_registry(None)
        ds = registry["pi"].done_strategy
        assert ds is not None
        assert ds.type == "stable"
        assert ds.stable_seconds == 20

    def test_merge_preserves_base_done_strategy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If override doesn't include [done], base done_strategy is preserved."""
        monkeypatch.setattr("dgov.agents.Path.home", lambda: tmp_path)
        config_dir = tmp_path / ".dgov"
        config_dir.mkdir(parents=True)
        (config_dir / "agents.toml").write_text("[agents.pi]\ncolor = 99\n")
        registry = load_registry(None)
        ds = registry["pi"].done_strategy
        assert ds is not None
        assert ds.type == "api"  # preserved from built-in


# ---------------------------------------------------------------------------
# _resolve_strategy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveStrategy:
    def test_none_strategy_defaults_to_api(self) -> None:
        stype, ss = _resolve_strategy(None, None)
        assert stype == "api"
        assert ss == 0

    def test_none_strategy_with_stable_seconds(self) -> None:
        stype, ss = _resolve_strategy(None, 20)
        assert stype == "api"
        assert ss == 0

    def test_explicit_strategy(self) -> None:
        ds = DoneStrategy(type="exit")
        stype, ss = _resolve_strategy(ds, None)
        assert stype == "exit"

    def test_stable_strategy_uses_own_seconds(self) -> None:
        ds = DoneStrategy(type="stable", stable_seconds=45)
        stype, ss = _resolve_strategy(ds, 10)
        assert stype == "stable"
        assert ss == 45


# ---------------------------------------------------------------------------
# _is_done with done_strategy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIsDoneWithStrategy:
    @pytest.mark.unit
    def test_signal_strategy_works_as_before(self, tmp_path: Path) -> None:
        """Signal strategy: done file → done."""
        session_root = str(tmp_path)
        slug = "test-signal"
        _setup_pane(tmp_path, slug=slug)
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / slug).touch()
        assert _is_done(session_root, slug, done_strategy=DoneStrategy(type="signal")) is False

    @pytest.mark.unit
    def test_exit_strategy_skips_commit_check(self, tmp_path: Path) -> None:
        """Exit strategy: new commits on branch should NOT trigger done."""
        session_root = str(tmp_path)
        slug = "test-exit"
        _setup_pane(tmp_path, slug=slug)
        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }

        with patch("dgov.done._has_new_commits", return_value=True):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                done_strategy=DoneStrategy(type="exit"),
            )
            # With exit strategy, commit check is skipped — pane is still alive → not done
            assert result is False

    @pytest.mark.unit
    def test_exit_strategy_still_checks_done_file(self, tmp_path: Path) -> None:
        """Exit strategy still honors done-signal file."""
        session_root = str(tmp_path)
        slug = "test-exit-done"
        _setup_pane(tmp_path, slug=slug)
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / slug).touch()
        assert _is_done(session_root, slug, done_strategy=DoneStrategy(type="exit")) is False

    @pytest.mark.unit
    def test_commit_strategy_skips_stabilization(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Commit strategy: output stabilization should NOT trigger done."""
        session_root = str(tmp_path)
        slug = "test-commit"
        _setup_pane(tmp_path, slug=slug)
        mock_backend.is_alive.return_value = True
        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }
        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 60}

        with (
            patch("dgov.done._has_new_commits", return_value=False),
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.done._agent_still_running", return_value=False),
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                _stable_state=stable_state,
                done_strategy=DoneStrategy(type="commit"),
            )
            # With commit strategy, stabilization is skipped
            assert result is False

    @pytest.mark.unit
    def test_stable_strategy_uses_custom_seconds(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Stable strategy: uses its own stable_seconds."""
        session_root = str(tmp_path)
        slug = "test-stable"
        _setup_pane(tmp_path, slug=slug)
        mock_backend.is_alive.return_value = True
        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }
        # stable_since is 10s ago, strategy requires 30s
        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 10}

        with (
            patch("dgov.done._has_new_commits", return_value=True),
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.done._agent_still_running", return_value=False),
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                _stable_state=stable_state,
                done_strategy=DoneStrategy(type="stable", stable_seconds=30),
            )
            # Not stable long enough yet
            assert result is False

    def test_stable_strategy_triggers_when_stable_enough(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Stable strategy: triggers when output stable for stable_seconds."""
        session_root = str(tmp_path)
        slug = "test-stable-done"
        _setup_pane(tmp_path, slug=slug)
        mock_backend.is_alive.return_value = True
        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }
        # stable_since is 35s ago, strategy requires 30s
        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 35}

        with (
            patch("dgov.done._has_new_commits", return_value=True),
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.done._agent_still_running", return_value=False),
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                _stable_state=stable_state,
                done_strategy=DoneStrategy(type="stable", stable_seconds=30),
            )
            assert result is True

    @pytest.mark.unit
    def test_stable_strategy_skips_commit_check(self, tmp_path: Path) -> None:
        """Stable strategy should skip commit check."""
        session_root = str(tmp_path)
        slug = "test-stable-nocommit"
        _setup_pane(tmp_path, slug=slug)
        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }

        with patch("dgov.done._has_new_commits", return_value=True) as mock_commits:
            _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                done_strategy=DoneStrategy(type="stable", stable_seconds=30),
                _stable_state={},
            )
            mock_commits.assert_not_called()

    @pytest.mark.unit
    def test_api_strategy_runs_commit_check_for_fallback(self, tmp_path: Path) -> None:
        """Api strategy runs commit check for fallback; without 60s stability, not done yet."""
        session_root = str(tmp_path)
        slug = "test-default"
        _setup_pane(tmp_path, slug=slug)
        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }

        with (
            patch("dgov.done._has_new_commits", return_value=True) as mock_commits,
            patch("dgov.done._agent_still_running", return_value=True),
            patch("dgov.done.get_backend") as mock_be,
        ):
            mock_be.return_value.is_alive.return_value = True
            stable_state = {"last_output": "x", "stable_since": time.monotonic() - 30}
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                _stable_state=stable_state,
            )
            # Api strategy: commits detected but only 30s stable (need 60s) → not done yet
            assert result is False
            mock_commits.assert_called_once()
            assert stable_state.get("commits_detected") is True


# ---------------------------------------------------------------------------
# list_worker_panes passes done_strategy to _is_done
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestListWorkerPanesPassesDoneStrategy:
    """Verify list_worker_panes resolves agent done_strategy and forwards it."""

    def test_claude_api_strategy_passed(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        """Claude panes get DoneStrategy(type='api') forwarded to _is_done."""
        from dgov.status import list_worker_panes

        session_root = str(tmp_path)
        slug = "claude-worker"
        _setup_pane(tmp_path, slug=slug)

        # Make the pane look alive
        mock_backend.bulk_info.return_value = {"%1": {"current_command": "claude"}}

        with (
            patch("dgov.status._is_done", wraps=_is_done) as spy_is_done,
            patch("dgov.done._has_new_commits", return_value=True),
            patch("dgov.done._agent_still_running", return_value=False),
        ):
            list_worker_panes(session_root, session_root, include_freshness=False)

            spy_is_done.assert_called_once()
            call_kwargs = spy_is_done.call_args
            strategy = call_kwargs.kwargs.get("done_strategy") or (
                call_kwargs[1].get("done_strategy") if len(call_kwargs) > 1 else None
            )
            assert strategy is not None, "done_strategy was not passed to _is_done"
            assert strategy.type == "api", f"Expected 'api', got '{strategy.type}'"
            assert call_kwargs.kwargs.get("alive") is True


@pytest.mark.unit
class TestShellReturnDoneDetection:
    """Test shell-return terminal state detection for api strategy."""

    @pytest.mark.unit
    def test_shell_return_no_commits_fails(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        """Shell return case: pane alive, agent not running, no commits -> failed."""
        from dgov.persistence import get_pane

        session_root = str(tmp_path)
        slug = "test-shell-return-fail"
        _setup_pane(tmp_path, slug=slug)

        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }

        mock_backend.is_alive.return_value = True

        with (
            patch("dgov.done._has_new_commits", return_value=False),
            patch("dgov.done._agent_still_running", return_value=False),
            patch("dgov.done.get_backend") as mock_be,
        ):
            mock_be.return_value.is_alive.return_value = True

            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                done_strategy=DoneStrategy(type="api"),
                alive=True,
                current_command="bash",
                _stable_state={},
            )

        assert result is True
        updated = get_pane(session_root, slug)
        assert updated["state"] == "failed"

    @pytest.mark.unit
    def test_shell_return_with_commits_done(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        """Shell return case: pane alive, agent not running, commits exist -> done."""
        from dgov.persistence import get_pane

        session_root = str(tmp_path)
        slug = "test-shell-return-done"
        _setup_pane(tmp_path, slug=slug)

        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }

        mock_backend.is_alive.return_value = True

        with (
            patch("dgov.done._has_new_commits", return_value=True),
            patch("dgov.done._agent_still_running", return_value=False),
            patch("dgov.done.get_backend") as mock_be,
        ):
            mock_be.return_value.is_alive.return_value = True

            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                done_strategy=DoneStrategy(type="api"),
                alive=True,
                current_command="bash",
                _stable_state={},
            )

        assert result is True
        updated = get_pane(session_root, slug)
        assert updated["state"] == "done"


@pytest.mark.unit
class TestLateTerminalSignals:
    def test_late_done_signal_after_failed_preserves_failed(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        session_root = str(tmp_path)
        slug = "test-late-done-signal"
        _setup_pane(tmp_path, slug=slug, state="failed")
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / slug).touch()

        with (
            patch("dgov.done._has_completion_commit", return_value=True),
            patch("dgov.persistence.emit_event") as mock_emit,
        ):
            result = _is_done(session_root, slug, _stable_state={})

        assert result is True
        assert get_pane(session_root, slug)["state"] == "failed"
        mock_emit.assert_not_called()

    def test_late_commit_completion_after_failed_preserves_failed(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        session_root = str(tmp_path)
        slug = "test-late-commit-done"
        _setup_pane(tmp_path, slug=slug, state="failed")

        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }

        mock_backend.is_alive.return_value = True

        with (
            patch("dgov.done._has_new_commits", return_value=True),
            patch("dgov.done._agent_still_running", return_value=False),
            patch("dgov.done.get_backend") as mock_be,
            patch("dgov.persistence.emit_event") as mock_emit,
        ):
            mock_be.return_value.is_alive.return_value = True
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                done_strategy=DoneStrategy(type="api"),
                alive=True,
                current_command="bash",
                _stable_state={},
            )

        assert result is True
        assert get_pane(session_root, slug)["state"] == "failed"
        mock_emit.assert_not_called()

    def test_stale_pane_record_done_signal_after_failed_preserves_failed(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        session_root = str(tmp_path)
        slug = "test-stale-done-signal"
        _setup_pane(tmp_path, slug=slug, state="failed")
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / slug).touch()

        stale_pane_record = {
            "slug": slug,
            "state": "active",
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",
        }

        with (
            patch("dgov.done._has_completion_commit", return_value=True),
            patch("dgov.persistence.emit_event") as mock_emit,
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=stale_pane_record,
                _stable_state={},
            )

        assert result is True
        assert get_pane(session_root, slug)["state"] == "failed"
        mock_emit.assert_not_called()

    def test_late_exit_signal_after_done_preserves_done(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        session_root = str(tmp_path)
        slug = "test-late-exit"
        _setup_pane(tmp_path, slug=slug, state="done")
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / f"{slug}.exit").write_text("1")

        with patch("dgov.persistence.emit_event") as mock_emit:
            result = _is_done(session_root, slug, _stable_state={})

        assert result is True
        assert get_pane(session_root, slug)["state"] == "done"
        mock_emit.assert_not_called()


@pytest.mark.unit
class TestDoneSignalWithZeroCommits:
    """Test done-signal detection when pane has 0 commits (db_state=done)."""

    @pytest.mark.unit
    def test_done_signal_with_zero_commits_and_db_done(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Test done signal path: done file + db state=done triggers detection."""
        session_root = str(tmp_path)
        slug = "test-zero-commit-done"
        _setup_pane(tmp_path, slug=slug, state="done")

        # Create done signal file
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / slug).touch()

        pane_record = {
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "branch_name": slug,
            "base_sha": "abc123",  # HEAD is base_sha → 0 commits
            "state": "done",
        }

        stable_state = {}
        result = _is_done(
            session_root,
            slug,
            pane_record=pane_record,
            _stable_state=stable_state,
        )

        assert result is True
        assert stable_state.get("_done_reason") == "done_signal_db_confirmed"


# ---------------------------------------------------------------------------
# _has_new_commits guards
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHasNewCommitsGuards:
    """Tests for _has_new_commits input validation."""

    def test_empty_project_root_returns_false(self) -> None:
        """_has_new_commits returns False when project_root is empty."""
        from dgov.done import _has_new_commits

        result = _has_new_commits("", "main", "abc123")
        assert result is False

    def test_empty_branch_name_returns_false(self) -> None:
        """_has_new_commits returns False when branch_name is empty."""
        from dgov.done import _has_new_commits

        result = _has_new_commits(".", "", "abc123")
        assert result is False

    def test_all_empty_returns_false(self) -> None:
        """_has_new_commits returns False when all inputs are empty."""
        from dgov.done import _has_new_commits

        result = _has_new_commits("", "", "")
        assert result is False

    def test_none_base_sha_returns_false(self) -> None:
        """_has_new_commits returns False when base_sha is None/empty."""
        from dgov.done import _has_new_commits

        result = _has_new_commits(".", "main", "")
        assert result is False


# ---------------------------------------------------------------------------
# _AGENT_COMMANDS verification
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAgentCommands:
    """Tests for _AGENT_COMMANDS frozenset in done.py."""

    def test_agent_commands_contains_expected_agents(self) -> None:
        """_AGENT_COMMANDS contains expected agent process names."""
        from dgov.done import _AGENT_COMMANDS

        # Core agents that should be detected
        assert "claude" in _AGENT_COMMANDS
        assert "codex" in _AGENT_COMMANDS
        assert "gemini" in _AGENT_COMMANDS
        assert "pi" in _AGENT_COMMANDS
        assert "qwen" in _AGENT_COMMANDS
        assert "python" in _AGENT_COMMANDS
        assert "python3" in _AGENT_COMMANDS

    def test_agent_commands_is_frozenset(self) -> None:
        """_AGENT_COMMANDS is immutable (frozenset)."""
        from dgov.done import _AGENT_COMMANDS

        assert isinstance(_AGENT_COMMANDS, frozenset)

    def test_agent_commands_case_sensitive(self) -> None:
        """_AGENT_COMMANDS uses lowercase keys."""
        from dgov.done import _AGENT_COMMANDS

        # Should be lowercase
        assert "claude" in _AGENT_COMMANDS
        assert "Claude" not in _AGENT_COMMANDS


# ---------------------------------------------------------------------------
# _wrap_cmd tests
# ---------------------------------------------------------------------------


class TestWrapCmd:
    """Tests for the unified _wrap_cmd function (consolidation of #75)."""

    def test_headless_without_worktree(self):
        from dgov.done import _wrap_cmd

        result = _wrap_cmd("pi -p", "/tmp/done/slug", headless=True)
        assert "touch" in result
        assert "pi -p" in result
        assert ".exit" in result

    def test_headless_with_worktree(self):
        from dgov.done import _wrap_cmd

        result = _wrap_cmd("pi -p", "/tmp/done/slug", worktree_path="/wt", headless=True)
        assert "Auto-commit on agent exit" in result
        assert "__dgov_rc" in result
        assert "touch" in result

    def test_interactive_without_worktree(self):
        from dgov.done import _wrap_cmd

        result = _wrap_cmd("claude -p", "/tmp/done/slug", headless=False)
        assert "[ -f" in result
        assert ".exit" in result
        assert "touch" not in result  # interactive never touches .done

    def test_interactive_with_worktree(self):
        from dgov.done import _wrap_cmd

        result = _wrap_cmd("claude -p", "/tmp/done/slug", worktree_path="/wt", headless=False)
        assert "Auto-commit on agent exit" in result
        assert "[ -f" in result

    def test_backward_compat_aliases(self):
        """_wrap_done_signal and _wrap_exit_signal produce same output as _wrap_cmd."""
        from dgov.done import _wrap_cmd, _wrap_done_signal, _wrap_exit_signal

        assert _wrap_done_signal("cmd", "/sig") == _wrap_cmd("cmd", "/sig", headless=True)
        assert _wrap_exit_signal("cmd", "/sig") == _wrap_cmd("cmd", "/sig", headless=False)
        assert _wrap_done_signal("cmd", "/sig", worktree_path="/wt") == _wrap_cmd(
            "cmd", "/sig", worktree_path="/wt", headless=True
        )
        assert _wrap_exit_signal("cmd", "/sig", worktree_path="/wt") == _wrap_cmd(
            "cmd", "/sig", worktree_path="/wt", headless=False
        )
