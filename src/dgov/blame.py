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

    sha_to_slug: dict[str, str] = {}
    for ev in events:
        if ev.get("event") == "pane_merged" and ev.get("merge_sha"):
            sha_to_slug[ev["merge_sha"][:7]] = ev["pane"]
            sha_to_slug[ev["merge_sha"]] = ev["pane"]

    result = subprocess.run(
        [
            "git",
            "-C",
            project_root,
            "log",
            "--format=COMMIT:%H %s",
            "--name-only",
            "--follow",
            "--",
            rel_file,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"file": rel_file, "history": [], "error": result.stderr.strip()}

    commits: list[tuple[str, str, int]] = []
    cur_hash = cur_subject = ""
    cur_files = 0
    for line in result.stdout.splitlines():
        if line.startswith("COMMIT:"):
            if cur_hash:
                commits.append((cur_hash, cur_subject, cur_files))
            payload = line[7:]
            parts = payload.split(" ", 1)
            cur_hash = parts[0]
            cur_subject = parts[1] if len(parts) > 1 else ""
            cur_files = 0
        elif line.strip():
            cur_files += 1
    if cur_hash:
        commits.append((cur_hash, cur_subject, cur_files))

    history = []
    for commit_hash, subject, files_in_change in commits:
        slug = (
            sha_to_slug.get(commit_hash)
            or sha_to_slug.get(commit_hash[:7])
            or _extract_slug_from_subject(subject)
        )

        info = slug_info.get(slug, {}) if slug else {}
        agent = info.get("agent", "")
        prompt = info.get("prompt", "")
        merged_at = merge_times.get(slug, "") if slug else ""

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


def blame_lines(
    project_root: str,
    file_path: str,
    session_root: str | None = None,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    agent_filter: str | None = None,
) -> dict:
    """Line-level blame: attribute each line of a file to its agent/pane."""
    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root or project_root)

    abs_file = os.path.abspath(file_path) if os.path.isabs(file_path) else file_path
    rel_file = os.path.relpath(abs_file, project_root) if os.path.isabs(abs_file) else file_path

    full_path = os.path.join(project_root, rel_file)
    if not os.path.isfile(full_path):
        return {"file": rel_file, "lines": [], "error": f"File not found: {rel_file}"}

    events = _load_events(session_root)

    slug_info: dict[str, dict] = {}
    for ev in events:
        if ev.get("event") == "pane_created":
            slug_info[ev["pane"]] = {"agent": ev.get("agent", "unknown")}

    sha_to_slug: dict[str, str] = {}
    for ev in events:
        if ev.get("event") == "pane_merged" and ev.get("merge_sha"):
            sha_to_slug[ev["merge_sha"][:7]] = ev["pane"]
            sha_to_slug[ev["merge_sha"]] = ev["pane"]

    cmd = ["git", "-C", project_root, "blame", "--porcelain", "--", rel_file]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"file": rel_file, "lines": [], "error": result.stderr.strip()}

    raw_lines = _parse_porcelain_blame(result.stdout)

    # Filter to requested line range
    if start_line is not None or end_line is not None:
        lo = start_line or 1
        hi = end_line or float("inf")
        raw_lines = [e for e in raw_lines if lo <= e["line_no"] <= hi]

    # Annotate each line with agent attribution
    for entry in raw_lines:
        sha = entry["commit"]
        slug = sha_to_slug.get(sha) or sha_to_slug.get(sha[:7]) or None
        # Try subject-based fallback: look up full commit message for this SHA
        if slug is None:
            slug = _slug_from_sha_subject(project_root, sha, sha_to_slug)
        info = slug_info.get(slug, {}) if slug else {}
        entry["slug"] = slug or ""
        entry["agent"] = info.get("agent", "")

    # Apply agent filter
    if agent_filter:
        raw_lines = [e for e in raw_lines if e["agent"] == agent_filter]

    # Group consecutive lines with same attribution
    groups = _group_blame_lines(raw_lines)

    return {"file": rel_file, "lines": groups}


