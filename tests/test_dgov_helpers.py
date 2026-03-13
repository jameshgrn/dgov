"""Unit tests for dgov helper functions — panes state, slug generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# panes state helpers
# ---------------------------------------------------------------------------


class TestPanesStateHelpers:
    def test_state_path(self, tmp_path: Path) -> None:
        from dgov.panes import _state_path

        result = _state_path(str(tmp_path))
        assert result == tmp_path / ".dgov" / "state.json"

    def test_read_state_empty(self, tmp_path: Path) -> None:
        from dgov.panes import _read_state

        result = _read_state(str(tmp_path))
        assert result == {"panes": []}

    def test_read_state_existing(self, tmp_path: Path) -> None:
        from dgov.panes import _read_state

        ws = tmp_path / ".dgov"
        ws.mkdir()
        (ws / "state.json").write_text(json.dumps({"panes": [{"slug": "s1"}]}))
        result = _read_state(str(tmp_path))
        assert len(result["panes"]) == 1
        assert result["panes"][0]["slug"] == "s1"

    def test_write_state(self, tmp_path: Path) -> None:
        from dgov.panes import _read_state, _write_state

        _write_state(str(tmp_path), {"panes": [{"slug": "s1"}]})
        result = _read_state(str(tmp_path))
        assert len(result["panes"]) == 1

    def test_add_pane(self, tmp_path: Path) -> None:
        from dgov.panes import WorkerPane, _add_pane, _all_panes

        pane = WorkerPane(
            slug="test-slug",
            pane_id="%99",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="test-branch",
            prompt="do stuff",
        )
        _add_pane(str(tmp_path), pane)
        all_panes = _all_panes(str(tmp_path))
        assert len(all_panes) == 1
        assert all_panes[0]["slug"] == "test-slug"

    def test_remove_pane(self, tmp_path: Path) -> None:
        from dgov.panes import WorkerPane, _add_pane, _all_panes, _remove_pane

        pane = WorkerPane(
            slug="rm-me",
            pane_id="%99",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="b",
            prompt="p",
        )
        _add_pane(str(tmp_path), pane)
        assert len(_all_panes(str(tmp_path))) == 1
        _remove_pane(str(tmp_path), "rm-me")
        assert len(_all_panes(str(tmp_path))) == 0

    def test_get_pane_found(self, tmp_path: Path) -> None:
        from dgov.panes import WorkerPane, _add_pane, _get_pane

        pane = WorkerPane(
            slug="find-me",
            pane_id="%99",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="b",
            prompt="p",
        )
        _add_pane(str(tmp_path), pane)
        result = _get_pane(str(tmp_path), "find-me")
        assert result is not None
        assert result["slug"] == "find-me"

    def test_get_pane_not_found(self, tmp_path: Path) -> None:
        from dgov.panes import _get_pane

        result = _get_pane(str(tmp_path), "missing")
        assert result is None

    def test_all_panes_empty(self, tmp_path: Path) -> None:
        from dgov.panes import _all_panes

        result = _all_panes(str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# _generate_slug
# ---------------------------------------------------------------------------


class TestGenerateSlug:
    def test_fallback_slug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _generate_slug

        # Force Qwen failure to test fallback
        monkeypatch.setattr(
            "dgov.panes._qwen_4b_request",
            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("no gpu")),
        )
        result = _generate_slug("Fix the broken login")
        assert isinstance(result, str)
        assert len(result) <= 50
        # "the" is a stop word and should be filtered from the fallback slug
        parts = result.split("-")
        assert "the" not in parts

    def test_slug_from_qwen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _generate_slug

        monkeypatch.setattr(
            "dgov.panes._qwen_4b_request",
            lambda *a, **kw: {"choices": [{"message": {"content": "fix-auth-bug"}}]},
        )
        result = _generate_slug("Fix the authentication bug")
        assert result == "fix-auth-bug"

    def test_slug_sanitized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _generate_slug

        monkeypatch.setattr(
            "dgov.panes._qwen_4b_request",
            lambda *a, **kw: {"choices": [{"message": {"content": "  Fix Auth!  "}}]},
        )
        result = _generate_slug("Fix Auth!")
        # Should be lowercase, no special chars
        assert result == result.lower()
        assert "!" not in result

    def test_slug_too_long_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _generate_slug

        monkeypatch.setattr(
            "dgov.panes._qwen_4b_request",
            lambda *a, **kw: {"choices": [{"message": {"content": "x" * 60}}]},
        )
        result = _generate_slug("some task")
        assert len(result) <= 50

    def test_empty_slug_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _generate_slug

        monkeypatch.setattr(
            "dgov.panes._qwen_4b_request",
            lambda *a, **kw: {"choices": [{"message": {"content": "---"}}]},
        )
        result = _generate_slug("some task")
        assert result  # should not be empty

    def test_max_words_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _generate_slug

        monkeypatch.setattr(
            "dgov.panes._qwen_4b_request",
            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError()),
        )
        result = _generate_slug("add new feature for user login authentication", max_words=2)
        # Should have at most 2 words (2 hyphens max)
        assert result.count("-") <= 1


# ---------------------------------------------------------------------------
# WorkerPane dataclass
# ---------------------------------------------------------------------------


class TestWorkerPane:
    def test_defaults(self) -> None:
        from dgov.panes import WorkerPane

        pane = WorkerPane(
            slug="s",
            pane_id="%1",
            agent="pi",
            project_root="/tmp",
            worktree_path="/tmp/wt",
            branch_name="b",
            prompt="p",
        )
        assert pane.owns_worktree is True
        assert pane.base_sha == ""
        assert isinstance(pane.created_at, float)

    def test_custom_fields(self) -> None:
        from dgov.panes import WorkerPane

        pane = WorkerPane(
            slug="s",
            pane_id="%1",
            agent="claude",
            project_root="/tmp",
            worktree_path="/tmp/wt",
            branch_name="b",
            prompt="p",
            owns_worktree=False,
            base_sha="abc123",
        )
        assert pane.owns_worktree is False
        assert pane.base_sha == "abc123"
