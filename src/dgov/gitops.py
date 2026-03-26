"""Low-level git plumbing helpers for worktree and branch management."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dgov.kernel import SemanticManifest


def _remove_worktree(project_root: str, worktree_path: str, branch_name: str) -> dict:
    result = subprocess.run(
        ["git", "-C", project_root, "worktree", "remove", "--force", worktree_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"success": False, "error": result.stderr.strip()}

    subprocess.run(["git", "-C", project_root, "branch", "-D", branch_name], capture_output=True)

    prune_result = subprocess.run(
        ["git", "-C", project_root, "worktree", "prune"],
        capture_output=True,
        text=True,
    )
    if prune_result.returncode != 0:
        return {"success": False, "error": prune_result.stderr.strip()}

    return {"success": True}


def build_manifest_on_completion(
    project_root: str,
    slug: str,
    base_sha: str,
    file_claims: tuple[str, ...] = (),
) -> "SemanticManifest":
    """Build a manifest from the worker's actual git diff after completion."""
    from dgov.kernel import SemanticManifest

    result = subprocess.run(
        ["git", "-C", project_root, "diff", "--name-only", f"{base_sha}..HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    paths_written = (
        tuple(f for f in result.stdout.strip().splitlines() if f) if result.returncode == 0 else ()
    )

    return SemanticManifest(
        base_sha=base_sha,
        file_claims=file_claims,
        paths_written=paths_written,
    )


def validate_manifest_freshness(
    project_root: str,
    manifest: "SemanticManifest",
) -> tuple[bool, list[str]]:
    """Check if main has changed files the worker wrote to since base_sha.

    Returns (is_fresh, stale_files). If stale_files is non-empty, the
    worker's changes may conflict with main.
    """
    if not manifest.base_sha or not manifest.paths_written:
        return True, []

    result = subprocess.run(
        ["git", "-C", project_root, "diff", "--name-only", f"{manifest.base_sha}..HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return True, []  # Can't check, assume fresh

    main_changed = set(f for f in result.stdout.strip().splitlines() if f)
    worker_written = set(manifest.paths_written)
    stale = sorted(main_changed & worker_written)
    return len(stale) == 0, stale
