"""Tests for shipped dgov agent skill assets."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.agent_skills import load_agent_skills, sync_agent_skills

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def _source_skill_bundle() -> dict[str, str]:
    skills_dir = ROOT / "agent-guidance" / "skills"
    return {
        path.parent.name: path.read_text(encoding="utf-8")
        for path in sorted(skills_dir.glob("*/SKILL.md"), key=lambda item: item.parent.name)
    }


def test_agent_skill_assets_match_source_bundle() -> None:
    skills = load_agent_skills()

    assert skills == _source_skill_bundle()
    assert set(skills) == {"dgov-ledger", "dgov-pane", "dgov-plan"}
    assert "note" in skills["dgov-ledger"]
    assert "uv run dgov run" in skills["dgov-plan"]
    assert "dgov pane" in skills["dgov-pane"]


def test_sync_agent_skills_creates_updates_and_preserves_others(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    other_skill = skills_dir / "other" / "SKILL.md"
    other_skill.parent.mkdir(parents=True)
    other_skill.write_text("# other\n", encoding="utf-8")

    first = sync_agent_skills(skills_dir)

    assert len(first.created) == 3
    assert not first.updated
    assert not first.unchanged
    assert other_skill.read_text(encoding="utf-8") == "# other\n"

    ledger_skill = skills_dir / "dgov-ledger" / "SKILL.md"
    ledger_skill.write_text("# stale\n", encoding="utf-8")

    second = sync_agent_skills(skills_dir)

    assert second.created == ()
    assert second.updated == (ledger_skill,)
    assert len(second.unchanged) == 2
    assert ledger_skill.read_text(encoding="utf-8") == load_agent_skills()["dgov-ledger"]
