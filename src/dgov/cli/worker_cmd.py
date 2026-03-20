"""Worker status reporting commands.

Called BY agents running inside worker panes to report progress and completion.
Uses DGOV_SLUG and DGOV_SESSION_ROOT env vars (injected by lifecycle.py).
"""

from __future__ import annotations

import json
import os
import sys

import click

from dgov.persistence import STATE_DIR


def _require_worker_env() -> tuple[str, str]:
    """Return (session_root, slug) from env vars or exit with error."""
    session_root = os.environ.get("DGOV_SESSION_ROOT", "")
    slug = os.environ.get("DGOV_SLUG", "")
    if not session_root or not slug:
        click.echo(
            "Error: DGOV_SESSION_ROOT and DGOV_SLUG must be set. "
            "This command is meant to be called by agents inside dgov worker panes.",
            err=True,
        )
        sys.exit(1)
    return session_root, slug


@click.group()
def worker():
    """Report worker status (called by agents, not humans)."""


@worker.command("complete")
@click.option("--message", "-m", default="", help="Completion message")
def worker_complete(message):
    """Signal that this worker has finished its task successfully."""
    import subprocess
    from pathlib import Path

    from dgov.persistence import emit_event, update_pane_state

    session_root, slug = _require_worker_env()
    worktree = os.environ.get("DGOV_WORKTREE_PATH", "")

    # Auto-commit uncommitted changes before signaling done
    if worktree and Path(worktree).is_dir():
        status = subprocess.run(
            ["git", "-C", worktree, "status", "--porcelain"],
            capture_output=True,
            text=True,
        )
        if status.stdout.strip():
            subprocess.run(
                ["git", "-C", worktree, "add", "-A"],
                capture_output=True,
            )
            # Unstage protected files so worker CLAUDE.md/AGENTS.md
            # don't get committed and cause merge conflicts
            for pf in ("CLAUDE.md", "AGENTS.md"):
                subprocess.run(
                    ["git", "-C", worktree, "reset", "HEAD", "--", pf],
                    capture_output=True,
                )
            commit_msg = message or f"Auto-commit from {slug}"
            subprocess.run(
                ["git", "-C", worktree, "commit", "-m", commit_msg],
                capture_output=True,
                env={**os.environ, "DGOV_SKIP_GOVERNOR_CHECK": "1"},
            )
            click.echo(json.dumps({"auto_committed": True, "slug": slug}), err=True)

    # Verify at least one commit exists beyond DGOV_BASE_SHA
    base_sha = os.environ.get("DGOV_BASE_SHA", "")
    if base_sha:
        log = subprocess.run(
            ["git", "-C", worktree, "log", f"{base_sha}..HEAD", "--oneline"],
            capture_output=True,
            text=True,
        )
        commits = [c for c in log.stdout.strip().split("\n") if c]
        if not commits:
            click.echo(
                f"Error: No commits found beyond DGOV_BASE_SHA ({base_sha}). "
                f"Cannot signal completion without actual repo changes.",
                err=True,
            )
            sys.exit(1)

    done_path = Path(session_root) / STATE_DIR / "done" / slug
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done_path.touch()
    update_pane_state(session_root, slug, "done")
    emit_event(session_root, "pane_done", slug, message=message)
    click.echo(json.dumps({"status": "complete", "slug": slug}))


@worker.command("fail")
@click.argument("reason", default="unspecified")
def worker_fail(reason):
    """Signal that this worker has failed."""
    from pathlib import Path

    from dgov.persistence import emit_event, update_pane_state

    session_root, slug = _require_worker_env()
    exit_path = Path(session_root) / STATE_DIR / "done" / f"{slug}.exit"
    exit_path.parent.mkdir(parents=True, exist_ok=True)
    exit_path.write_text(reason, encoding="utf-8")
    update_pane_state(session_root, slug, "failed")
    emit_event(session_root, "pane_failed", slug, reason=reason)
    click.echo(json.dumps({"status": "failed", "slug": slug, "reason": reason}))


@worker.command("checkpoint")
@click.argument("message")
def worker_checkpoint(message):
    """Record a progress checkpoint."""
    from dgov.persistence import emit_event, set_pane_metadata

    session_root, slug = _require_worker_env()
    set_pane_metadata(session_root, slug, last_checkpoint=message)
    emit_event(session_root, "checkpoint_created", slug, message=message)
    click.echo(json.dumps({"status": "checkpoint", "slug": slug, "message": message}))
