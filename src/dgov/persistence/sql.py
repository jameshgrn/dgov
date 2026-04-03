"""SQL Table Definitions for dgov.

Extracted from schema.py to improve structural equality.
"""

_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS panes (
    slug TEXT PRIMARY KEY,
    prompt TEXT,
    pane_id TEXT,
    agent TEXT,
    project_root TEXT,
    worktree_path TEXT,
    branch_name TEXT,
    created_at REAL,
    owns_worktree INTEGER,
    base_sha TEXT,
    provenance TEXT NOT NULL DEFAULT '{"kind": "original"}',  -- JSON discriminated union
    role TEXT DEFAULT 'worker',
    state TEXT,
    metadata TEXT,
    file_claims TEXT NOT NULL DEFAULT '[]',
    commit_message TEXT DEFAULT NULL,
    circuit_breaker INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 0,
    monitor_reason TEXT DEFAULT NULL,
    last_checkpoint TEXT DEFAULT NULL,
    last_hook_match TEXT DEFAULT NULL,
    preserve_reason TEXT DEFAULT NULL,
    preserve_recoverable INTEGER NOT NULL DEFAULT 0
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

_CREATE_DAG_RUNS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_file TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL,
    current_tier INTEGER NOT NULL DEFAULT 0,
    state_json TEXT NOT NULL DEFAULT '{}',
    definition_json TEXT NOT NULL DEFAULT '{}'
)"""

_CREATE_DAG_TASKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_run_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL,
    agent TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    pane_slug TEXT,
    worktree_path TEXT,
    file_claims TEXT NOT NULL DEFAULT '[]',
    commit_message TEXT DEFAULT NULL,
    error TEXT,
    UNIQUE(dag_run_id, slug),
    FOREIGN KEY (dag_run_id) REFERENCES dag_runs(id)
)"""

_CREATE_DAG_EVALS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_evals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_run_id INTEGER NOT NULL,
    eval_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    statement TEXT NOT NULL,
    evidence TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '[]',
    UNIQUE(dag_run_id, eval_id),
    FOREIGN KEY (dag_run_id) REFERENCES dag_runs(id)
)"""

_CREATE_DAG_UNIT_EVAL_LINKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_unit_eval_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_run_id INTEGER NOT NULL,
    unit_slug TEXT NOT NULL,
    eval_id TEXT NOT NULL,
    UNIQUE(dag_run_id, unit_slug, eval_id),
    FOREIGN KEY (dag_run_id) REFERENCES dag_runs(id)
)"""

_CREATE_DAG_EVAL_RESULTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS dag_eval_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dag_run_id INTEGER NOT NULL,
    eval_id TEXT NOT NULL,
    passed INTEGER NOT NULL,
    exit_code INTEGER,
    output TEXT NOT NULL DEFAULT '',
    verified_at TEXT NOT NULL,
    UNIQUE(dag_run_id, eval_id),
    FOREIGN KEY (dag_run_id) REFERENCES dag_runs(id)
)"""

_CREATE_MERGE_QUEUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS merge_queue (
    ticket TEXT PRIMARY KEY,
    branch TEXT NOT NULL,
    requester TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    result TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed_at TIMESTAMP
)"""

_CREATE_DECISION_JOURNAL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS decision_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    trace_id TEXT,
    model_id TEXT,
    confidence REAL,
    pane_slug TEXT,
    agent_id TEXT,
    request_json TEXT NOT NULL,
    result_json TEXT,
    error TEXT,
    duration_ms REAL NOT NULL
)"""

_CREATE_SLUG_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS slug_history (
    slug TEXT PRIMARY KEY,
    used_at TEXT NOT NULL)
"""
