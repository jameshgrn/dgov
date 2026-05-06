"""Tests for policy/canon drift checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.policy_drift import find_policy_drift

pytestmark = pytest.mark.unit


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_policy_drift_allows_repos_without_guidance_files(tmp_path: Path) -> None:
    assert find_policy_drift(tmp_path) == []


def test_policy_drift_flags_guidance_mirror_mismatch(tmp_path: Path) -> None:
    _write(tmp_path / "AGENTS.md", "canon\n")
    _write(tmp_path / "CLAUDE.md", "drift\n")
    _write(tmp_path / "GEMINI.md", "canon\n")

    assert find_policy_drift(tmp_path) == [
        "Guidance mirror drift: AGENTS.md differs from CLAUDE.md"
    ]


def test_policy_drift_flags_bootstrap_policy_mismatch(tmp_path: Path) -> None:
    _write(tmp_path / ".dgov" / "governor.md", "repo governor\n")
    _write(tmp_path / ".dgov" / "sops" / "testing.md", "repo sop\n")
    _write(
        tmp_path / "src" / "dgov" / "bootstrap_policy_data" / "governor.md",
        "asset governor\n",
    )
    _write(
        tmp_path / "src" / "dgov" / "bootstrap_policy_data" / "sops" / "testing.md",
        "asset sop\n",
    )

    assert find_policy_drift(tmp_path) == [
        ".dgov/governor.md differs from bootstrap policy asset",
        ".dgov/sops/testing.md differs from bootstrap policy asset",
    ]
