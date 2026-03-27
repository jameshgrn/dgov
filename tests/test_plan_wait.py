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
