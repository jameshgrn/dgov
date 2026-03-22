"""Tests for dgov.terrain_pane — terrain TUI helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from rich.text import Text

from dgov.terrain_pane import _clamp_rendered, _detect_pane_size

pytestmark = pytest.mark.unit


class TestDetectPaneSize:
    def test_subtracts_border(self):
        console = MagicMock()
        console.size = MagicMock(width=80, height=24)
        terminal_size = MagicMock(columns=80, lines=24)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("shutil.get_terminal_size", lambda fallback: terminal_size)
            w, h = _detect_pane_size(console)
        assert w == 78  # 80 - 2
        assert h == 22  # 24 - 2

    def test_minimum_1x1(self):
        console = MagicMock()
        console.size = MagicMock(width=1, height=1)
        terminal_size = MagicMock(columns=1, lines=1)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("shutil.get_terminal_size", lambda fallback: terminal_size)
            w, h = _detect_pane_size(console)
        assert w >= 1
        assert h >= 1


class TestClampRendered:
    def test_clamps_height(self):
        lines = [f"line {i}" for i in range(10)]
        rendered = Text("\n".join(lines))
        clamped = _clamp_rendered(rendered, width=80, height=3)
        result_lines = clamped.plain.split("\n")
        assert len(result_lines) <= 3

    def test_preserves_content(self):
        rendered = Text("hello\nworld")
        clamped = _clamp_rendered(rendered, width=80, height=10)
        assert "hello" in clamped.plain
        assert "world" in clamped.plain

    def test_empty_input(self):
        rendered = Text("")
        clamped = _clamp_rendered(rendered, width=80, height=10)
        assert clamped.plain == ""
