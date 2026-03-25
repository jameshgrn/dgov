"""Shared test fixtures for dgov test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _cleanup_persistence_cache():
    """Close and clear cached SQLite connections between tests.

    The module-level _conn_cache in persistence.py accumulates one connection
    per unique (db_path, thread_id). In tests using tmp_path, each test creates
    a new DB path, leaking connections. This fixture closes them after every test,
    preventing file descriptor exhaustion and SQLite lock contention when multiple
    pytest processes run concurrently.
    """
    yield
    try:
        from dgov.persistence import _conn_cache, _conn_lock

        with _conn_lock:
            for conn in _conn_cache.values():
                try:
                    conn.close()
                except Exception:
                    pass
            _conn_cache.clear()
    except ImportError:
        pass
