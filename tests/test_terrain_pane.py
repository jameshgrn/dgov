"""Tests for dgov.terrain_pane — terrain TUI helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from rich.text import Text

from dgov.terrain import ErosionModel
from dgov.terrain_pane import _clamp_rendered, _compute_hud, _detect_pane_size

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


class TestHUD:
    def test_hud_returns_text(self):
        """HUD should return a non-empty Rich Text object."""
        model = ErosionModel(width=20, height=20, seed=42)
        for _ in range(10):
            model.step()
        hud = _compute_hud(model)
        assert isinstance(hud, Text)
        assert len(hud.plain) > 0

    def test_hud_contains_maturity(self):
        """HUD should contain the maturity metric (mat:xx.xx)."""
        model = ErosionModel(width=20, height=20, seed=42)
        for _ in range(10):
            model.step()
        hud = _compute_hud(model)
        assert "mat:" in hud.plain

    def test_hud_contains_state_label(self):
        """HUD should contain one of the state labels."""
        model = ErosionModel(width=20, height=20, seed=42)
        for _ in range(5):
            model.step()
        hud = _compute_hud(model)
        labels = ["youthful", "organizing", "mature", "settled"]
        assert any(label in hud.plain for label in labels)

    def test_hud_contains_delta_z(self):
        """HUD should contain the delta z metric (dz:xx.xxxx)."""
        model = ErosionModel(width=20, height=20, seed=42)
        for _ in range(10):
            model.step()
        hud = _compute_hud(model)
        assert "dz:" in hud.plain

    def test_hud_fresh_model(self):
        """HUD should work on a model with zero steps."""
        model = ErosionModel(width=13, height=13, seed=7)
        hud = _compute_hud(model)
        assert isinstance(hud, Text)

    def test_hud_contains_active_channels(self):
        """HUD should contain the active channels count (ch:xx)."""
        model = ErosionModel(width=20, height=20, seed=42)
        for _ in range(10):
            model.step()
        hud = _compute_hud(model)
        assert "ch:" in hud.plain

    def test_hud_uses_correct_styles(self):
        """HUD metrics should use correct Rich styles."""
        model = ErosionModel(width=20, height=20, seed=42)
        for _ in range(10):
            model.step()
        hud = _compute_hud(model)

        # Check that styled text appears - verify segments have spans applied
        # Segments with styles will have non-empty spans
        has_style = any(len(getattr(seg, "spans", [])) > 0 for seg in hud)
        assert has_style

    def test_hud_different_maturity_levels(self):
        """HUD should show different state labels for different maturity levels."""
        # Young model (low maturity)
        young_model = ErosionModel(width=15, height=15, seed=123)
        young_hud = _compute_hud(young_model)

        # Mature model (after steps)
        mature_model = ErosionModel(width=15, height=15, seed=123)
        for _ in range(20):
            mature_model.step()
        mature_hud = _compute_hud(mature_model)

        # Both should contain state labels
        assert any(label in young_hud.plain for label in ["youthful", "organizing"])
        assert any(label in mature_hud.plain for label in ["mature", "settled"])
