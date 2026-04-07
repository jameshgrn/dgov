"""Tests for schema migration logic."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dgov.persistence.connection import _get_db, clear_connection_cache
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
    assert "task_slug" in cols
    assert "plan_name" in cols
    assert "action" in cols

    # 4. Verify 'tasks' columns
    cursor = conn.execute("PRAGMA table_info(tasks)")
    cols = {row[1] for row in cursor.fetchall()}
    assert "plan_name" in cols

    conn.close()
