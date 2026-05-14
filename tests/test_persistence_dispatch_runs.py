from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dgov.dispatch_run import DispatchRun
from dgov.persistence import clear_connection_cache
from dgov.persistence.connection import _get_db
from dgov.persistence.dispatch_runs import (
    get_dispatch_run,
    get_dispatch_runs_for_unit,
    list_dispatch_runs,
    save_dispatch_run,
)
from dgov.persistence.schema import _SCHEMA_VERSION

pytestmark = pytest.mark.unit


def _dt(second: int = 0) -> datetime:
    return datetime(2026, 5, 14, 12, 0, second, tzinfo=UTC)


def _dispatch_run(
    *,
    plan_id: str = "plan-a",
    unit_slug: str = "unit-a",
    state: str = "active",
    dispatched_at: datetime | None = None,
) -> DispatchRun:
    run = DispatchRun(
        from_plan_id=plan_id,
        unit_slug=unit_slug,
        worktree_id=f"/tmp/{unit_slug}",
        branch=f"dgov/{unit_slug}",
        base_commit="abc123",
        agent_model="codex",
        effective_sop_set_hash="effective",
        drift_against_plan=True,
        drift_evidence=("metadata:modified=bundle:sop_set_hash",),
        dispatched_by="watermaster:test",
        dispatched_at=dispatched_at or _dt(),
    ).start_active()
    if state == "done":
        return run.complete_done(
            exit_code=0,
            output_dir="/tmp/out",
            prompt_tokens=1,
            completion_tokens=2,
            iteration_count=3,
            terminated_at=_dt(30),
        )
    return run


@pytest.fixture(autouse=True)
def _clear_db_cache() -> Iterator[None]:
    clear_connection_cache()
    yield
    clear_connection_cache()


def test_save_and_get_dispatch_run_round_trips(tmp_path: Path) -> None:
    run = _dispatch_run()

    save_dispatch_run(str(tmp_path), run)
    row = get_dispatch_run(str(tmp_path), run.id)

    assert row is not None
    assert row["id"] == run.id
    assert row["drift_against_plan"] is True
    assert row["drift_evidence"] == ("metadata:modified=bundle:sop_set_hash",)
    assert row["dispatched_at"] == run.dispatched_at.isoformat()
    assert row["terminated_at"] is None


def test_save_dispatch_run_is_idempotent(tmp_path: Path) -> None:
    run = _dispatch_run()

    save_dispatch_run(str(tmp_path), run)
    save_dispatch_run(str(tmp_path), run)

    conn = _get_db(str(tmp_path))
    count = conn.execute("SELECT COUNT(*) FROM dispatch_runs").fetchone()[0]
    assert count == 1


def test_terminal_state_replaces_prior_active_state(tmp_path: Path) -> None:
    run = _dispatch_run()
    terminal = run.complete_done(
        exit_code=0,
        output_dir="/tmp/out",
        prompt_tokens=4,
        completion_tokens=5,
        iteration_count=6,
        terminated_at=_dt(10),
    )

    save_dispatch_run(str(tmp_path), run)
    save_dispatch_run(str(tmp_path), terminal)

    row = get_dispatch_run(str(tmp_path), run.id)
    assert row is not None
    assert row["state"] == "done"
    assert row["prompt_tokens"] == 4
    assert row["terminated_at"] == _dt(10).isoformat()


def test_list_dispatch_runs_filters(tmp_path: Path) -> None:
    save_dispatch_run(str(tmp_path), _dispatch_run(plan_id="plan-a", unit_slug="unit-a"))
    save_dispatch_run(str(tmp_path), _dispatch_run(plan_id="plan-a", unit_slug="unit-b"))
    save_dispatch_run(str(tmp_path), _dispatch_run(plan_id="plan-b", unit_slug="unit-a"))
    save_dispatch_run(
        str(tmp_path),
        _dispatch_run(plan_id="plan-b", unit_slug="unit-c", state="done"),
    )

    assert len(list_dispatch_runs(str(tmp_path), plan_id="plan-a")) == 2
    assert len(list_dispatch_runs(str(tmp_path), unit_slug="unit-a")) == 2
    assert len(list_dispatch_runs(str(tmp_path), state="done")) == 1


def test_get_dispatch_runs_for_unit_orders_by_dispatched_at(tmp_path: Path) -> None:
    later = _dispatch_run(unit_slug="unit-a", dispatched_at=_dt(2))
    earlier = _dispatch_run(unit_slug="unit-a", dispatched_at=_dt(1))

    save_dispatch_run(str(tmp_path), later)
    save_dispatch_run(str(tmp_path), earlier)

    rows = get_dispatch_runs_for_unit(str(tmp_path), "unit-a")
    assert [row["id"] for row in rows] == [earlier.id, later.id]


def test_dispatch_runs_table_created_on_db_initialization(tmp_path: Path) -> None:
    conn = _get_db(str(tmp_path))

    table_name = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'dispatch_runs'"
    ).fetchone()

    assert table_name is not None


def test_schema_version_is_9() -> None:
    assert _SCHEMA_VERSION == 9
