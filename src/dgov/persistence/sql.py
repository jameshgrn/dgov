from __future__ import annotations

"""SQL Table Definitions for dgov.

Only tables actively used by the Lacustrine kernel are initialized.
"""

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS tasks (
    slug TEXT PRIMARY KEY,
    prompt TEXT,
    task_id TEXT,
    agent TEXT,
    project_root TEXT,
    worktree_path TEXT,
    branch_name TEXT,
    created_at REAL,
    owns_worktree INTEGER,
    base_sha TEXT,
    provenance TEXT NOT NULL DEFAULT '{"kind": "original"}',
    role TEXT DEFAULT 'worker',
    state TEXT,
    metadata TEXT,
    file_claims TEXT NOT NULL DEFAULT '[]',
    commit_message TEXT DEFAULT NULL
)"""

_CREATE_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    pane TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    commit_count TEXT DEFAULT NULL,
    error TEXT DEFAULT NULL,
    reason TEXT DEFAULT NULL,
    merge_sha TEXT DEFAULT NULL,
    branch TEXT DEFAULT NULL,
    new_slug TEXT DEFAULT NULL,
    target_agent TEXT DEFAULT NULL,
    message TEXT DEFAULT NULL)
"""

_CREATE_SLUG_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS slug_history (
    slug TEXT PRIMARY KEY,
    used_at TEXT NOT NULL)
"""
