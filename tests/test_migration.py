"""Tests for schema migration logic."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dgov.persistence.connection import _get_db, clear_connection_cache
from dgov.persistence.events import read_events
from dgov.persistence.schema import state_path


@pytest.mark.unit
def test_migrate_schema_adds_missing_columns(tmp_path: Path, monkeypatch):
    """Existing databases without new columns should be automatically migrated."""
    session_root = str(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    db_file = state_path(session_root)
    db_file.parent.mkdir(parents=True)

    # 1. Create a "legacy" v5-like schema (missing task_slug, plan_name, action)
    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE tasks (slug TEXT PRIMARY KEY, prompt TEXT)")
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, event TEXT, pane TEXT)")
    conn.close()

    # 2. Trigger connection (should run migration)
    clear_connection_cache()
    conn = _get_db(session_root)

    # 3. Verify 'events' columns
    cursor = conn.execute("PRAGMA table_info(events)")
    cols = {row[1] for row in cursor.fetchall()}
    assert "data" in cols
    assert "task_slug" in cols
    assert "plan_name" in cols
    assert "action" in cols
    assert "commit_count" in cols
    assert "error" in cols
    assert "reason" in cols
    assert "merge_sha" in cols
    assert "branch" in cols
    assert "new_slug" in cols
    assert "target_agent" in cols
    assert "message" in cols
    assert "run_source" in cols

    # 4. Verify 'tasks' columns
    cursor = conn.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in cursor.fetchall()}
    assert "plan_name" in cols

    conn.close()


@pytest.mark.unit
def test_migrate_legacy_events_schema_read_events_works(tmp_path: Path, monkeypatch):
    """Legacy events table missing data/error/etc should migrate and allow read_events."""
    session_root = str(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    db_file = state_path(session_root)
    db_file.parent.mkdir(parents=True)

    conn = sqlite3.connect(str(db_file))
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, ts TEXT, event TEXT, pane TEXT)")
    conn.execute(
        "INSERT INTO events (ts, event, pane) VALUES ('2024-01-01T00:00:00', 'run_start', 'p1')"
    )
    conn.commit()
    conn.close()

    clear_connection_cache()
    events = read_events(session_root)
    assert len(events) == 1
    assert events[0]["event"] == "run_start"
    assert events[0]["pane"] == "p1"


@pytest.mark.unit
def test_get_db_retries_locked_connection_bootstrap(tmp_path: Path, monkeypatch) -> None:
    """Connection setup should retry if WAL/schema bootstrap hits a transient lock."""
    from dgov.persistence import connection as connection_module

    session_root = str(tmp_path)
    clear_connection_cache()
    monkeypatch.setattr(connection_module, "_LOCK_BACKOFF_S", 0)
    original_open = connection_module._open_db_connection
    calls = 0

    def _flaky_open(db_path: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        return original_open(db_path)

    monkeypatch.setattr(connection_module, "_open_db_connection", _flaky_open)

    conn = _get_db(session_root)

    assert calls == 2
    assert conn.execute("SELECT COUNT(*) FROM ledger").fetchone() is not None
