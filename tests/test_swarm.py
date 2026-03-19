from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dgov.cli.swarm import swarm


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_session(tmp_path):
    session = tmp_path / "test_session"
    session.mkdir()
    (session / ".dgov").mkdir()
    return str(session)


@patch("dgov.lifecycle.create_worker_pane")
@patch("dgov.persistence.emit_event")
def test_think_command(mock_emit, mock_create, runner, mock_session):
    mock_pane = MagicMock()
    mock_pane.slug = "test-think-slug"
    mock_pane.pane_id = "%1"
    mock_create.return_value = mock_pane

    result = runner.invoke(swarm, ["think", "-s", mock_session, "-p", "Test prompt"])

    assert result.exit_code == 0
    mock_create.assert_called_once()
    assert mock_create.call_args[1]["role"] == "reasoner"
    mock_emit.assert_called_with(
        mock_session, "think_started", "test-think-slug", agent="qwen-35b", prompt="Test prompt"
    )
    assert "test-think-slug" in result.output


@patch("dgov.lifecycle.create_worker_pane")
@patch("dgov.persistence.emit_event")
def test_convo_command(mock_emit, mock_create, runner, mock_session):
    mock_host = MagicMock()
    mock_host.slug = "host-slug"
    mock_host.pane_id = "%1"

    mock_part = MagicMock()
    mock_part.slug = "part-slug"

    mock_create.side_effect = [mock_host, mock_part, mock_part]

    result = runner.invoke(
        swarm, ["convo", "-s", mock_session, "-a", "agent1", "-a", "agent2", "-p", "Init"]
    )

    assert result.exit_code == 0
    assert mock_create.call_count == 3
    mock_emit.assert_called_once()
    assert mock_emit.call_args[0][1] == "convo_started"
    assert "host-slug" in result.output


@patch("dgov.lifecycle.create_worker_pane")
@patch("dgov.persistence.emit_event")
def test_watch_command(mock_emit, mock_create, runner, mock_session):
    mock_watcher = MagicMock()
    mock_watcher.slug = "watcher-slug"
    mock_watcher.pane_id = "%1"
    mock_create.return_value = mock_watcher

    # We need to mock the polling loop or it will hang
    with patch("time.sleep", side_effect=KeyboardInterrupt):
        with patch("dgov.persistence.read_events", return_value=[]):
            result = runner.invoke(
                swarm, ["watch", "-s", mock_session, "-t", "target", "-p", "pattern"]
            )

    assert result.exit_code == 0
    mock_create.assert_called_once()
    assert mock_create.call_args[1]["role"] == "watcher"
    mock_emit.assert_any_call(
        mock_session,
        "watch_started",
        "watcher-slug",
        target_slug="target",
        pattern="pattern",
        threshold=0.8,
        interval=1.0,
    )
