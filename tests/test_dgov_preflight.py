"""Unit tests for dgov preflight validation."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from dgov.agents import AgentDef
from dgov.preflight import (
    CheckResult,
    PreflightReport,
    _fix_agent_health,
    _fix_deps,
    _fix_river_tunnel,
    _fix_stale_worktrees,
    check_agent_cli,
    check_agent_concurrency,
    check_agent_health,
    check_deps,
    check_file_locks,
    check_git_branch,
    check_git_clean,
    check_river_tunnel,
    check_stale_worktrees,
    fix_preflight,
    run_preflight,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# check_agent_cli
# ---------------------------------------------------------------------------


def test_check_agent_cli_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dgov.preflight.shutil.which", lambda _: "/usr/bin/claude")
    r = check_agent_cli("claude")
    assert r.passed is True
    assert r.critical is True
    assert "claude found" in r.message


def test_check_agent_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dgov.preflight.shutil.which", lambda _: None)
    r = check_agent_cli("claude")
    assert r.passed is False
    assert r.critical is True
    assert "not found" in r.message


def test_check_agent_cli_unknown_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dgov.router.is_routable", lambda _, **kw: False)
    r = check_agent_cli("nonexistent")
    assert r.passed is False
    assert "not in registry" in r.message


def test_check_agent_cli_logical_routable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logical routing names (qwen-35b, worker, etc.) pass preflight."""
    monkeypatch.setattr("dgov.router.is_routable", lambda name, **kw: name == "qwen-35b")
    r = check_agent_cli("qwen-35b")
    assert r.passed is True
    assert "routable" in r.message


def test_check_agent_cli_with_custom_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dgov.preflight.shutil.which", lambda _: "/usr/bin/pi")
    custom_reg = {
        "pi": AgentDef(
            id="pi",
            name="pi",
            short_label="pi",
            prompt_command="pi",
            prompt_transport="positional",
        )
    }
    r = check_agent_cli("pi", registry=custom_reg)
    assert r.passed is True
    assert "pi found" in r.message


# ---------------------------------------------------------------------------
# check_git_clean
# ---------------------------------------------------------------------------


def _mock_git_diff(monkeypatch, *, status_output: str = "", returncode: int = 0) -> None:
    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = returncode
        mock.stdout = status_output
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)


def test_check_git_clean_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_git_diff(monkeypatch)
    r = check_git_clean("/tmp/repo")
    assert r.passed is True


def test_check_git_clean_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_git_diff(monkeypatch, status_output=" M src/app.py\n")
    r = check_git_clean("/tmp/repo")
    assert r.passed is False
    assert r.critical is True
    assert "tracked changes" in r.message