def _parse_porcelain_blame(output: str) -> list[dict]:
    """Parse git blame --porcelain output into per-line entries."""
    lines = output.split("\n")
    entries: list[dict] = []
    i = 0
    # Track header info by SHA (porcelain only emits full headers on first occurrence)
    seen_shas: dict[str, dict] = {}

    while i < len(lines):
        line = lines[i]
        if not line:
            i += 1
            continue

        # Blame entry header: <sha> <orig_line> <final_line> [<num_lines>]
        parts = line.split()
        if len(parts) < 3:
            i += 1
            continue

        sha = parts[0]
        # SHA must be 40 hex chars
        if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
            i += 1
            continue

        final_line = int(parts[2])
        i += 1

        # Read header lines until we hit the content line (tab-prefixed)
        author = ""
        if sha in seen_shas:
            author = seen_shas[sha].get("author", "")
        while i < len(lines):
            if lines[i].startswith("\t"):
                break
            if lines[i].startswith("author "):
                author = lines[i][7:]
                if sha not in seen_shas:
                    seen_shas[sha] = {}
                seen_shas[sha]["author"] = author
            i += 1

        # Content line
        content = ""
        if i < len(lines) and lines[i].startswith("\t"):
            content = lines[i][1:]  # strip leading tab
            i += 1

        entries.append(
            {
                "line_no": final_line,
                "commit": sha[:7],
                "author": author,
                "content": content,
            }
        )

    return entries


def _group_blame_lines(entries: list[dict]) -> list[dict]:
    """Group consecutive lines with identical attribution into ranges."""
    if not entries:
        return []

    groups: list[dict] = []
    cur = entries[0]
    group = {
        "line_start": cur["line_no"],
        "line_end": cur["line_no"],
        "commit": cur["commit"],
        "slug": cur.get("slug", ""),
        "agent": cur.get("agent", ""),
        "author": cur["author"],
        "content": [cur["content"]],
    }

    for entry in entries[1:]:
        same_attribution = (
            entry["commit"] == group["commit"]
            and entry.get("slug", "") == group["slug"]
            and entry.get("agent", "") == group["agent"]
            and entry["author"] == group["author"]
            and entry["line_no"] == group["line_end"] + 1
        )
        if same_attribution:
            group["line_end"] = entry["line_no"]
            group["content"].append(entry["content"])
        else:
            groups.append(group)
            group = {
                "line_start": entry["line_no"],
                "line_end": entry["line_no"],
                "commit": entry["commit"],
                "slug": entry.get("slug", ""),
                "agent": entry.get("agent", ""),
                "author": entry["author"],
                "content": [entry["content"]],
            }

    groups.append(group)
    return groups


def _slug_from_sha_subject(
    project_root: str, short_sha: str, sha_to_slug: dict[str, str]
) -> str | None:
    """Try to resolve a blame SHA to a slug via its merge parent's subject line."""
    # Check if this commit's merge parent is in our sha_to_slug map
    result = subprocess.run(
        [
            "git",
            "-C",
            project_root,
            "log",
            "--format=%H %s",
            "--merges",
            "--ancestry-path",
            f"{short_sha}..HEAD",
            "--reverse",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    for log_line in result.stdout.splitlines():
        parts = log_line.split(" ", 1)
        if len(parts) < 2:
            continue
        merge_sha = parts[0]
        subject = parts[1]
        # Check SHA first
        if merge_sha in sha_to_slug or merge_sha[:7] in sha_to_slug:
            return sha_to_slug.get(merge_sha) or sha_to_slug.get(merge_sha[:7])
        # Then try subject parsing
        slug = _extract_slug_from_subject(subject)
        if slug:
            return slug
    return None


def _extract_slug_from_subject(subject: str) -> str | None:
    m = re.match(r"Merge branch '([^']+)'", subject)
    if m:
        return m.group(1)
    m = re.match(r"Merge (\S+)", subject)
    if m:
        return m.group(1)
    return None
