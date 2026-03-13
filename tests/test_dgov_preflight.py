"""Unit tests for dgov preflight validation."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from dgov.preflight import (
    CheckResult,
    PreflightReport,
    _fix_deps,
    _fix_kerberos,
    _fix_stale_worktrees,
    _fix_tunnel,
    check_agent_cli,
    check_deps,
    check_file_locks,
    check_git_branch,
    check_git_clean,
    check_kerberos,
    check_stale_worktrees,
    check_tunnel,
    fix_preflight,
    run_preflight,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# check_agent_cli
# ---------------------------------------------------------------------------


def test_check_agent_cli_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dgov.preflight.shutil.which", lambda _: "/usr/bin/pi")
    r = check_agent_cli("pi")
    assert r.passed is True
    assert r.critical is True
    assert "pi found" in r.message


def test_check_agent_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dgov.preflight.shutil.which", lambda _: None)
    r = check_agent_cli("pi")
    assert r.passed is False
    assert r.critical is True
    assert "not found" in r.message


def test_check_agent_cli_unknown_agent() -> None:
    r = check_agent_cli("nonexistent")
    assert r.passed is False
    assert "Unknown agent" in r.message


# ---------------------------------------------------------------------------
# check_tunnel
# ---------------------------------------------------------------------------


def _mock_curl(monkeypatch, responses: dict[int, str]) -> None:
    """Mock subprocess.run for curl calls. responses maps port -> http_code."""

    def fake_run(cmd, **kwargs):
        for port, code in responses.items():
            if f"http://localhost:{port}/health" in cmd:
                mock = MagicMock()
                mock.stdout = code
                return mock
        mock = MagicMock()
        mock.stdout = "000"
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)


def test_check_tunnel_up(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_curl(monkeypatch, {8080: "200", 8081: "200", 8082: "200"})
    r = check_tunnel()
    assert r.passed is True
    assert r.fixable is True


def test_check_tunnel_down(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_curl(monkeypatch, {8080: "000", 8081: "000", 8082: "000"})
    r = check_tunnel()
    assert r.passed is False
    assert r.critical is True


def test_check_tunnel_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_curl(monkeypatch, {8080: "200", 8081: "000", 8082: "000"})
    r = check_tunnel()
    assert r.passed is True  # at least one port up


# ---------------------------------------------------------------------------
# check_git_clean
# ---------------------------------------------------------------------------


def _mock_git_diff(monkeypatch, unstaged_rc: int = 0, staged_rc: int = 0) -> None:
    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        if "diff" in cmd and "--cached" in cmd:
            mock.returncode = staged_rc
        elif "diff" in cmd and "HEAD" in cmd:
            mock.returncode = unstaged_rc
        else:
            mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)


def test_check_git_clean_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_git_diff(monkeypatch, unstaged_rc=0, staged_rc=0)
    r = check_git_clean("/tmp/repo")
    assert r.passed is True


def test_check_git_clean_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_git_diff(monkeypatch, unstaged_rc=1)
    r = check_git_clean("/tmp/repo")
    assert r.passed is False
    assert r.critical is True
    assert "unstaged" in r.message


def test_check_git_clean_staged(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_git_diff(monkeypatch, unstaged_rc=0, staged_rc=1)
    r = check_git_clean("/tmp/repo")
    assert r.passed is False
    assert "staged" in r.message


# ---------------------------------------------------------------------------
# check_git_branch
# ---------------------------------------------------------------------------


def _mock_git_branch(monkeypatch, branch: str) -> None:
    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.stdout = branch + "\n"
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)


def test_check_git_branch_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_git_branch(monkeypatch, "main")
    r = check_git_branch("/tmp/repo", expected="main")
    assert r.passed is True
    assert "matches expected" in r.message


def test_check_git_branch_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_git_branch(monkeypatch, "feature-x")
    r = check_git_branch("/tmp/repo", expected="main")
    assert r.passed is False
    assert r.critical is False
    assert "feature-x" in r.message


def test_check_git_branch_no_expected(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_git_branch(monkeypatch, "develop")
    r = check_git_branch("/tmp/repo")
    assert r.passed is True


# ---------------------------------------------------------------------------
# check_kerberos
# ---------------------------------------------------------------------------


def test_check_kerberos_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        mock = MagicMock()
        if cmd == ["klist", "--test"]:
            mock.returncode = 0
        else:
            # klist full output with valid ticket far in the future
            mock.stdout = (
                "Credentials cache: FILE:/tmp/krb5cc_501\n"
                "        Principal: jgearon@AD.UNC.EDU\n"
                "  Issued                Expires               Principal\n"
                "Mar  5 05:17:57 2026  Mar  5 15:17:55 2099  krbtgt/AD.UNC.EDU@AD.UNC.EDU\n"
            )
            mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_kerberos()
    assert r.passed is True


def test_check_kerberos_expiring_soon(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timedelta

    soon = datetime.now() + timedelta(minutes=30)
    expiry_line = soon.strftime(
        "Mar  5 05:17:57 2026  %b %d %H:%M:%S %Y  krbtgt/AD.UNC.EDU@AD.UNC.EDU\n"
    )

    call_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        mock = MagicMock()
        if cmd == ["klist", "--test"]:
            mock.returncode = 0
        else:
            mock.stdout = (
                "Credentials cache: FILE:/tmp/krb5cc_501\n"
                "        Principal: jgearon@AD.UNC.EDU\n" + expiry_line
            )
            mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_kerberos(min_remaining_hours=2)
    assert r.passed is False
    assert "expires" in r.message.lower() or "0." in r.message


def test_check_kerberos_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 1
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_kerberos()
    assert r.passed is False
    assert r.fixable is True


def test_check_kerberos_no_klist(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("klist")

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_kerberos()
    assert r.passed is False
    assert "not installed" in r.message


# ---------------------------------------------------------------------------
# check_deps
# ---------------------------------------------------------------------------


def test_check_deps_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        mock.stderr = ""
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_deps()
    assert r.passed is True


def test_check_deps_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        mock.stderr = "Would install foo==1.0"
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_deps()
    assert r.passed is False
    assert r.fixable is True


# ---------------------------------------------------------------------------
# check_stale_worktrees
# ---------------------------------------------------------------------------


def test_check_stale_worktrees_none(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    resolved = str(tmp_path.resolve())

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.stdout = f"worktree {resolved}\n"
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_stale_worktrees(str(tmp_path))
    assert r.passed is True


def test_check_stale_worktrees_found(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    resolved = str(tmp_path.resolve())
    stale_wt = f"{resolved}/.dgov/worktrees/old-task"

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.stdout = f"worktree {resolved}\n\nworktree {stale_wt}\n\n"
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    monkeypatch.setattr("dgov.panes.list_worker_panes", lambda *a, **kw: [])
    r = check_stale_worktrees(str(tmp_path))
    assert r.passed is False
    assert "stale" in r.message.lower()


# ---------------------------------------------------------------------------
# check_file_locks
# ---------------------------------------------------------------------------


def test_check_file_locks_clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(
        "dgov.panes.list_worker_panes",
        lambda *a, **kw: [],
    )
    r = check_file_locks(str(tmp_path), ["src/foo.py"])
    assert r.passed is True


def test_check_file_locks_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    wt_path = tmp_path / "worktrees" / "task-1"
    wt_path.mkdir(parents=True)

    def fake_panes(*a, **kw):
        return [{"slug": "task-1", "worktree_path": str(wt_path)}]

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.stdout = "src/foo.py\n"
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.panes.list_worker_panes", fake_panes)
    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_file_locks(str(tmp_path), ["src/foo.py"])
    assert r.passed is False
    assert "task-1" in r.message


def test_check_file_locks_no_touches() -> None:
    r = check_file_locks("/tmp/repo", [])
    assert r.passed is True


# ---------------------------------------------------------------------------
# run_preflight
# ---------------------------------------------------------------------------


def _patch_all_checks(monkeypatch, results: dict[str, CheckResult]) -> None:
    """Patch individual checkers to return predetermined results."""
    for name, result in results.items():
        monkeypatch.setattr(f"dgov.preflight.{name}", lambda *a, _r=result, **kw: _r)


def test_run_preflight_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_all_checks(
        monkeypatch,
        {
            "check_agent_cli": CheckResult("agent_cli", True, True, "ok"),
            "check_dmux_compat": CheckResult("dmux_compat", True, True, "ok"),
            "check_git_clean": CheckResult("git_clean", True, True, "ok"),
            "check_git_branch": CheckResult("git_branch", True, False, "ok"),
            "check_tunnel": CheckResult("tunnel", True, True, "ok"),
            "check_kerberos": CheckResult("kerberos", True, True, "ok"),
            "check_gpu_concurrency": CheckResult("gpu_concurrency", True, True, "ok"),
            "check_deps": CheckResult("deps", True, False, "ok"),
            "check_stale_worktrees": CheckResult("stale_worktrees", True, False, "ok"),
            "check_file_locks": CheckResult("file_locks", True, True, "ok"),
        },
    )
    report = run_preflight("/tmp/repo", agent="pi")
    assert report.passed is True
    assert len(report.checks) == 10


def test_run_preflight_critical_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_all_checks(
        monkeypatch,
        {
            "check_agent_cli": CheckResult("agent_cli", False, True, "not found"),
            "check_dmux_compat": CheckResult("dmux_compat", True, True, "ok"),
            "check_git_clean": CheckResult("git_clean", True, True, "ok"),
            "check_git_branch": CheckResult("git_branch", True, False, "ok"),
            "check_tunnel": CheckResult("tunnel", True, True, "ok"),
            "check_kerberos": CheckResult("kerberos", True, True, "ok"),
            "check_gpu_concurrency": CheckResult("gpu_concurrency", True, True, "ok"),
            "check_deps": CheckResult("deps", True, False, "ok"),
            "check_stale_worktrees": CheckResult("stale_worktrees", True, False, "ok"),
            "check_file_locks": CheckResult("file_locks", True, True, "ok"),
        },
    )
    report = run_preflight("/tmp/repo", agent="pi")
    assert report.passed is False


def test_run_preflight_skips_tunnel_for_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_all_checks(
        monkeypatch,
        {
            "check_agent_cli": CheckResult("agent_cli", True, True, "ok"),
            "check_dmux_compat": CheckResult("dmux_compat", True, True, "ok"),
            "check_git_clean": CheckResult("git_clean", True, True, "ok"),
            "check_git_branch": CheckResult("git_branch", True, False, "ok"),
            "check_gpu_concurrency": CheckResult("gpu_concurrency", True, False, "ok"),
            "check_deps": CheckResult("deps", True, False, "ok"),
            "check_stale_worktrees": CheckResult("stale_worktrees", True, False, "ok"),
            "check_file_locks": CheckResult("file_locks", True, True, "ok"),
        },
    )
    report = run_preflight("/tmp/repo", agent="claude")
    assert report.passed is True
    names = {c.name for c in report.checks}
    assert "tunnel" not in names
    assert "kerberos" not in names


# ---------------------------------------------------------------------------
# fix_preflight
# ---------------------------------------------------------------------------


def test_fix_preflight_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dgov.preflight._fix_tunnel", lambda: True)
    # After fix, the tunnel re-check passes
    monkeypatch.setattr(
        "dgov.preflight.check_tunnel",
        lambda *a, **kw: CheckResult("tunnel", True, True, "fixed"),
    )

    report = PreflightReport(
        checks=[
            CheckResult("agent_cli", True, True, "ok"),
            CheckResult("tunnel", False, True, "down", fixable=True),
        ]
    )
    fixed = fix_preflight(report, "/tmp/repo")
    tunnel = next(c for c in fixed.checks if c.name == "tunnel")
    assert tunnel.passed is True


def test_fix_preflight_kerberos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dgov.preflight._fix_kerberos", lambda: True)
    monkeypatch.setattr(
        "dgov.preflight.check_kerberos",
        lambda *a, **kw: CheckResult("kerberos", True, True, "renewed"),
    )

    report = PreflightReport(
        checks=[
            CheckResult("kerberos", False, True, "expired", fixable=True),
        ]
    )
    fixed = fix_preflight(report, "/tmp/repo")
    krb = next(c for c in fixed.checks if c.name == "kerberos")
    assert krb.passed is True


def test_fix_preflight_noop_when_not_fixable() -> None:
    report = PreflightReport(
        checks=[
            CheckResult("agent_cli", False, True, "missing"),
        ]
    )
    fixed = fix_preflight(report, "/tmp/repo")
    assert fixed.passed is False
    assert fixed.checks[0].passed is False


# ---------------------------------------------------------------------------
# PreflightReport
# ---------------------------------------------------------------------------


def test_preflight_report_passed_ignores_noncritical() -> None:
    report = PreflightReport(
        checks=[
            CheckResult("git_clean", True, True, "ok"),
            CheckResult("deps", False, False, "out of sync"),
        ]
    )
    assert report.passed is True


def test_preflight_report_to_dict() -> None:
    report = PreflightReport(
        checks=[
            CheckResult("git_clean", True, True, "ok"),
        ]
    )
    d = report.to_dict()
    assert "checks" in d
    assert "timestamp" in d
    assert d["passed"] is True


# ---------------------------------------------------------------------------
# CheckResult dataclass
# ---------------------------------------------------------------------------


class TestCheckResult:
    def test_defaults(self) -> None:
        r = CheckResult("test", True, False, "ok")
        assert r.name == "test"
        assert r.passed is True
        assert r.critical is False
        assert r.message == "ok"
        assert r.fixable is False

    def test_fixable(self) -> None:
        r = CheckResult("test", False, True, "fail", fixable=True)
        assert r.fixable is True


# ---------------------------------------------------------------------------
# check_tunnel edge cases
# ---------------------------------------------------------------------------


class TestCheckTunnelEdgeCases:
    def test_timeout_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 5)

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_tunnel()
        assert r.passed is False

    def test_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            raise OSError("curl not found")

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_tunnel()
        assert r.passed is False

    def test_custom_ports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called_ports: list[int] = []

        def fake_run(cmd, **kwargs):
            for port in (9090, 9091):
                if f"http://localhost:{port}/health" in cmd:
                    called_ports.append(port)
            mock = MagicMock()
            mock.stdout = "200"
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_tunnel(ports=(9090, 9091))
        assert r.passed is True
        assert 9090 in called_ports

    def test_all_ports_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _mock_curl(monkeypatch, {8080: "200", 8081: "200", 8082: "200"})
        r = check_tunnel()
        assert "all ports up" in r.message


# ---------------------------------------------------------------------------
# check_git_clean edge cases
# ---------------------------------------------------------------------------


class TestCheckGitCleanEdgeCases:
    def test_not_git_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 128
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_git_clean("/tmp/not-a-repo")
        assert r.passed is True
        assert "skipped" in r.message.lower()

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 10)

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_git_clean("/tmp/repo")
        assert r.passed is False
        assert "failed" in r.message.lower()

    def test_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            raise OSError("git not found")

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_git_clean("/tmp/repo")
        assert r.passed is False
        assert "git not found" in r.message


# ---------------------------------------------------------------------------
# check_git_branch edge cases
# ---------------------------------------------------------------------------


class TestCheckGitBranchEdgeCases:
    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 10)

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_git_branch("/tmp/repo", expected="main")
        assert r.passed is True  # non-critical, passes on error
        assert r.critical is False
        assert "Could not determine" in r.message

    def test_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            raise OSError("git not found")

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_git_branch("/tmp/repo")
        assert r.passed is True
        assert "Could not determine" in r.message


# ---------------------------------------------------------------------------
# check_kerberos edge cases
# ---------------------------------------------------------------------------


class TestCheckKerberosEdgeCases:
    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 5)

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_kerberos()
        assert r.passed is False
        assert "timed out" in r.message

    def test_unparseable_expiry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """klist --test passes but expiry can't be parsed."""

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            if cmd == ["klist", "--test"]:
                mock.returncode = 0
            else:
                mock.stdout = "Some output\nwithout krbtgt line\n"
                mock.returncode = 0
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_kerberos()
        assert r.passed is True
        assert "could not parse" in r.message.lower()

    def test_detail_klist_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """klist --test passes but detail klist times out."""
        import subprocess as sp

        call_count = [0]

        def fake_run(cmd, **kwargs):
            call_count[0] += 1
            if cmd == ["klist", "--test"]:
                mock = MagicMock()
                mock.returncode = 0
                return mock
            raise sp.TimeoutExpired(cmd, 5)

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_kerberos()
        assert r.passed is True
        assert "could not parse" in r.message.lower()


