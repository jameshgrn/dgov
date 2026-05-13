"""Plan archiving — move completed or abandoned plans out of the active plans directory."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class ArchiveError(RuntimeError):
    """Raised when archiving would hide durable plan source from git."""


def archive_plan(plan_dir: Path) -> Path:
    """Move plan_dir to .dgov/plans/archive/<name>/. Returns the destination path."""
    dest = plan_dir.parent / "archive" / plan_dir.name
    _ensure_durable_archive_is_trackable(plan_dir, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(plan_dir), dest)
    return dest


def _ensure_durable_archive_is_trackable(plan_dir: Path, dest: Path) -> None:
    if not _is_durable_plan_dir(plan_dir):
        return
    repo_root = _git_repo_root(plan_dir)
    if repo_root is None:
        return
    probe = dest / "_root.toml"
    if not _is_git_ignored(repo_root, probe):
        return
    raise ArchiveError(
        "Refusing to archive durable plan source into ignored .dgov/plans/archive: "
        f"{dest}. Fix .gitignore so archived plan source is tracked, then retry."
    )


def _is_durable_plan_dir(plan_dir: Path) -> bool:
    parent = plan_dir.parent
    return parent.name == "plans" and parent.parent.name == ".dgov"


def _git_repo_root(cwd: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _is_git_ignored(repo_root: Path, path: Path) -> bool:
    try:
        rel = path.resolve(strict=False).relative_to(repo_root)
    except ValueError:
        return False
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", str(rel)],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
