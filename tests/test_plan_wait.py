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
