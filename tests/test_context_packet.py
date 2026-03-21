from __future__ import annotations

import pytest

from dgov.context_packet import build_context_packet, render_start_here_section

pytestmark = pytest.mark.unit


def test_architecture_context_renders_in_start_here():
    """Test that architecture context is rendered in start here section."""
    packet = build_context_packet(
        "Fix merge boundary bug",
        architecture_context=["rule1", "rule2"],
    )
    rendered = render_start_here_section(packet)

    assert "Architecture context:" in rendered
    assert "rule1" in rendered
    assert "rule2" in rendered


def test_architecture_context_empty_skips_section():
    """Test that empty architecture context does not render the section."""
    packet = build_context_packet("Fix merge boundary bug", architecture_context=[])
    rendered = render_start_here_section(packet)

    assert "Architecture context:" not in rendered


def test_build_context_packet_passes_architecture_context():
    """Test that build_context_packet properly passes architecture_context."""
    packet = build_context_packet(
        "Fix merge boundary bug",
        architecture_context=["test"],
    )

    assert packet.architecture_context == ("test",)


def test_build_context_packet_prefers_exact_claims(monkeypatch):
    monkeypatch.setattr(
        "dgov.strategy.extract_task_context",
        lambda prompt: {
            "primary_files": ["src/dgov/merger.py"],
            "also_check": ["src/dgov/inspection.py"],
            "tests": ["tests/test_merger_coverage.py"],
            "hints": ["Run related tests."],
        },
    )

    packet = build_context_packet(
        "Fix merge boundary bug",
        file_claims=["src/dgov/executor.py"],
        commit_message="Unify executor context",
    )

    assert packet.file_claims == ("src/dgov/executor.py",)
    assert packet.edit_files == ("src/dgov/executor.py",)
    assert packet.read_files == ("src/dgov/executor.py", "src/dgov/inspection.py")
    assert packet.tests == ("tests/test_merger_coverage.py",)
    assert packet.commit_message == "Unify executor context"
    assert packet.touches == ("src/dgov/executor.py",)


def test_render_start_here_section_includes_claims_tests_and_commit(monkeypatch):
    monkeypatch.setattr(
        "dgov.strategy.extract_task_context",
        lambda prompt: {
            "primary_files": ["src/dgov/merger.py"],
            "also_check": ["src/dgov/inspection.py"],
            "tests": ["tests/test_merger_coverage.py"],
            "hints": ["Run related tests."],
        },
    )

    packet = build_context_packet("Fix merge boundary bug")
    rendered = render_start_here_section(packet)

    assert "Read first:" in rendered
    assert "tests/test_merger_coverage.py" in rendered
    assert "Run related tests." in rendered
    assert "Commit message:" in rendered
