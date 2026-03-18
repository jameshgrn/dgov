"""Tests for bounded retry with automatic escalation."""

from __future__ import annotations

import pytest

from dgov.persistence import WorkerPane, _get_db, add_pane, set_pane_metadata
from dgov.recovery import ESCALATION_CHAIN, _resolve_escalation_target, retry_or_escalate

pytestmark = pytest.mark.unit


def _seed_pane(tmp_path, slug="task-1", agent="river-35b", state="failed", **meta):
    """Insert a fake pane record for testing."""
    _get_db(str(tmp_path))
    pane = WorkerPane(
        slug=slug,
        prompt="do the thing",
        pane_id="%1",
        agent=agent,
        project_root="/fake",
        worktree_path=str(tmp_path / "wt"),
        branch_name=slug,
        state=state,
    )
    add_pane(str(tmp_path), pane)
    if meta:
        set_pane_metadata(str(tmp_path), slug, **meta)


class _FakePane:
    """Minimal stand-in for WorkerPane returned by create_worker_pane."""

    def __init__(self, slug):
        self.slug = slug
        self.pane_id = "%999"
        self.worktree_path = "/fake/wt"


class TestRetryOrEscalate:
    def test_not_found(self, tmp_path):
        _get_db(str(tmp_path))
        result = retry_or_escalate(str(tmp_path), "nope", session_root=str(tmp_path))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_first_retry_same_agent(self, tmp_path, monkeypatch):
        """First failure retries with the same agent."""
        _seed_pane(tmp_path, slug="task-1", agent="river-35b", retry_count=0)
        monkeypatch.setattr(
            "dgov.recovery.create_worker_pane",
            lambda **kw: _FakePane("task-1-2"),
        )
        monkeypatch.setattr(
            "dgov.recovery.close_worker_pane",
            lambda *a, **kw: True,
        )

        result = retry_or_escalate(
            str(tmp_path), "task-1", session_root=str(tmp_path), max_retries=2
        )
        assert result["action"] == "retry"
        assert result["agent"] == "river-35b"
        assert result["retry_count"] == 1
        assert result["new_slug"] == "task-1-2"

    def test_second_retry_still_retries(self, tmp_path, monkeypatch):
        """Second failure (retry_count=1, max=2) still retries."""
        _seed_pane(tmp_path, slug="task-1", agent="river-35b", retry_count=1)
        monkeypatch.setattr(
            "dgov.recovery.create_worker_pane",
            lambda **kw: _FakePane("task-1-2"),
        )
        monkeypatch.setattr(
            "dgov.recovery.close_worker_pane",
            lambda *a, **kw: True,
        )

        result = retry_or_escalate(
            str(tmp_path), "task-1", session_root=str(tmp_path), max_retries=2
        )
        assert result["action"] == "retry"
        assert result["retry_count"] == 2

    def test_escalates_after_max_retries(self, tmp_path, monkeypatch):
        """After max_retries, escalates to next agent in chain."""
        _seed_pane(tmp_path, slug="task-1", agent="river-35b", retry_count=2)

        created_slugs = []

        def fake_create(**kw):
            created_slugs.append(kw.get("slug", "unknown"))
            return _FakePane(kw.get("slug", "esc-1"))

        monkeypatch.setattr("dgov.recovery.create_worker_pane", fake_create)
        monkeypatch.setattr("dgov.recovery.close_worker_pane", lambda *a, **kw: True)
        monkeypatch.setattr("dgov.recovery.update_pane_state", lambda *a, **kw: None)
        monkeypatch.setattr("dgov.recovery.emit_event", lambda *a, **kw: None)

        result = retry_or_escalate(
            str(tmp_path), "task-1", session_root=str(tmp_path), max_retries=2
        )
        assert result["action"] == "escalate"
        assert result["agent"] == "qwen-122b"  # river-35b -> qwen-122b in ESCALATION_CHAIN
        assert result["from_agent"] == "river-35b"
        assert result["retry_count"] == 0

    def test_terminal_agent_no_escalation(self, tmp_path, monkeypatch):
        """qwen3-max maps to itself — no further escalation possible."""
        _seed_pane(tmp_path, slug="task-1", agent="qwen-max", retry_count=2)
        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda *a, **kw: {},
        )

        result = retry_or_escalate(
            str(tmp_path), "task-1", session_root=str(tmp_path), max_retries=2
        )
        assert "error" in result
        assert "exhausted" in result["error"].lower()

    def test_pane_max_retries_override(self, tmp_path, monkeypatch):
        """Per-pane max_retries in metadata overrides the function argument."""
        _seed_pane(tmp_path, slug="task-1", agent="river-35b", retry_count=0, max_retries=1)
        monkeypatch.setattr(
            "dgov.recovery.create_worker_pane",
            lambda **kw: _FakePane("task-1-2"),
        )
        monkeypatch.setattr("dgov.recovery.close_worker_pane", lambda *a, **kw: True)

        # Function arg says max_retries=5, but pane metadata says 1
        result = retry_or_escalate(
            str(tmp_path), "task-1", session_root=str(tmp_path), max_retries=5
        )
        assert result["action"] == "retry"
        assert result["retry_count"] == 1

        # Now retry_count=1, pane max_retries=1 → should escalate
        _seed_pane(tmp_path, slug="task-2", agent="river-35b", retry_count=1, max_retries=1)

        def fake_create(**kw):
            return _FakePane(kw.get("slug", "esc-1"))

        monkeypatch.setattr("dgov.recovery.create_worker_pane", fake_create)
        monkeypatch.setattr("dgov.recovery.update_pane_state", lambda *a, **kw: None)
        monkeypatch.setattr("dgov.recovery.emit_event", lambda *a, **kw: None)

        result = retry_or_escalate(
            str(tmp_path), "task-2", session_root=str(tmp_path), max_retries=5
        )
        assert result["action"] == "escalate"

    def test_retry_propagates_error(self, tmp_path, monkeypatch):
        """If retry_worker_pane returns an error, it bubbles up."""
        _seed_pane(tmp_path, slug="task-1", agent="river-35b", retry_count=0)
        monkeypatch.setattr(
            "dgov.recovery.create_worker_pane",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("tmux dead")),
        )

        result = retry_or_escalate(
            str(tmp_path), "task-1", session_root=str(tmp_path), max_retries=2
        )
        assert "error" in result


