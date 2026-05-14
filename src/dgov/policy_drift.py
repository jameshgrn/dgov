"""Policy source drift checks for the dgov source repository."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import cast

GUIDANCE_FILENAMES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")
_EXPECTED_BOOTSTRAP_FORCE_INCLUDE = {
    ".dgov/governor.md": "dgov/bootstrap_policy_data/governor.md",
    ".dgov/sops": "dgov/bootstrap_policy_data/sops",
}


def find_policy_drift(project_root: Path) -> list[str]:
    """Return policy source/canon drift issues visible in the current source tree."""
    issues: list[str] = []
    issues.extend(_guidance_file_drift(project_root))
    issues.extend(_bootstrap_policy_drift(project_root))
    return issues


def _guidance_file_drift(project_root: Path) -> list[str]:
    paths = [project_root / name for name in GUIDANCE_FILENAMES]
    existing = [path for path in paths if path.exists()]
    if not existing:
        return []
    missing = [path.name for path in paths if not path.exists()]
    if missing:
        return [f"Missing guidance mirror(s): {', '.join(missing)}"]

    canonical = paths[0].read_text()
    drifted = [path.name for path in paths[1:] if path.read_text() != canonical]
    if drifted:
        return [f"Guidance mirror drift: AGENTS.md differs from {', '.join(drifted)}"]
    return []


def _bootstrap_policy_drift(project_root: Path) -> list[str]:
    source_policy_dir = project_root / "src" / "dgov" / "bootstrap_policy_data"
    repo_policy_dir = project_root / ".dgov"
    if not source_policy_dir.is_dir() or not repo_policy_dir.is_dir():
        return []

    issues: list[str] = []
    mirrored_assets = _bootstrap_policy_asset_mirrors(source_policy_dir)
    if mirrored_assets:
        issues.append(
            "Bootstrap policy assets are mirrored under "
            f"src/dgov/bootstrap_policy_data: {', '.join(mirrored_assets)}"
        )
    issues.extend(_bootstrap_policy_build_mapping_drift(project_root))
    return issues


def _bootstrap_policy_asset_mirrors(source_policy_dir: Path) -> list[str]:
    mirrored: list[str] = []
    if (source_policy_dir / "governor.md").is_file():
        mirrored.append("governor.md")
    mirrored.extend(
        f"sops/{path.name}" for path in sorted((source_policy_dir / "sops").glob("*.md"))
    )
    return mirrored


def _bootstrap_policy_build_mapping_drift(project_root: Path) -> list[str]:
    force_include = _wheel_force_include(project_root / "pyproject.toml")
    missing = [
        f"{source} -> {target}"
        for source, target in _EXPECTED_BOOTSTRAP_FORCE_INCLUDE.items()
        if force_include.get(source) != target
    ]
    if not missing:
        return []
    return ["Bootstrap policy wheel force-include missing: " + ", ".join(missing)]


def _wheel_force_include(pyproject: Path) -> dict[str, str]:
    data = _read_pyproject(pyproject)
    tool = _mapping(data.get("tool"))
    hatch = _mapping(tool.get("hatch"))
    build = _mapping(hatch.get("build"))
    targets = _mapping(build.get("targets"))
    wheel = _mapping(targets.get("wheel"))
    force_include = _mapping(wheel.get("force-include"))
    return {
        str(source): target
        for source, target in force_include.items()
        if isinstance(source, str) and isinstance(target, str)
    }


def _read_pyproject(pyproject: Path) -> dict[str, object]:
    if not pyproject.is_file():
        return {}
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return cast("dict[str, object]", data)


def _mapping(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}
