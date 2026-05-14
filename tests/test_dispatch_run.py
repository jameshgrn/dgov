from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from dgov.dispatch_run import DispatchRun, derive_dispatch_run_id, derive_drift_evidence

pytestmark = pytest.mark.unit


def _dt(offset: int = 0) -> datetime:
    return datetime(2026, 5, 14, 12, 0, offset, tzinfo=UTC)


def _dispatch_run(**overrides: Any) -> DispatchRun:
    kwargs: dict[str, Any] = {
        "from_plan_id": "plan-a",
        "unit_slug": "unit-a",
        "worktree_id": "/tmp/worktree",
        "branch": "dgov/unit-a",
        "base_commit": "abc123",
        "agent_model": "codex",
        "effective_sop_set_hash": "hash-a",
        "drift_against_plan": False,
        "dispatched_by": "watermaster:test",
        "dispatched_at": _dt(),
    }
    kwargs.update(overrides)
    return DispatchRun(**kwargs)


def test_derive_dispatch_run_id_is_stable_and_strategy_tagged() -> None:
    first = _derive_id()
    second = _derive_id()
    changed = _derive_id(base_commit="def456")

    assert first == second
    assert first != changed
    assert first.startswith("disprun:")


def _derive_id(*, base_commit: str = "abc123") -> str:
    return derive_dispatch_run_id(
        from_plan_id="plan-a",
        unit_slug="unit-a",
        worktree_id="/tmp/worktree",
        branch="dgov/unit-a",
        base_commit=base_commit,
        agent_model="codex",
        effective_sop_set_hash="hash-a",
        retried_from=None,
        forked_from=None,
        retry_index=0,
        fork_depth=0,
        dispatched_at=_dt(),
    )


def test_init_rejects_retry_and_fork_together() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _dispatch_run(retried_from="a", forked_from="b", retry_index=1, fork_depth=1)


def test_init_rejects_counter_without_lineage() -> None:
    with pytest.raises(ValueError, match="retry_index"):
        _dispatch_run(retry_index=1)

    with pytest.raises(ValueError, match="fork_depth"):
        _dispatch_run(fork_depth=1)


def test_init_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError, match="dispatched_at"):
        _dispatch_run(dispatched_at=datetime(2026, 5, 14, 12, 0, 0))


def test_init_rejects_invalid_state() -> None:
    with pytest.raises(ValueError, match="Invalid DispatchRun state"):
        _dispatch_run(state="reviewing")


def test_start_active_returns_new_instance_with_same_id() -> None:
    run = _dispatch_run()
    active = run.start_active()

    assert run.state == "pending"
    assert active.state == "active"
    assert active.id == run.id


def test_start_active_rejects_non_pending() -> None:
    with pytest.raises(ValueError, match="active"):
        _dispatch_run().start_active().start_active()


def test_complete_done_populates_terminal_fields_and_preserves_id() -> None:
    active = _dispatch_run().start_active()
    terminal = active.complete_done(
        exit_code=0,
        output_dir="/tmp/out",
        prompt_tokens=10,
        completion_tokens=20,
        iteration_count=3,
        terminated_at=_dt(1),
    )

    assert terminal.state == "done"
    assert terminal.exit_code == 0
    assert terminal.output_dir == "/tmp/out"
    assert terminal.prompt_tokens == 10
    assert terminal.completion_tokens == 20
    assert terminal.iteration_count == 3
    assert terminal.terminated_at == _dt(1)
    assert terminal.id == active.id


@pytest.mark.parametrize(
    ("method_name", "expected_state", "kwargs"),
    [
        (
            "complete_failed",
            "failed",
            {"exit_code": 1, "last_error": "boom"},
        ),
        (
            "complete_timed_out",
            "timed_out",
            {"last_error": "Timed out after 1s"},
        ),
        (
            "complete_abandoned",
            "abandoned",
            {"last_error": "shutdown"},
        ),
    ],
)
def test_terminal_failure_transitions(
    method_name: str,
    expected_state: str,
    kwargs: dict[str, object],
) -> None:
    active = _dispatch_run().start_active()
    method = getattr(active, method_name)
    terminal = method(
        **kwargs,
        output_dir="/tmp/out",
        prompt_tokens=1,
        completion_tokens=2,
        iteration_count=3,
        terminated_at=_dt(1),
    )

    assert terminal.state == expected_state
    assert terminal.last_error
    assert terminal.id == active.id


def test_state_transitions_preserve_input_fields() -> None:
    active = _dispatch_run(
        drift_evidence=("metadata:modified=bundle:sop_set_hash",)
    ).start_active()
    terminal = active.complete_failed(
        exit_code=2,
        last_error="failed",
        output_dir="/tmp/out",
        prompt_tokens=1,
        completion_tokens=2,
        iteration_count=3,
        terminated_at=_dt(1),
    )

    assert terminal.from_plan_id == active.from_plan_id
    assert terminal.unit_slug == active.unit_slug
    assert terminal.worktree_id == active.worktree_id
    assert terminal.effective_sop_set_hash == active.effective_sop_set_hash
    assert terminal.drift_evidence == active.drift_evidence
    with pytest.raises(FrozenInstanceError):
        _assign_branch(terminal)


def test_clone_preserves_id_and_unchanged_fields() -> None:
    run = _dispatch_run()
    cloned = run._clone(last_error="changed")

    assert cloned.id == run.id
    assert cloned.branch == run.branch
    assert cloned.last_error == "changed"


def test_derive_drift_evidence() -> None:
    assert derive_drift_evidence(plan_hash="a", effective_hash="a") == ()
    assert derive_drift_evidence(plan_hash=None, effective_hash="a") == ()
    assert derive_drift_evidence(plan_hash="a", effective_hash="b") == (
        "metadata:modified=bundle:sop_set_hash",
    )


def test_rejects_non_utc_timezone() -> None:
    with pytest.raises(ValueError, match="UTC"):
        _dispatch_run(
            dispatched_at=datetime(2026, 5, 14, 12, 0, tzinfo=timezone(timedelta(hours=1)))
        )


def _assign_branch(run: Any) -> None:
    run.branch = "changed"