def test_check_git_clean_staged(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_git_diff(monkeypatch, status_output="M  src/app.py\n")
    r = check_git_clean("/tmp/repo")
    assert r.passed is False
    assert "tracked changes" in r.message


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
    r = check_deps("/tmp/project")
    assert r.passed is True


def test_check_deps_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 1
        mock.stdout = ""
        mock.stderr = "Resolved 5 packages; would install foo==1.0"
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_deps("/tmp/project")
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
    monkeypatch.setattr("dgov.status.list_worker_panes", lambda *a, **kw: [])
    r = check_stale_worktrees(str(tmp_path))
    assert r.passed is False
    assert "stale" in r.message.lower()


# ---------------------------------------------------------------------------
# check_file_locks
# ---------------------------------------------------------------------------


def test_check_file_locks_clean(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr("dgov.status.list_worker_panes", lambda *a, **kw: [])
    r = check_file_locks(str(tmp_path), ["src/foo.py"])
    assert r.passed is True


def test_check_file_locks_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    wt_path = tmp_path / "worktrees" / "task-1"
    wt_path.mkdir(parents=True)

    def fake_panes(*a, **kw):
        return [{"slug": "task-1", "worktree_path": str(wt_path), "base_sha": "abc123"}]

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        if cmd[:3] == ["git", "diff", "--name-only"]:
            mock.stdout = "src/foo.py\n"
        else:
            mock.stdout = ""
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.persistence.all_panes", fake_panes)
    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_file_locks(str(tmp_path), ["src/foo.py"])
    assert r.passed is False
    assert "task-1" in r.message


def test_check_file_locks_conflict_for_touch_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    wt_path = tmp_path / "worktrees" / "task-1"
    wt_path.mkdir(parents=True)

    def fake_panes(*a, **kw):
        return [{"slug": "task-1", "worktree_path": str(wt_path), "base_sha": "abc123"}]

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        if cmd[:3] == ["git", "diff", "--name-only"]:
            mock.stdout = "src/foo.py\n"
        else:
            mock.stdout = ""
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.persistence.all_panes", fake_panes)
    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_file_locks(str(tmp_path), ["src"])
    assert r.passed is False
    assert "src/foo.py" in r.message


def test_check_file_locks_detects_committed_worker_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    wt_path = tmp_path / "worktrees" / "task-1"
    wt_path.mkdir(parents=True)

    def fake_panes(*a, **kw):
        return [{"slug": "task-1", "worktree_path": str(wt_path), "base_sha": "abc123"}]

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        if cmd[:3] == ["git", "diff", "--name-only"]:
            mock.stdout = "src/foo.py\n"
        elif cmd[:3] == ["git", "status", "--porcelain"]:
            mock.stdout = ""
        else:
            mock.stdout = ""
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.persistence.all_panes", fake_panes)
    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_file_locks(str(tmp_path), ["src/foo.py"])
    assert r.passed is False
    assert "task-1" in r.message


def test_check_file_locks_no_touches() -> None:
    r = check_file_locks("/tmp/repo", [])
    assert r.passed is True


def test_check_file_locks_derived_only_downgrades_conflict_to_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When derived_only=True, conflicts become non-critical warnings (bug fix)."""
    wt_path = tmp_path / "worktrees" / "task-1"
    wt_path.mkdir(parents=True)

    def fake_panes(*a, **kw):
        return [{"slug": "task-1", "worktree_path": str(wt_path), "base_sha": "abc123"}]

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        if cmd[:3] == ["git", "diff", "--name-only"]:
            mock.stdout = "src/foo.py\n"
        else:
            mock.stdout = ""
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.persistence.all_panes", fake_panes)
    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_file_locks(str(tmp_path), ["src/foo.py"], derived_only=True)
    # When derived_only=True, conflicts should be warnings (passed=True, critical=False)
    assert r.passed is True
    assert r.critical is False
    assert "derived touches, warning only" in r.message


@pytest.mark.unit
def test_check_file_locks_explicit_claims_still_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When derived_only=False (explicit claims), conflicts MUST block."""
    wt_path = tmp_path / "worktrees" / "task-1"
    wt_path.mkdir(parents=True)

    def fake_panes(*a, **kw):
        return [{"slug": "task-1", "worktree_path": str(wt_path), "base_sha": "abc123"}]

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        if cmd[:3] == ["git", "diff", "--name-only"]:
            mock.stdout = "src/foo.py\n"
        else:
            mock.stdout = ""
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.persistence.all_panes", fake_panes)
    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_file_locks(str(tmp_path), ["src/foo.py"], derived_only=False)
    # Explicit claims should block
    assert r.passed is False
    assert r.critical is True


def test_check_git_clean_allows_disjoint_touches(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "src").mkdir()
    (repo / "src" / "dirty.py").write_text("x = 1\n")
    (repo / "src" / "clean.py").write_text("y = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "src" / "dirty.py").write_text("x = 2\n")

    report = check_git_clean(str(repo), touches=["src/clean.py"])

    assert report.passed is True
    assert "outside declared touches" in report.message


def test_check_git_clean_blocks_overlapping_touches(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "src").mkdir()
    (repo / "src" / "dirty.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "src" / "dirty.py").write_text("x = 2\n")

    report = check_git_clean(str(repo), touches=["src"])

    assert report.passed is False
    assert "src/dirty.py" in report.message


# ---------------------------------------------------------------------------
# check_agent_concurrency
# ---------------------------------------------------------------------------


def test_check_agent_concurrency_skips_no_limit() -> None:
    registry = {
        "claude": AgentDef(
            id="claude",
            name="Claude",
            short_label="cc",
            prompt_command="claude",
            prompt_transport="positional",
        )
    }
    r = check_agent_concurrency("/tmp/repo", "claude", registry=registry)
    assert r.passed is True
    assert "No concurrency limit" in r.message


def test_check_agent_concurrency_blocks_when_limit_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = {
        "pi": AgentDef(
            id="pi",
            name="pi",
            short_label="pi",
            prompt_command="pi",
            prompt_transport="positional",
            max_concurrent=2,
        )
    }
    monkeypatch.setattr("dgov.status._count_active_agent_workers", lambda sr, agent: 2)
    r = check_agent_concurrency("/tmp/repo", "pi", session_root="/tmp/session", registry=registry)
    assert r.passed is False
    assert "max 2" in r.message


def test_check_agent_concurrency_passes_under_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = {
        "pi": AgentDef(
            id="pi",
            name="pi",
            short_label="pi",
            prompt_command="pi",
            prompt_transport="positional",
            max_concurrent=2,
        )
    }
    monkeypatch.setattr("dgov.status._count_active_agent_workers", lambda sr, agent: 1)
    r = check_agent_concurrency("/tmp/repo", "pi", session_root="/tmp/session", registry=registry)
    assert r.passed is True


# ---------------------------------------------------------------------------
# check_agent_health
# ---------------------------------------------------------------------------


def test_check_agent_health_no_healthcheck() -> None:
    registry = {
        "claude": AgentDef(
            id="claude",
            name="Claude",
            short_label="cc",
            prompt_command="claude",
            prompt_transport="positional",
        )
    }
    r = check_agent_health("claude", registry=registry)
    assert r.passed is True
    assert "No health check" in r.message


def test_check_agent_health_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = {
        "pi": AgentDef(
            id="pi",
            name="pi",
            short_label="pi",
            prompt_command="pi",
            prompt_transport="positional",
            health_check="curl -sf http://localhost:8080/health",
        )
    }

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_agent_health("pi", registry=registry)
    assert r.passed is True


def test_check_agent_health_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = {
        "pi": AgentDef(
            id="pi",
            name="pi",
            short_label="pi",
            prompt_command="pi",
            prompt_transport="positional",
            health_check="curl -sf http://localhost:8080/health",
            health_fix="ssh -fN river-tunnel",
        )
    }

    def fake_run(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 1
        return mock

    monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
    r = check_agent_health("pi", registry=registry)
    assert r.passed is False
    assert r.fixable is True


# ---------------------------------------------------------------------------
# run_preflight
# ---------------------------------------------------------------------------


def _patch_all_checks(monkeypatch, results: dict[str, CheckResult]) -> None:
    for name, result in results.items():
        monkeypatch.setattr(f"dgov.preflight.{name}", lambda *a, _r=result, **kw: _r)


def test_run_preflight_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_all_checks(
        monkeypatch,
        {
            "check_agent_cli": CheckResult("agent_cli", True, True, "ok"),
            "check_git_clean": CheckResult("git_clean", True, True, "ok"),
            "check_git_branch": CheckResult("git_branch", True, False, "ok"),
            "check_agent_concurrency": CheckResult("agent_concurrency", True, True, "ok"),
            "check_deps": CheckResult("deps", True, False, "ok"),
            "check_stale_worktrees": CheckResult("stale_worktrees", True, False, "ok"),
            "check_file_locks": CheckResult("file_locks", True, True, "ok"),
        },
    )
    # Mock load_registry to return no health_check agents
    monkeypatch.setattr(
        "dgov.agents.load_registry",
        lambda pr: {
            "pi": AgentDef(
                id="pi",
                name="pi CLI",
                short_label="pi",
                prompt_command="pi",
                prompt_transport="positional",
            )
        },
    )
    report = run_preflight("/tmp/repo", agent="pi")
    assert report.passed is True


def test_run_preflight_critical_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_all_checks(
        monkeypatch,
        {
            "check_agent_cli": CheckResult("agent_cli", False, True, "not found"),
            "check_git_clean": CheckResult("git_clean", True, True, "ok"),
            "check_git_branch": CheckResult("git_branch", True, False, "ok"),
            "check_agent_concurrency": CheckResult("agent_concurrency", True, True, "ok"),
            "check_deps": CheckResult("deps", True, False, "ok"),
            "check_stale_worktrees": CheckResult("stale_worktrees", True, False, "ok"),
            "check_file_locks": CheckResult("file_locks", True, True, "ok"),
        },
    )
    monkeypatch.setattr(
        "dgov.agents.load_registry",
        lambda pr: {
            "pi": AgentDef(
                id="pi",
                name="pi CLI",
                short_label="pi",
                prompt_command="pi",
                prompt_transport="positional",
            )
        },
    )
    report = run_preflight("/tmp/repo", agent="pi")
    assert report.passed is False


def test_run_preflight_includes_health_check_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    _patch_all_checks(
        monkeypatch,
        {
            "check_agent_cli": CheckResult("agent_cli", True, True, "ok"),
            "check_git_clean": CheckResult("git_clean", True, True, "ok"),
            "check_git_branch": CheckResult("git_branch", True, False, "ok"),
            "check_agent_health": CheckResult("agent_health", True, True, "ok"),
            "check_agent_concurrency": CheckResult("agent_concurrency", True, True, "ok"),
            "check_deps": CheckResult("deps", True, False, "ok"),
            "check_stale_worktrees": CheckResult("stale_worktrees", True, False, "ok"),
            "check_file_locks": CheckResult("file_locks", True, True, "ok"),
        },
    )
    monkeypatch.setattr(
        "dgov.agents.load_registry",
        lambda pr: {
            "pi": AgentDef(
                id="pi",
                name="pi",
                short_label="pi",
                prompt_command="pi",
                prompt_transport="positional",
                health_check="curl -sf http://localhost:8080/health",
            )
        },
    )
    report = run_preflight("/tmp/repo", agent="pi")
    names = {c.name for c in report.checks}
    assert "agent_health" in names


def test_run_preflight_skips_health_check_for_no_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_all_checks(
        monkeypatch,
        {
            "check_agent_cli": CheckResult("agent_cli", True, True, "ok"),
            "check_git_clean": CheckResult("git_clean", True, True, "ok"),
            "check_git_branch": CheckResult("git_branch", True, False, "ok"),
            "check_agent_concurrency": CheckResult("agent_concurrency", True, False, "ok"),
            "check_deps": CheckResult("deps", True, False, "ok"),
            "check_stale_worktrees": CheckResult("stale_worktrees", True, False, "ok"),
            "check_file_locks": CheckResult("file_locks", True, True, "ok"),
        },
    )
    monkeypatch.setattr(
        "dgov.agents.load_registry",
        lambda pr: {
            "pi": AgentDef(
                id="pi",
                name="pi CLI",
                short_label="pi",
                prompt_command="pi",
                prompt_transport="positional",
            )
        },
    )
    report = run_preflight("/tmp/repo", agent="pi")
    names = {c.name for c in report.checks}
    assert "agent_health" not in names


# ---------------------------------------------------------------------------
# fix_preflight
# ---------------------------------------------------------------------------


def test_fix_preflight_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "dgov.preflight.FIXER_REGISTRY",
        {
            "stale_worktrees": (
                lambda pr: True,
                check_stale_worktrees,
            ),
            "deps": (lambda pr: True, lambda *a, **kw: CheckResult("deps", True, False, "synced")),
            "agent_health": (
                lambda pr, agent_id=None: True,
                lambda agent, *, registry=None, project_root=None: CheckResult(
                    "agent_health", True, True, "ok"
                ),
            ),
            "river_tunnel": (lambda pr: True, check_river_tunnel),
        },
    )
    report = PreflightReport(
        checks=[CheckResult("deps", False, False, "out of sync", fixable=True)]
    )
    fixed = fix_preflight(report, "/tmp/repo")
    deps = next(c for c in fixed.checks if c.name == "deps")
    assert deps.passed is True


def test_fix_preflight_noop_when_not_fixable() -> None:
    report = PreflightReport(checks=[CheckResult("agent_cli", False, True, "missing")])
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
    report = PreflightReport(checks=[CheckResult("git_clean", True, True, "ok")])
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
        assert r.passed is False
        assert r.critical is False
        assert "Could not determine" in r.message

    def test_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            raise OSError("git not found")

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_git_branch("/tmp/repo")
        assert r.passed is False
        assert "Could not determine" in r.message


# ---------------------------------------------------------------------------
# check_deps edge cases
# ---------------------------------------------------------------------------


class TestCheckDepsEdgeCases:
    def test_uv_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("uv")

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_deps("/tmp/project")
        assert r.passed is False
        assert "uv not found" in r.message

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        def fake_run(cmd, **kwargs):
            raise sp.TimeoutExpired(cmd, 30)

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_deps("/tmp/project")
        assert r.passed is False
        assert "timed out" in r.message

    def test_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 1
            mock.stderr = "Resolution failed"
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_deps("/tmp/project")
        assert r.passed is False
        assert "Resolution" in r.message

    def test_locked_out_of_sync(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 1
            mock.stdout = ""
            mock.stderr = "Lockfile is not up to date"
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        r = check_deps("/tmp/project")
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
            "dgov.status.list_worker_panes",
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
        monkeypatch.setattr("dgov.status.list_worker_panes", lambda *a, **kw: [])
        lock = tmp_path / "src" / "foo.py.lock"
        lock.parent.mkdir(parents=True)
        lock.touch()
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is False
        assert "lock file" in r.message

    def test_pane_worktree_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setattr(
            "dgov.status.list_worker_panes",
            lambda *a, **kw: [{"slug": "t1", "worktree_path": "/nonexistent/path"}],
        )
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is True

    def test_pane_no_worktree_key(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setattr(
            "dgov.status.list_worker_panes",
            lambda *a, **kw: [{"slug": "t1"}],
        )
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is True

    @pytest.mark.parametrize(
        "state",
        [
            "done",
            "failed",
            "merged",
            "closed",
            "abandoned",
            "superseded",
            "timed_out",
            "escalated",
        ],
    )
    def test_terminal_state_panes_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path, state: str
    ) -> None:
        """Panes in terminal states should not block new dispatches (ledger #72)."""
        monkeypatch.setattr(
            "dgov.persistence.all_panes",
            lambda *a, **kw: [
                {
                    "slug": "old-task",
                    "state": state,
                    "file_claims": '["src/foo.py"]',
                    "worktree_path": str(tmp_path / "wt"),
                    "base_sha": "abc123",
                }
            ],
        )
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is True

    def test_active_state_pane_blocks(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """Panes in active states should still block on overlapping claims."""
        monkeypatch.setattr(
            "dgov.persistence.all_panes",
            lambda *a, **kw: [
                {
                    "slug": "active-task",
                    "state": "working",
                    "file_claims": '["src/foo.py"]',
                    "worktree_path": str(tmp_path / "wt"),
                    "base_sha": "abc123",
                }
            ],
        )
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is False
        assert "active-task" in r.message

    def test_git_diff_timeout(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        import subprocess as sp

        wt = tmp_path / "wt"
        wt.mkdir()
        monkeypatch.setattr(
            "dgov.status.list_worker_panes",
            lambda *a, **kw: [{"slug": "t1", "worktree_path": str(wt)}],
        )
        monkeypatch.setattr(
            "dgov.preflight.subprocess.run",
            lambda cmd, **kw: (_ for _ in ()).throw(sp.TimeoutExpired(cmd, 10)),
        )
        r = check_file_locks(str(tmp_path), ["src/foo.py"])
        assert r.passed is True


# ---------------------------------------------------------------------------
# _fix helpers
# ---------------------------------------------------------------------------


class TestFixHelpers:
    def test_fix_deps_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            mock = MagicMock()
            mock.returncode = 0
            return mock

        monkeypatch.setattr("dgov.preflight.subprocess.run", fake_run)
        assert _fix_deps("/tmp/repo") is True

    def test_fix_deps_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess as sp

        monkeypatch.setattr(
            "dgov.preflight.subprocess.run",
            lambda cmd, **kw: (_ for _ in ()).throw(sp.TimeoutExpired(cmd, 120)),
        )
        assert _fix_deps("/tmp/repo") is False

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
        monkeypatch.setattr(
            "dgov.preflight.FIXER_REGISTRY",
            {
                "stale_worktrees": (
                    lambda pr: True,
                    check_stale_worktrees,
                ),
                "deps": (
                    lambda pr: True,
                    lambda *a, **kw: CheckResult("deps", True, False, "synced"),
                ),
                "agent_health": (
                    lambda pr, agent_id=None: True,
                    lambda agent, *, registry=None, project_root=None: CheckResult(
                        "agent_health", True, True, "ok"
                    ),
                ),
                "river_tunnel": (lambda pr: True, check_river_tunnel),
            },
        )
        report = PreflightReport(
            checks=[CheckResult("deps", False, False, "out of sync", fixable=True)]
        )
        fixed = fix_preflight(report, "/tmp/repo")
        deps = next(c for c in fixed.checks if c.name == "deps")
        assert deps.passed is True

    def test_fix_stale_worktrees(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.preflight.FIXER_REGISTRY",
            {
                "stale_worktrees": (
                    lambda root: True,
                    lambda *a, **kw: CheckResult("stale_worktrees", True, False, "clean"),
                ),
                "deps": (_fix_deps, check_deps),
                "agent_health": (lambda pr, agent_id=None: True, check_agent_health),
                "river_tunnel": (_fix_river_tunnel, check_river_tunnel),
            },
        )
        report = PreflightReport(
            checks=[CheckResult("stale_worktrees", False, False, "stale", fixable=True)]
        )
        fixed = fix_preflight(report, "/tmp/repo")
        wt = next(c for c in fixed.checks if c.name == "stale_worktrees")
        assert wt.passed is True

    def test_fix_fails_no_recheck(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.preflight.FIXER_REGISTRY",
            {
                "stale_worktrees": (_fix_stale_worktrees, check_stale_worktrees),
                "deps": (lambda pr: False, check_deps),
                "agent_health": (_fix_agent_health, check_agent_health),
                "river_tunnel": (_fix_river_tunnel, check_river_tunnel),
            },
        )
        report = PreflightReport(
            checks=[CheckResult("deps", False, False, "out of sync", fixable=True)]
        )
        fixed = fix_preflight(report, "/tmp/repo")
        assert fixed.checks[0].passed is False
        assert fixed.checks[0].message == "out of sync"

    def test_non_fixable_name_skipped(self) -> None:
        report = PreflightReport(
            checks=[CheckResult("agent_cli", False, True, "missing", fixable=True)]
        )
        fixed = fix_preflight(report, "/tmp/repo")
        assert fixed.checks[0].passed is False


# ---------------------------------------------------------------------------
# run_preflight edge cases
# ---------------------------------------------------------------------------


class TestRunPreflightEdgeCases:
    def test_noncritical_fail_still_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_all_checks(
            monkeypatch,
            {
                "check_agent_cli": CheckResult("agent_cli", True, True, "ok"),
                "check_git_clean": CheckResult("git_clean", True, True, "ok"),
                "check_git_branch": CheckResult("git_branch", False, False, "wrong branch"),
                "check_agent_concurrency": CheckResult("agent_concurrency", True, True, "ok"),
                "check_deps": CheckResult("deps", False, False, "out of sync"),
                "check_stale_worktrees": CheckResult("stale_worktrees", False, False, "stale"),
                "check_file_locks": CheckResult("file_locks", True, True, "ok"),
            },
        )
        monkeypatch.setattr(
            "dgov.agents.load_registry",
            lambda pr: {
                "claude": AgentDef(
                    id="claude",
                    name="Claude",
                    short_label="cc",
                    prompt_command="claude",
                    prompt_transport="positional",
                )
            },
        )
        report = run_preflight("/tmp/repo", agent="claude")
        assert report.passed is True


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
        assert report.passed is True

    def test_timestamp_is_iso(self) -> None:
        report = PreflightReport(checks=[])
        assert "T" in report.timestamp

    def test_to_dict_structure(self) -> None:
        report = PreflightReport(checks=[CheckResult("a", True, True, "ok", fixable=True)])
        d = report.to_dict()
        assert len(d["checks"]) == 1
        check = d["checks"][0]
        assert check["name"] == "a"
        assert check["passed"] is True
        assert check["fixable"] is True
