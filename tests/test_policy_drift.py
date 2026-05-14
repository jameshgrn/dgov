"""Tests for policy/canon drift checks."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from dgov.policy_drift import find_policy_drift

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


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


def test_policy_drift_flags_stale_bootstrap_policy_mirrors(tmp_path: Path) -> None:
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
        "Bootstrap policy assets are mirrored under "
        "src/dgov/bootstrap_policy_data: governor.md, sops/testing.md",
        "Bootstrap policy wheel force-include missing: "
        ".dgov/governor.md -> dgov/bootstrap_policy_data/governor.md, "
        ".dgov/sops -> dgov/bootstrap_policy_data/sops",
    ]


def test_policy_drift_accepts_derived_bootstrap_policy_assets(tmp_path: Path) -> None:
    _write(tmp_path / ".dgov" / "governor.md", "repo governor\n")
    _write(tmp_path / ".dgov" / "sops" / "testing.md", "repo sop\n")
    _write(tmp_path / "src" / "dgov" / "bootstrap_policy_data" / "__init__.py", "")
    _write(
        tmp_path / "pyproject.toml",
        """
[tool.hatch.build.targets.wheel.force-include]
".dgov/governor.md" = "dgov/bootstrap_policy_data/governor.md"
".dgov/sops" = "dgov/bootstrap_policy_data/sops"
""",
    )

    assert find_policy_drift(tmp_path) == []


def test_pyproject_packages_bootstrap_policy_from_repo_canonical_files() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    assert force_include[".dgov/governor.md"] == "dgov/bootstrap_policy_data/governor.md"
    assert force_include[".dgov/sops"] == "dgov/bootstrap_policy_data/sops"
