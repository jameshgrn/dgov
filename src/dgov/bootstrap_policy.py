"""Runtime-owned canonical bootstrap policy pack."""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final


def _source_checkout_policy_dir() -> Path | None:
    """Return repo-local policy assets when running from a source checkout."""
    module_path = Path(__file__).resolve()
    for parent in module_path.parents:
        source_module = parent / "src" / "dgov" / "bootstrap_policy.py"
        policy_dir = parent / ".dgov"
        if not source_module.is_file() or not (policy_dir / "governor.md").is_file():
            continue
        try:
            if source_module.resolve() == module_path:
                return policy_dir
        except OSError:
            continue
    return None


def _read_text(path: Traversable) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Missing bootstrap policy asset: {path}") from exc


def _read_repo_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Missing source policy asset: {path}") from exc


def _read_repo_policy(policy_dir: Path) -> tuple[str, dict[str, str]]:
    sops_dir = policy_dir / "sops"
    return (
        _read_repo_text(policy_dir / "governor.md"),
        {
            path.name: _read_repo_text(path)
            for path in sorted(sops_dir.glob("*.md"), key=lambda item: item.name)
        },
    )


def _read_packaged_policy() -> tuple[str, dict[str, str]]:
    policy_dir: Traversable = files("dgov.bootstrap_policy_data")
    sops_dir = policy_dir.joinpath("sops")
    return (
        _read_text(policy_dir.joinpath("governor.md")),
        {
            path.name: _read_text(path)
            for path in sorted(sops_dir.iterdir(), key=lambda item: item.name)
            if path.name.endswith(".md")
        },
    )


_POLICY = (
    _read_repo_policy(source_policy_dir)
    if (source_policy_dir := _source_checkout_policy_dir()) is not None
    else _read_packaged_policy()
)

GOVERNOR_CHARTER: Final[str] = _POLICY[0]
SOP_FILES: Final[dict[str, str]] = _POLICY[1]
BOOTSTRAP_SOP_FILENAMES: Final[tuple[str, ...]] = tuple(SOP_FILES)

if not SOP_FILES:
    raise RuntimeError("No bootstrap SOP assets found")
