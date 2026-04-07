"""Tests for operational ledger."""

from __future__ import annotations

import pytest

from dgov.persistence import (
    add_ledger_entry,
    clear_connection_cache,
    list_ledger_entries,
    resolve_ledger_entry,
)


@pytest.fixture
def session_root(tmp_path):
    root = str(tmp_path)
    clear_connection_cache()
    return root


@pytest.mark.unit
def test_ledger_crud(session_root):
    # 1. Add entries
    id1 = add_ledger_entry(session_root, "bug", "Nasty bug in kernel")
    id2 = add_ledger_entry(session_root, "rule", "No global state")
    id3 = add_ledger_entry(session_root, "debt", "Refactor worker.py")

    assert id1 == 1
    assert id2 == 2
    assert id3 == 3

    # 2. List all (open by default)
    entries = list_ledger_entries(session_root)
    assert len(entries) == 3
    assert entries[0].content == "Refactor worker.py"  # ordered by DESC created_at

    # 3. Filter by category
    bugs = list_ledger_entries(session_root, category="bug")
    assert len(bugs) == 1
    assert bugs[0].content == "Nasty bug in kernel"

    # 4. Filter by query
    kernel_entries = list_ledger_entries(session_root, query="kernel")
    assert len(kernel_entries) == 1
    assert kernel_entries[0].content == "Nasty bug in kernel"

    worker_entries = list_ledger_entries(session_root, query="worker")
    assert len(worker_entries) == 1
    assert worker_entries[0].content == "Refactor worker.py"

    empty = list_ledger_entries(session_root, query="missing")
    assert len(empty) == 0

    # 5. Resolve an entry
    assert resolve_ledger_entry(session_root, id1) is True

    # 6. List open vs resolved
    open_entries = list_ledger_entries(session_root, status="open")
    assert len(open_entries) == 2

    resolved_entries = list_ledger_entries(session_root, status="resolved")
    assert len(resolved_entries) == 1
    assert resolved_entries[0].id == id1
    assert resolved_entries[0].status == "resolved"


@pytest.mark.unit
def test_resolve_missing_entry(session_root):
    assert resolve_ledger_entry(session_root, 999) is False
