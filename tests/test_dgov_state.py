"""Unit tests for dgov status command (formerly dgov.state)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


class TestStatusCommand:
    def test_returns_expected_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.panes.list_worker_panes",
            lambda *a, **kw: [{"alive": True}, {"alive": False}],
        )
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert data["total"] == 2
        assert data["alive"] == 1

    def test_empty_panes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.panes.list_worker_panes", lambda *a, **kw: [])
        monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")
        runner = CliRunner()
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert data["total"] == 0
        assert data["alive"] == 0