# ---------------------------------------------------------------------------
# check_deps edge cases
# ---------------------------------------------------------------------------


class TestCheckDepsEdgeCases:
    def test_uv_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("uv")

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_deps()
        assert r.passed is False
        assert "uv not found" in r.message

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 30)

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_deps()
        assert r.passed is False
        assert "timed out" in r.message

    def test_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 1
            mock.stderr = "Resolution failed"
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_deps()
        assert r.passed is False
        assert "Resolution" in r.message

    def test_would_install_in_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = "Would install foo==1.0"
            mock.stderr = ""
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_deps()
        assert r.passed is False


# ---------------------------------------------------------------------------
# check_stale_worktrees edge cases
# ---------------------------------------------------------------------------


class TestCheckStaleWorktreesEdgeCases:
    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 10)

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_stale_worktrees("/tmp/repo")
        assert r.passed is True
        assert "Could not list" in r.message

    def test_all_tracked(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        resolved = str(tmp_path.resolve())
        wt = f"{resolved}/.dgov/worktrees/task-1"

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.stdout = f"worktree {resolved}\n\nworktree {wt}\n\n"
            mock.returncode = 0
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        monkeypatch.setattr(
            "dgov.panes.list_worker_panes",
            lambda *a, **kw: [{"worktree_path": wt}],
        )
        r = check_stale_worktrees(str(tmp_path))
        assert r.passed is True
        assert "all tracked" in r.message


# ---------------------------------------------------------------------------
# check_file_locks edge cases
# ---------------------------------------------------------------------------


class TestCheckFileLocksEdgeCases:
    def test_lock_file_on_disk(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setattr("dgov.panes.list_worker_panes", lambda *a, **kw: [])
        lock = tmp_path / "src" / "foo.py.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is False
        assert "lock file" in r.message

    def test_pane_worktree_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """Pane exists but worktree_path is gone — should not crash."""
        monkeypatch.setattr(
            "dgov.panes.list_worker_panes",
            lambda *a, **kw: [{"slug": "t1", "worktree_path": "/nonexistent/path"}],
        )
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is True

    def test_pane_no_worktree_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """Pane has no worktree_path — should skip gracefully."""
        monkeypatch.setattr(
            "dgov.panes.list_worker_panes",
            lambda *a, **kw: [{"slug": "t1"}],
        )
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is True

    def test_git_diff_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import subprocess as sp

        wt = tmp_path / "wt"
        wt.mkdir()
        monkeypatch.setattr(
            "dgov.panes.list_worker_panes",
            lambda *a, **kw: [{"slug": "t1", "worktree_path": str(wt)}],
        )
        monkeypatch.setattr(
            "dgov.preflight.subprocess.run",
            lambda cmd, **kw: (_ for _ in ()).throw(sp.TimeoutExpired(cmd, 10)),
        )
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is True  # continues on error


# ---------------------------------------------------------------------------
# _fix helpers
# ---------------------------------------------------------------------------


class TestFixHelpers:
    def test_fix_tunnel_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        assert _fix_tunnel() is True

    def test_fix_tunnel_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 1
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        assert _fix_tunnel() is False

    def test_fix_tunnel_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        monkeypatch.setattr(
            "dgov.preflight.subprocess.run",
            lambda cmd, **kw: (_ for _ in ()).throw(sp.TimeoutExpired(cmd, 15)),
        )
        assert _fix_tunnel() is False

    def test_fix_kerberos_no_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RIVER_PW", raising=False)
        assert _fix_kerberos() is False

    def test_fix_kerberos_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RIVER_PW", "secret")

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        assert _fix_kerberos() is True

    def test_fix_kerberos_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RIVER_PW", "secret")

        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 1
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        assert _fix_kerberos() is False

    def test_fix_deps_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        assert _fix_deps() is True

    def test_fix_deps_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        monkeypatch.setattr(
            "dgov.preflight.subprocess.run",
            lambda cmd, **kw: (_ for _ in ()).throw(sp.TimeoutExpired(cmd, 120)),
        )
        assert _fix_deps() is False

    def test_fix_stale_worktrees_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        assert _fix_stale_worktrees("/tmp/repo") is True

    def test_fix_stale_worktrees_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 1
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        assert _fix_stale_worktrees("/tmp/repo") is False


# ---------------------------------------------------------------------------
# fix_preflight edge cases
# ---------------------------------------------------------------------------


class TestFixPreflightEdgeCases:
    def test_fix_deps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.preflight._fix_deps", lambda: True)
        monkeypatch.setattr(
            "dgov.preflight.check_deps",
            lambda *a, **kw: CheckResult("deps", True, False, "synced"),
        )
        report = PreflightReport(
            checks=[CheckResult("deps", False, False, "out of sync", fixable=True)]
        )
        fixed = fix_preflight(report, "/tmp/repo")
        deps = next(c for c in fixed.checks if c.name == "deps")
        assert deps.passed is True

    def test_fix_stale_worktrees(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.preflight._fix_stale_worktrees", lambda root: True)
        monkeypatch.setattr(
            "dgov.preflight.check_stale_worktrees",
            lambda *a, **kw: CheckResult("stale_worktrees", True, False, "clean"),
        )
        report = PreflightReport(
            checks=[CheckResult("stale_worktrees", False, False, "stale", fixable=True)]
        )
        fixed = fix_preflight(report, "/tmp/repo")
        wt = next(c for c in fixed.checks if c.name == "stale_worktrees")
        assert wt.passed is True

    def test_fix_fails_no_recheck(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.preflight._fix_tunnel", lambda: False)
        report = PreflightReport(checks=[CheckResult("tunnel", False, True, "down", fixable=True)])
        fixed = fix_preflight(report, "/tmp/repo")
        # Fix failed so no recheck — original report returned
        assert fixed.checks[0].passed is False
        assert fixed.checks[0].message == "down"

    def test_non_fixable_name_skipped(self) -> None:
        """A fixable check with name not in _FIXER_NAMES is not attempted."""
        report = PreflightReport(
            checks=[CheckResult("agent_cli", False, True, "missing", fixable=True)]
        )
        fixed = fix_preflight(report, "/tmp/repo")
        assert fixed.checks[0].passed is False


# ---------------------------------------------------------------------------
# run_preflight edge cases
# ---------------------------------------------------------------------------


class TestRunPreflightEdgeCases:
    def test_non_tunnel_agent_skips_tunnel_and_kerberos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_all_checks(
            monkeypatch,
            {
                "check_agent_cli": CheckResult("agent_cli", True, True, "ok"),
                "check_dmux_compat": CheckResult("dmux_compat", True, True, "ok"),
                "check_git_clean": CheckResult("git_clean", True, True, "ok"),
                "check_git_branch": CheckResult("git_branch", True, False, "ok"),
                "check_gpu_concurrency": CheckResult("gpu_concurrency", True, False, "ok"),
                "check_deps": CheckResult("deps", True, False, "ok"),
                "check_stale_worktrees": CheckResult("stale_worktrees", True, False, "ok"),
                "check_file_locks": CheckResult("file_locks", True, True, "ok"),
            },
        )
        report = run_preflight("/tmp/repo", agent="codex")
        names = {c.name for c in report.checks}
        assert "tunnel" not in names
        assert "kerberos" not in names

    def test_noncritical_fail_still_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_all_checks(
            monkeypatch,
            {
                "check_agent_cli": CheckResult("agent_cli", True, True, "ok"),
                "check_dmux_compat": CheckResult("dmux_compat", True, True, "ok"),
                "check_git_clean": CheckResult("git_clean", True, True, "ok"),
                "check_git_branch": CheckResult("git_branch", False, False, "wrong branch"),
                "check_tunnel": CheckResult("tunnel", True, True, "ok"),
                "check_kerberos": CheckResult("kerberos", True, True, "ok"),
                "check_gpu_concurrency": CheckResult("gpu_concurrency", True, True, "ok"),
                "check_deps": CheckResult("deps", False, False, "out of sync"),
                "check_stale_worktrees": CheckResult("stale_worktrees", False, False, "stale"),
                "check_file_locks": CheckResult("file_locks", True, True, "ok"),
            },
        )
        report = run_preflight("/tmp/repo", agent="pi")
        assert report.passed is True  # only critical checks matter


# ---------------------------------------------------------------------------
# PreflightReport edge cases
# ---------------------------------------------------------------------------


class TestPreflightReportEdgeCases:
    def test_all_critical_fail(self) -> None:
        report = PreflightReport(
            checks=[
                CheckResult("a", False, True, "fail"),
                CheckResult("b", False, True, "fail"),
            ]
        )
        assert report.passed is False

    def test_empty_checks(self) -> None:
        report = PreflightReport(checks=[])
        assert report.passed is True  # vacuously true

    def test_timestamp_is_iso(self) -> None:
        report = PreflightReport(checks=[])
        # ISO format should contain 'T' separator
        assert "T" in report.timestamp

    def test_to_dict_structure(self) -> None:
        report = PreflightReport(
            checks=[
                CheckResult("a", True, True, "ok", fixable=True),
            ]
        )
        d = report.to_dict()
        assert len(d["checks"]) == 1
        check = d["checks"][0]
        assert check["name"] == "a"
        assert check["passed"] is True
        assert check["fixable"] is True
