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
from dgov.persistence import STATE_DIR, WorkerPane, add_pane
from dgov.waiter import _is_done, _resolve_strategy

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


def _setup_pane(tmp_path: Path, slug: str = "test-slug") -> None:
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
    def test_claude_has_no_strategy(self) -> None:
        assert AGENT_REGISTRY["claude"].done_strategy is None

    def test_codex_signal_strategy(self) -> None:
        ds = AGENT_REGISTRY["codex"].done_strategy
        assert ds is not None
        assert ds.type == "signal"

    def test_gemini_signal_strategy(self) -> None:
        ds = AGENT_REGISTRY["gemini"].done_strategy
        assert ds is not None
        assert ds.type == "signal"

    def test_pi_exit_strategy(self) -> None:
        ds = AGENT_REGISTRY["pi"].done_strategy
        assert ds is not None
        assert ds.type == "exit"

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
        assert agent.done_strategy is None


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
        assert ds.type == "exit"  # preserved from built-in


# ---------------------------------------------------------------------------
# _resolve_strategy
# ---------------------------------------------------------------------------


class TestResolveStrategy:
    def test_none_strategy_defaults_to_signal(self) -> None:
        stype, ss = _resolve_strategy(None, None)
        assert stype == "signal"
        assert ss == 0

    def test_none_strategy_with_stable_seconds(self) -> None:
        stype, ss = _resolve_strategy(None, 20)
        assert stype == "signal"
        assert ss == 20

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


class TestIsDoneWithStrategy:
    def test_signal_strategy_works_as_before(self, tmp_path: Path) -> None:
        """Signal strategy: done file → done."""
        session_root = str(tmp_path)
        slug = "test-signal"
        _setup_pane(tmp_path, slug=slug)
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / slug).touch()
        assert _is_done(session_root, slug, done_strategy=DoneStrategy(type="signal")) is True

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

        with patch("dgov.waiter._has_new_commits", return_value=True):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                done_strategy=DoneStrategy(type="exit"),
            )
            # With exit strategy, commit check is skipped — pane is still alive → not done
            assert result is False

    def test_exit_strategy_still_checks_done_file(self, tmp_path: Path) -> None:
        """Exit strategy still honors done-signal file."""
        session_root = str(tmp_path)
        slug = "test-exit-done"
        _setup_pane(tmp_path, slug=slug)
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / slug).touch()
        assert _is_done(session_root, slug, done_strategy=DoneStrategy(type="exit")) is True

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
            patch("dgov.waiter._has_new_commits", return_value=False),
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.waiter._agent_still_running", return_value=False),
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
            "branch_name": "",
            "base_sha": "",
        }
        # stable_since is 10s ago, strategy requires 30s
        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 10}

        with (
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.waiter._agent_still_running", return_value=False),
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
            "branch_name": "",
            "base_sha": "",
        }
        # stable_since is 35s ago, strategy requires 30s
        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 35}

        with (
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.waiter._agent_still_running", return_value=False),
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                _stable_state=stable_state,
                done_strategy=DoneStrategy(type="stable", stable_seconds=30),
            )
            assert result is True

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

        with patch("dgov.waiter._has_new_commits", return_value=True) as mock_commits:
            _is_done(
                session_root,
                slug,
                pane_record=pane_record,
                done_strategy=DoneStrategy(type="stable", stable_seconds=30),
                _stable_state={},
            )
            mock_commits.assert_not_called()

    def test_none_strategy_uses_all_signals(self, tmp_path: Path) -> None:
        """None strategy (default) uses all signals including commit check."""
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
            patch("dgov.waiter._has_new_commits", return_value=True),
            patch("dgov.waiter._agent_still_running", return_value=False),
        ):
            result = _is_done(
                session_root,
                slug,
                pane_record=pane_record,
            )
            assert result is True
