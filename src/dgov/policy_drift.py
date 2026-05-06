"""Policy drift checks for the dgov source repository."""

from __future__ import annotations

from pathlib import Path

GUIDANCE_FILENAMES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")


def find_policy_drift(project_root: Path) -> list[str]:
    """Return policy/canon drift issues visible in the current source tree."""
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
    repo_governor = repo_policy_dir / "governor.md"
    source_governor = source_policy_dir / "governor.md"
    if not repo_governor.exists() or not source_governor.exists():
        issues.append("Missing governor.md in repo policy or bootstrap policy assets")
    elif repo_governor.read_text() != source_governor.read_text():
        issues.append(".dgov/governor.md differs from bootstrap policy asset")

    repo_sops = _markdown_files(repo_policy_dir / "sops")
    source_sops = _markdown_files(source_policy_dir / "sops")
    if set(repo_sops) != set(source_sops):
        missing_from_assets = sorted(set(repo_sops) - set(source_sops))
        missing_from_repo = sorted(set(source_sops) - set(repo_sops))
        if missing_from_assets:
            issues.append(f"SOP missing from bootstrap assets: {', '.join(missing_from_assets)}")
        if missing_from_repo:
            issues.append(f"SOP missing from repo policy: {', '.join(missing_from_repo)}")

    for name in sorted(set(repo_sops) & set(source_sops)):
        if repo_sops[name].read_text() != source_sops[name].read_text():
            issues.append(f".dgov/sops/{name} differs from bootstrap policy asset")
    return issues


def _markdown_files(directory: Path) -> dict[str, Path]:
    if not directory.is_dir():
        return {}
    return {path.name: path for path in sorted(directory.glob("*.md"))}