class TestEscalationChain:
    def test_default_chain_coverage(self):
        assert ESCALATION_CHAIN["river-4b"] == "qwen-9b"
        assert ESCALATION_CHAIN["river-9b"] == "qwen-35b"
        assert ESCALATION_CHAIN["river-35b"] == "qwen-122b"
        assert ESCALATION_CHAIN["qwen35-35b"] == "qwen-122b"
        assert ESCALATION_CHAIN["qwen35-122b"] == "qwen-397b"
        assert ESCALATION_CHAIN["qwen35-397b"] == "qwen-max"
        assert ESCALATION_CHAIN["qwen-max"] == "qwen-max"
        assert ESCALATION_CHAIN["hunter"] == "qwen-35b"

    def test_unknown_agent_returns_self(self, tmp_path, monkeypatch):
        """Agents not in the chain map to themselves (no escalation)."""
        monkeypatch.setattr("dgov.agents.load_registry", lambda *a, **kw: {})
        result = _resolve_escalation_target("unknown-agent", "/fake")
        assert result == "unknown-agent"

    def test_agent_config_takes_priority(self, tmp_path, monkeypatch):
        """Agent's retry_escalate_to overrides ESCALATION_CHAIN."""
        from dataclasses import dataclass, field

        from dgov.agents import DoneStrategy

        @dataclass
        class FakeAgent:
            name: str = "pi"
            command: str = "pi"
            max_retries: int = 2
            retry_escalate_to: str = "gemini"
            health_check: str | None = None
            health_fix: str | None = None
            max_concurrent: int | None = None
            color: int | None = None
            env: dict = field(default_factory=dict)
            done_strategy: DoneStrategy | None = None

        monkeypatch.setattr(
            "dgov.recovery.load_registry",
            lambda *a, **kw: {"pi": FakeAgent()},
        )
        result = _resolve_escalation_target("pi", "/fake")
        # Agent config says gemini, not the chain default of claude
        assert result == "gemini"
