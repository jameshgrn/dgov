"""Unit tests for dgov.state — status checks."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dgov.state import get_status

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_returns_expected_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.state.list_worker_panes",
            lambda *a, **kw: [{"alive": True}, {"alive": False}],
        )
        result = get_status("/tmp/repo")
        assert result["total"] == 2
        assert result["alive"] == 1

    def test_empty_panes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.state.list_worker_panes", lambda *a, **kw: [])
        result = get_status("/tmp/repo")
        assert result["total"] == 0
        assert result["alive"] == 0

    def test_session_root_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def fake_list(project_root, session_root=None):
            captured["session_root"] = session_root
            return []

        monkeypatch.setattr("dgov.state.list_worker_panes", fake_list)
        get_status("/tmp/repo", session_root="/tmp/session")
        assert captured["session_root"] == "/tmp/session"


class TestGetStatusEdgeCases:
    def test_no_panes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        with patch("dgov.state.list_worker_panes", return_value=[]):
            result = get_status(str(tmp_path))

        assert result["total"] == 0
        assert result["alive"] == 0

    def test_mixed_pane_states(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        panes = [
            {"name": "w1", "alive": True},
            {"name": "w2", "alive": False},
            {"name": "w3", "alive": True},
        ]
        with patch("dgov.state.list_worker_panes", return_value=panes):
            result = get_status(str(tmp_path))

        assert result["total"] == 3
        assert result["alive"] == 2
