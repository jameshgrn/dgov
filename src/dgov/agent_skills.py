"""Runtime-owned dgov machine-agent skills."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path


@dataclass(frozen=True)
class SkillSyncResult:
    """Summary of a dgov agent skill sync."""

    created: tuple[Path, ...]
    updated: tuple[Path, ...]
    unchanged: tuple[Path, ...]

    @property
    def changed(self) -> tuple[Path, ...]:
        return (*self.created, *self.updated)


def _source_checkout_skills_dir() -> Path | None:
    """Return repo-local skill assets when running from a source checkout."""
    module_path = Path(__file__).resolve()
    for parent in module_path.parents:
        source_module = parent / "src" / "dgov" / "agent_skills.py"
        skills_dir = parent / "agent-guidance" / "skills"
        if not source_module.is_file() or not skills_dir.is_dir():
            continue
        try:
            if source_module.resolve() == module_path:
                return skills_dir
        except OSError:
            continue
    return None


def _read_text(path: Traversable) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Missing agent skill asset: {path}") from exc


def _read_repo_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Missing source agent skill asset: {path}") from exc


def _read_repo_skills(skills_dir: Path) -> dict[str, str]:
    return {
        path.parent.name: _read_repo_text(path)
        for path in sorted(skills_dir.glob("*/SKILL.md"), key=lambda item: item.parent.name)
    }


def _read_packaged_skills() -> dict[str, str]:
    skills_dir: Traversable = files("dgov.agent_skill_data").joinpath("skills")
    skills: dict[str, str] = {}
    for path in sorted(skills_dir.iterdir(), key=lambda item: item.name):
        skill_file = path.joinpath("SKILL.md")
        if skill_file.is_file():
            skills[path.name] = _read_text(skill_file)
    return skills


def load_agent_skills() -> dict[str, str]:
    """Return shipped dgov agent skills keyed by skill directory name."""
    skills = (
        _read_repo_skills(source_skills_dir)
        if (source_skills_dir := _source_checkout_skills_dir()) is not None
        else _read_packaged_skills()
    )
    if not skills:
        raise RuntimeError("No dgov agent skill assets found")
    return skills


def sync_agent_skills(skills_dir: Path) -> SkillSyncResult:
    """Install or update shipped dgov skills in a local agent skill directory."""
    target_root = skills_dir.expanduser()
    skills = load_agent_skills()
    created: list[Path] = []
    updated: list[Path] = []
    unchanged: list[Path] = []

    for name, content in sorted(skills.items()):
        skill_file = target_root / name / "SKILL.md"
        if skill_file.exists() and skill_file.read_text(encoding="utf-8") == content:
            unchanged.append(skill_file)
            continue
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        existed = skill_file.exists()
        skill_file.write_text(content, encoding="utf-8")
        if existed:
            updated.append(skill_file)
        else:
            created.append(skill_file)

    return SkillSyncResult(tuple(created), tuple(updated), tuple(unchanged))
