"""Unit tests for dgov.state — status checks and health probes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.state import _check_kerberos_ticket, _check_tunnel_health, get_status

pytestmark = pytest.mark.unit


class TestCheckTunnelHealth:
    def test_all_ports_up(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock = MagicMock()
        mock.stdout = "200"
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        result = _check_tunnel_health()
        assert result["any_up"] is True
        assert all(v == "up" for v in result["ports"].values())

    def test_all_ports_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock = MagicMock()
        mock.stdout = "000"
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        result = _check_tunnel_health()
        assert result["any_up"] is False
        assert all(v == "down" for v in result["ports"].values())

    def test_mixed_ports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_count = {"n": 0}

        def fake_run(*a, **kw):
            call_count["n"] += 1
            mock = MagicMock()
            mock.stdout = "200" if call_count["n"] <= 2 else "000"
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_tunnel_health()
        assert result["any_up"] is True
        up_count = sum(1 for v in result["ports"].values() if v == "up")
        assert up_count == 2

    def test_timeout_marks_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired("curl", 5)

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_tunnel_health()
        assert result["any_up"] is False
        assert all(v == "down" for v in result["ports"].values())

    def test_os_error_marks_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*a, **kw):
            raise OSError("no curl")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_tunnel_health()
        assert result["any_up"] is False

    def test_checks_all_four_ports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            mock = MagicMock()
            mock.stdout = "200"
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_tunnel_health()
        assert len(result["ports"]) == 4
        assert set(result["ports"].keys()) == {8080, 8081, 8082, 8083}


# ---------------------------------------------------------------------------
# _check_kerberos_ticket
# ---------------------------------------------------------------------------


class TestCheckKerberosTicket:
    def test_no_klist_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(*a, **kw):
            raise FileNotFoundError("klist")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_kerberos_ticket()
        assert result["valid"] is False
        assert result["principal"] is None
        assert result["expires"] is None

    def test_timeout_on_klist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired("klist", 5)

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_kerberos_ticket()
        assert result["valid"] is False

    def test_no_ticket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock = MagicMock()
        mock.returncode = 1
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        result = _check_kerberos_ticket()
        assert result["valid"] is False

    def test_valid_ticket_with_principal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            mock = MagicMock()
            if calls["n"] == 1:
                # klist --test
                mock.returncode = 0
            else:
                # klist (detail)
                mock.returncode = 0
                mock.stdout = (
                    "Credentials cache: FILE:/tmp/krb5cc_1000\n"
                    "        Principal: user@REALM.EDU\n"
                    "  Issued           Expires          Principal\n"
                    "Mar  5 05:17:57 2026  Mar  5 15:17:55 2026  krbtgt/REALM@REALM\n"
                )
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_kerberos_ticket()
        assert result["valid"] is True
        assert result["principal"] == "user@REALM.EDU"
        assert result["expires"] is not None

    def test_valid_ticket_detail_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                mock = MagicMock()
                mock.returncode = 0
                return mock
            raise subprocess.TimeoutExpired("klist", 5)

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_kerberos_ticket()
        assert result["valid"] is True
        assert result["principal"] is None

    def test_mit_kerberos_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def fake_run(cmd, **kw):
            calls["n"] += 1
            mock = MagicMock()
            if calls["n"] == 1:
                mock.returncode = 0
            else:
                mock.returncode = 0
                mock.stdout = "Default principal: user@REALM\n"
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_kerberos_ticket()
        assert result["valid"] is True
        assert result["principal"] == "user@REALM"


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestGetStatus:
    def test_returns_expected_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.state.list_worker_panes",
            lambda *a, **kw: [{"alive": True}, {"alive": False}],
        )
        monkeypatch.setattr(
            "dgov.state._check_tunnel_health",
            lambda: {"ports": {}, "any_up": False},
        )
        monkeypatch.setattr(
            "dgov.state._check_kerberos_ticket",
            lambda: {"valid": False, "principal": None, "expires": None},
        )
        result = get_status("/tmp/repo")
        assert result["total"] == 2
        assert result["alive"] == 1
        assert "tunnel" in result
        assert "kerberos" in result

    def test_empty_panes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.state.list_worker_panes", lambda *a, **kw: [])
        monkeypatch.setattr(
            "dgov.state._check_tunnel_health",
            lambda: {"ports": {}, "any_up": True},
        )
        monkeypatch.setattr(
            "dgov.state._check_kerberos_ticket",
            lambda: {"valid": True, "principal": "u@R", "expires": "tomorrow"},
        )
        result = get_status("/tmp/repo")
        assert result["total"] == 0
        assert result["alive"] == 0

    def test_session_root_forwarded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured = {}

        def fake_list(project_root, session_root=None):
            captured["session_root"] = session_root
            return []

        monkeypatch.setattr("dgov.state.list_worker_panes", fake_list)
        monkeypatch.setattr(
            "dgov.state._check_tunnel_health",
            lambda: {"ports": {}, "any_up": False},
        )
        monkeypatch.setattr(
            "dgov.state._check_kerberos_ticket",
            lambda: {"valid": False, "principal": None, "expires": None},
        )
        get_status("/tmp/repo", session_root="/tmp/session")
        assert captured["session_root"] == "/tmp/session"


class TestGetStatusEdgeCases:
    def test_no_panes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: float
        ) -> subprocess.CompletedProcess:
            if "curl" in cmd[0]:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
            if cmd == ["klist", "--test"]:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        with patch("dgov.state.list_worker_panes", return_value=[]):
            result = get_status(str(tmp_path))

        assert result["total"] == 0
        assert result["alive"] == 0

    def test_mixed_pane_states(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: float
        ) -> subprocess.CompletedProcess:
            if "curl" in cmd[0]:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
            if cmd == ["klist", "--test"]:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        panes = [
            {"name": "w1", "alive": True},
            {"name": "w2", "alive": False},
            {"name": "w3", "alive": True},
        ]
        with patch("dgov.state.list_worker_panes", return_value=panes):
            result = get_status(str(tmp_path))

        assert result["total"] == 3
        assert result["alive"] == 2


class TestKerberosEdgeCases:
    def test_no_ticket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test when klist --test fails (no ticket)."""

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: float
        ) -> subprocess.CompletedProcess:
            if cmd == ["klist", "--test"]:
                return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_kerberos_ticket()
        assert result["valid"] is False
        assert result["principal"] is None

    def test_klist_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test when klist times out."""

        def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: float) -> None:
            raise subprocess.TimeoutExpired(cmd, timeout)

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_kerberos_ticket()
        assert result["valid"] is False

    def test_no_principal_in_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test when klist output has no principal line."""

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: float
        ) -> subprocess.CompletedProcess:
            if cmd == ["klist", "--test"]:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Some random output\n", stderr=""
            )

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_kerberos_ticket()
        assert result["valid"] is True
        assert result["principal"] is None
        assert result["expires"] is None


class TestTunnelEdgeCases:
    def test_timeout_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test when curl times out (subprocess.TimeoutExpired)."""

        def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: float) -> None:
            raise subprocess.TimeoutExpired(cmd, timeout)

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_tunnel_health()
        assert result["any_up"] is False
        assert all(v == "down" for v in result["ports"].values())

    def test_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test when curl is not found (OSError)."""

        def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: float) -> None:
            raise OSError("curl not found")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_tunnel_health()
        assert result["any_up"] is False

    def test_non_200_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test when curl returns non-200 status code."""

        def fake_run(
            cmd: list[str], capture_output: bool, text: bool, timeout: float
        ) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="503", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _check_tunnel_health()
        assert result["any_up"] is False
        assert all(v == "down" for v in result["ports"].values())
