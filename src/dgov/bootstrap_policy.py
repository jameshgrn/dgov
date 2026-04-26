"""Runtime-owned canonical bootstrap policy pack."""

from __future__ import annotations

from importlib.resources import files
from importlib.resources.abc import Traversable
from typing import Final

_POLICY_DIR: Final[Traversable] = files("dgov.bootstrap_policy_data")
_SOPS_DIR: Final[Traversable] = _POLICY_DIR.joinpath("sops")


def _read_text(path: Traversable) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Missing bootstrap policy asset: {path}") from exc


GOVERNOR_CHARTER = _read_text(_POLICY_DIR.joinpath("governor.md"))
SOP_FILES = {
    path.name: _read_text(path)
    for path in sorted(_SOPS_DIR.iterdir(), key=lambda item: item.name)
    if path.name.endswith(".md")
}
BOOTSTRAP_SOP_FILENAMES = tuple(SOP_FILES)

if not SOP_FILES:
    raise RuntimeError(f"No bootstrap SOP assets found in {_SOPS_DIR}")
