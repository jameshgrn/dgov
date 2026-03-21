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


def test_touches_excludes_tests_from_conflict_scope(monkeypatch):
    """Tests are informational context, not edit targets. They should NOT be in touches."""
    monkeypatch.setattr(
        "dgov.strategy.extract_task_context",
        lambda prompt: {
            "primary_files": ["src/dgov/merger.py"],
            "also_check": ["src/dgov/inspection.py"],
            "tests": [
                "tests/test_merger_coverage.py",
                "tests/test_merger_conflicts.py",
                "tests/test_dgov_merger.py",
            ],
        },
    )

    packet = build_context_packet("Fix merge conflict handling")

    # Tests should be stored in .tests for worker info
    assert "tests/test_merger_coverage.py" in packet.tests
    assert "tests/test_merger_conflicts.py" in packet.tests
    assert "tests/test_dgov_merger.py" in packet.tests

    # But touches (conflict scope) should NOT include them
    assert "tests/test_merger_coverage.py" not in packet.touches
    assert "tests/test_merger_conflicts.py" not in packet.touches
    assert "tests/test_dgov_merger.py" not in packet.touches

    # Only primary and also_check should be in touches
    assert packet.touches == ("src/dgov/merger.py", "src/dgov/inspection.py")


def test_touches_with_file_claims_excludes_tests(monkeypatch):
    """When file_claims are present, tests should not affect touches."""
    monkeypatch.setattr(
        "dgov.strategy.extract_task_context",
        lambda prompt: {
            "primary_files": ["src/dgov/merger.py"],
            "tests": ["tests/test_retry.py", "tests/test_bounded_retry.py"],
        },
    )

    packet = build_context_packet(
        "Improve retry strategy with test coverage",
        file_claims=["src/dgov/retry.py"],
    )

    # Tests are in .tests for worker info
    assert "tests/test_retry.py" in packet.tests
    assert "tests/test_bounded_retry.py" in packet.tests

    # But touches should ONLY be the claims (tests excluded)
    assert packet.touches == ("src/dgov/retry.py",)


def test_touches_also_check_included_without_tests(monkeypatch):
    """Also check files should be in touches when no file_claims."""
    monkeypatch.setattr(
        "dgov.strategy.extract_task_context",
        lambda prompt: {
            "primary_files": ["src/dgov/merger.py"],
            "also_check": ["src/dgov/persistence.py", "src/dgov/responder.py"],
            "tests": ["tests/test_recovery_dogfood.py"],
        },
    )

    packet = build_context_packet("Improve merge recovery")

    # Tests should be in .tests
    assert "tests/test_recovery_dogfood.py" in packet.tests

    # touches should include primary and also_check, but NOT tests
    assert packet.touches == (
        "src/dgov/merger.py",
        "src/dgov/persistence.py",
        "src/dgov/responder.py",
    )
