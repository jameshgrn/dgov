"""Plan archiving — move completed or abandoned plans out of the active plans directory."""

from __future__ import annotations

import shutil
from pathlib import Path


def archive_plan(plan_dir: Path) -> Path:
    """Move plan_dir to .dgov/plans/archive/<name>/. Returns the destination path."""
    dest = plan_dir.parent / "archive" / plan_dir.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(plan_dir), dest)
    return dest
