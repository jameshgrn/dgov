from __future__ import annotations

"""SQL Table Definitions for dgov.

Only tables actively used by the Lacustrine kernel are initialized.
"""

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS tasks (
    slug TEXT PRIMARY KEY,
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
    plan_name TEXT DEFAULT NULL
)"""

_CREATE_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    pane TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    task_slug TEXT DEFAULT NULL,
    plan_name TEXT DEFAULT NULL,
    action TEXT DEFAULT NULL,
    commit_count TEXT DEFAULT NULL,
    error TEXT DEFAULT NULL,
    reason TEXT DEFAULT NULL,
    merge_sha TEXT DEFAULT NULL,
    branch TEXT DEFAULT NULL,
    new_slug TEXT DEFAULT NULL,
    target_agent TEXT DEFAULT NULL,
    message TEXT DEFAULT NULL,
    run_source TEXT DEFAULT NULL)
"""

_CREATE_SLUG_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS slug_history (
    slug TEXT PRIMARY KEY,
    used_at TEXT NOT NULL)
"""

_CREATE_LEDGER_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL, -- bug, rule, note, debt
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', -- open, resolved
    created_at REAL NOT NULL,
    resolved_at REAL DEFAULT NULL,
    affected_paths TEXT DEFAULT NULL, -- JSON array of affected file paths
    affected_tags TEXT DEFAULT NULL -- JSON array of tags
)"""

_CREATE_DISPATCH_RUNS_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS dispatch_runs (
    id TEXT PRIMARY KEY,
    from_plan_id TEXT NOT NULL,
    unit_slug TEXT NOT NULL,
    worktree_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    agent_model TEXT NOT NULL,
    effective_sop_set_hash TEXT NOT NULL,
    drift_against_plan INTEGER NOT NULL,
    drift_evidence TEXT NOT NULL DEFAULT '[]',
    run_source TEXT NOT NULL DEFAULT 'manual',
    retried_from TEXT,
    forked_from TEXT,
    retry_index INTEGER NOT NULL DEFAULT 0,
    fork_depth INTEGER NOT NULL DEFAULT 0,
    dispatched_by TEXT NOT NULL,
    dispatched_at TEXT NOT NULL,
    state TEXT NOT NULL,
    exit_code INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    output_dir TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    iteration_count INTEGER NOT NULL DEFAULT 0,
    terminated_at TEXT
)"""
