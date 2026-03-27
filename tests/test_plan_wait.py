from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def test_wait_for_dag_exits_on_blocked(tmp_path, monkeypatch, capsys):
    from dgov.cli.plan_cmd import _wait_for_dag

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args: 0)
    monkeypatch.setattr(
        "dgov.persistence.wait_for_events",
        lambda *args, **kwargs: [
            {
                "id": 1,
                "event": "dag_blocked",
                "pane": "dag/42",
                "data": json.dumps(
                    {"dag_run_id": 42, "task": "task-1", "reason": "review_failed"}
                ),
            }
        ],
    )
    monkeypatch.setattr(
        "dgov.persistence.get_dag_run",
        lambda *args, **kwargs: {"id": 42, "status": "blocked", "eval_results": []},
    )

    with pytest.raises(SystemExit) as exc_info:
        _wait_for_dag(42)

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "DAG blocked" in output
    assert "task-1" in output
    assert "DAG run 42: blocked" in output


def test_wait_for_dag_exits_on_cancelled(tmp_path, monkeypatch, capsys):
    from dgov.cli.plan_cmd import _wait_for_dag

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args: 0)
    monkeypatch.setattr(
        "dgov.persistence.wait_for_events",
        lambda *args, **kwargs: [
            {
                "id": 1,
                "event": "dag_cancelled",
                "pane": "dag/42",
                "data": json.dumps({"dag_run_id": 42, "status": "cancelled"}),
            }
        ],
    )
    monkeypatch.setattr(
        "dgov.persistence.get_dag_run",
        lambda *args, **kwargs: {"id": 42, "status": "cancelled", "eval_results": []},
    )

    with pytest.raises(SystemExit) as exc_info:
        _wait_for_dag(42)

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    assert "DAG cancelled" in output
    assert "DAG run 42: cancelled" in output


def test_plan_cancel_uses_latest_open_run(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from dgov.cli.plan_cmd import plan_cmd

    plan_file = tmp_path / "plan.toml"
    plan_file.write_text(
        "[plan]\nversion = 1\nname = 'p'\ngoal = 'g'\n"
        "[units.u]\nsummary = 's'\nprompt = 'p'\ncommit_message = 'c'\n"
        "[units.u.files]\nedit = ['a.py']\n"
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.persistence.ensure_dag_tables", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "dgov.persistence.get_open_dag_run",
        lambda session_root, abs_path: {"id": 9, "dag_file": abs_path, "status": "running"},
    )
    monkeypatch.setattr(
        "dgov.executor.run_cancel_dag",
        lambda session_root, run_id: {
            "run_id": run_id,
            "status": "cancelled",
            "cancelled": [],
            "closed": [],
        },
    )

    runner = CliRunner()
    result = runner.invoke(plan_cmd, ["cancel", str(plan_file)], catch_exceptions=False)

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["run_id"] == 9
    assert payload["status"] == "cancelled"


def test_wait_for_dag_exits_on_dag_completed_without_evals_verified(tmp_path, monkeypatch, capsys):
    """
    Regression: dag_completed event without later evals_verified should exit
    when get_dag_run shows completed.
    """
    from dgov.cli.plan_cmd import _wait_for_dag

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args: 0)

    # Simulate dag_completed event but no evals_verified
    call_count = [0]

    def mock_wait_for_events(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return [
                {
                    "id": 1,
                    "event": "dag_completed",
                    "pane": "dag/42",
                    "data": json.dumps({"dag_run_id": 42, "status": "completed"}),
                }
            ]
        return []  # Empty batch on second call

    monkeypatch.setattr(
        "dgov.persistence.wait_for_events",
        mock_wait_for_events,
    )
    monkeypatch.setattr(
        "dgov.persistence.get_dag_run",
        lambda *args, **kwargs: {"id": 42, "status": "completed", "eval_results": []},
    )

    _wait_for_dag(42)

    # Should exit cleanly (no SystemExit) after seeing completed status
    output = capsys.readouterr().out
    assert "DAG completed" in output


def test_wait_for_dag_exits_on_terminal_state_after_timeout(tmp_path, monkeypatch, capsys):
    """Regression: timeout/empty event batch where get_dag_run is terminal should exit."""
    from dgov.cli.plan_cmd import _wait_for_dag

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args: 0)

    def mock_wait_for_events(*args, **kwargs):
        return []  # Empty batch simulates timeout

    monkeypatch.setattr(
        "dgov.persistence.wait_for_events",
        mock_wait_for_events,
    )
    # DAG already completed before wait started (bug #181 scenario)
    monkeypatch.setattr(
        "dgov.persistence.get_dag_run",
        lambda *args, **kwargs: {"id": 89, "status": "completed", "eval_results": []},
    )

    _wait_for_dag(89)


def test_wait_for_dag_preserves_evals_summary_when_arrives_in_time(tmp_path, monkeypatch, capsys):
    """Regression: evals_verified should still show richer summary when it arrives."""
    from dgov.cli.plan_cmd import _wait_for_dag

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args: 0)

    call_count = [0]

    def mock_wait_for_events(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return [
                {
                    "id": 1,
                    "event": "dag_completed",
                    "pane": "dag/42",
                    "data": json.dumps({"dag_run_id": 42, "status": "completed"}),
                }
            ]
        elif call_count[0] == 2:
            return [
                {
                    "id": 2,
                    "event": "evals_verified",
                    "pane": "dag/42",
                    "data": json.dumps({"dag_run_id": 42}),
                }
            ]
        return []

    monkeypatch.setattr(
        "dgov.persistence.wait_for_events",
        mock_wait_for_events,
    )

    def mock_get_dag_run(*args, **kwargs):
        run_id = args[1] if len(args) > 1 else kwargs.get("run_id")
        if run_id == 42:
            return {
                "id": 42,
                "status": "completed",
                "eval_results": [
                    {"passed": True, "eval_id": "test-1", "output": "test output"},
                    {"passed": False, "eval_id": "test-2", "output": "fail output"},
                ],
            }
        return {}

    monkeypatch.setattr("dgov.persistence.get_dag_run", mock_get_dag_run)

    with pytest.raises(SystemExit) as exc_info:
        _wait_for_dag(42)

    assert exc_info.value.code == 2

    output = capsys.readouterr().out
    assert "DAG completed" in output
    assert "[PASS] test-1" in output
    assert "[FAIL] test-2" in output
