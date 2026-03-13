"""Blame: query event journal + git history to attribute file changes to agents."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


def blame_file(
    project_root: str,
    file_path: str,
    session_root: str | None = None,
    *,
    last_only: bool = True,
    agent_filter: str | None = None,
) -> dict:
    """Attribute changes to a file back to the agent/pane that produced them."""
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)

    abs_file = os.path.abspath(file_path) if os.path.isabs(file_path) else file_path
    rel_file = os.path.relpath(abs_file, project_root) if os.path.isabs(abs_file) else file_path

    events = _load_events(session_root)

    slug_info: dict[str, dict] = {}
    for ev in events:
        if ev.get("event") == "pane_created":
            slug_info[ev["pane"]] = {
                "agent": ev.get("agent", "unknown"),
                "prompt": ev.get("prompt", ""),
                "created_at": ev.get("ts", ""),
            }

    merge_times: dict[str, str] = {}
    for ev in events:
        if ev.get("event") == "pane_merged":
            merge_times[ev["pane"]] = ev.get("ts", "")

    result = subprocess.run(
        ["git", "-C", project_root, "log", "--format=%H %s", "--follow", "--", rel_file],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"file": rel_file, "history": [], "error": result.stderr.strip()}

    history = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split(" ", 1)
        commit_hash = parts[0]
        subject = parts[1] if len(parts) > 1 else ""

        slug = _extract_slug_from_subject(subject)

        info = slug_info.get(slug, {}) if slug else {}
        agent = info.get("agent", "")
        prompt = info.get("prompt", "")
        merged_at = merge_times.get(slug, "") if slug else ""

        files_result = subprocess.run(
            [
                "git",
                "-C",
                project_root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                commit_hash,
            ],
            capture_output=True,
            text=True,
        )
        files_in_change = (
            len(files_result.stdout.strip().splitlines()) if files_result.returncode == 0 else 0
        )

        entry = {
            "commit": commit_hash[:7],
            "subject": subject,
            "slug": slug or "",
            "agent": agent,
            "prompt": prompt,
            "merged_at": merged_at,
            "files_in_change": files_in_change,
        }

        if agent_filter and agent != agent_filter:
            continue

        history.append(entry)

        if last_only:
            break

    return {"file": rel_file, "history": history}


def _load_events(session_root: str) -> list[dict]:
    events_path = Path(session_root) / ".dgov" / "events.jsonl"
    if not events_path.exists():
        return []
    events = []
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def _extract_slug_from_subject(subject: str) -> str | None:
    m = re.match(r"Merge branch '([^']+)'", subject)
    if m:
        return m.group(1)
    m = re.match(r"Merge (\S+)", subject)
    if m:
        return m.group(1)
    return None
