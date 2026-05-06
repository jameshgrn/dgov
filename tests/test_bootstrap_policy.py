"""Tests for the runtime-owned bootstrap policy pack."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.bootstrap_policy import GOVERNOR_CHARTER, SOP_FILES
from dgov.config import load_project_config
from dgov.policy_drift import find_policy_drift

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def test_bootstrap_policy_assets_match_repo_policy_pack() -> None:
    repo_governor = (ROOT / ".dgov" / "governor.md").read_text()
    repo_sops_dir = ROOT / ".dgov" / "sops"
    repo_sop_paths = sorted(repo_sops_dir.glob("*.md"))

    assert repo_governor == GOVERNOR_CHARTER
    assert set(SOP_FILES) == {path.name for path in repo_sop_paths}
    for path in repo_sop_paths:
        assert SOP_FILES[path.name] == path.read_text()


def test_repo_policy_has_no_drift() -> None:
    assert find_policy_drift(ROOT) == []


def test_repo_project_test_command_is_unit_scoped() -> None:
    config = load_project_config(str(ROOT))

    assert "-m unit" in config.test_cmd
