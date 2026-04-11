"""Tests for dgov run's quiet-by-default output and --stream / --verbose flags."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.cli.run import _make_worker_event_callback

pytestmark = pytest.mark.unit


def test_quiet_mode_suppresses_thoughts_and_calls(capsys: pytest.CaptureFixture) -> None:
    cb = _make_worker_event_callback(stream=False)
    cb("task-1", "thought", "thinking hard")
    cb("task-1", "call", {"tool": "read_file", "args": {"path": "a.py"}})
    err = capsys.readouterr().err
    assert err == ""


def test_quiet_mode_still_surfaces_errors_and_done(capsys: pytest.CaptureFixture) -> None:
    cb = _make_worker_event_callback(stream=False)
    cb("task-1", "error", "boom")
    cb("task-1", "done", "all clear")
    err = capsys.readouterr().err
    assert "ERROR: boom" in err
    assert "done: all clear" in err


def test_stream_mode_shows_thoughts_and_calls(capsys: pytest.CaptureFixture) -> None:
    cb = _make_worker_event_callback(stream=True)
    cb("task-1", "thought", "thinking hard")
    cb("task-1", "call", {"tool": "read_file", "args": {"path": "a.py"}})
    err = capsys.readouterr().err
    assert "thinking hard" in err
    assert "read_file(" in err
    assert "path=" in err


def test_run_help_lists_stream_and_verbose_flags() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "--stream" in result.output
    assert "-v, --verbose" in result.output
