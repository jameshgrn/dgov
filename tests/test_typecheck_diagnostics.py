"""Tests for type-check diagnostic parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.typecheck_diagnostics import count_diagnostics, parse_diagnostic_identities

pytestmark = pytest.mark.unit


def test_count_diagnostics_parses_singular_and_plural() -> None:
    assert count_diagnostics("Found 1 diagnostic") == 1
    assert count_diagnostics("Found 12 diagnostics") == 12


def test_count_diagnostics_returns_zero_without_summary() -> None:
    assert count_diagnostics("type check failed") == 0


def test_parse_diagnostic_identities_uses_relative_paths(tmp_path: Path) -> None:
    output = (
        "error[unknown-symbol]: missing name\n"
        f"  --> {tmp_path / 'src' / 'a.py'}:10:5\n"
        "error[invalid-argument-type]: wrong arg\n"
        f"  --> {tmp_path / 'tests' / 'test_a.py'}:3:1\n"
    )

    assert parse_diagnostic_identities(output, tmp_path) == {
        ("src/a.py", "unknown-symbol"),
        ("tests/test_a.py", "invalid-argument-type"),
    }


def test_parse_diagnostic_identities_ignores_unpaired_error_codes() -> None:
    output = "error[unknown-symbol]: missing name\n"

    assert parse_diagnostic_identities(output) == set()
