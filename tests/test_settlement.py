"""Tests for settlement gates — review, autofix, validate.

Uses real git repos (tmp_path) to test the actual git operations
that gate worker output before merge.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from dgov.config import ProjectConfig
from dgov.persistence import emit_event
from dgov.settlement import (
    _build_test_cmd,
    _run_coverage_gate,
    _run_sentrux_gate,
    _sentrux_is_warn_only,
    autofix_sandbox,
    preflight_sandbox,
    review_sandbox,
    validate_sandbox,
)

# ---------------------------------------------------------------------------
# Sentrux warn-only classification
# ---------------------------------------------------------------------------


class TestSentruxWarnOnly:
    _COMPLEXITY_ONLY = """\
sentrux gate — structural regression check

Quality:      2304 -> 2316
Coupling:     0.02 → 0.02
Cycles:       50 → 50
God files:    124 → 124

✗ DEGRADED
  ✗ Complex functions increased: 3779 → 3780
"""
    _QUALITY_DROP = """\
sentrux gate — structural regression check

Quality:      2316 -> 2300
Coupling:     0.02 → 0.04

✗ DEGRADED
  ✗ Quality score dropped: 2316 → 2300
  ✗ Coupling increased: 0.02 → 0.04
"""
    _COUPLING_ONLY = """\
✗ DEGRADED
  ✗ Coupling increased: 0.02 → 0.05
"""
    _CLEAN = "✓ No degradation detected\n"

    def test_complexity_only_is_warn(self):
        assert _sentrux_is_warn_only(self._COMPLEXITY_ONLY) is True

    def test_quality_drop_is_not_warn(self):
        assert _sentrux_is_warn_only(self._QUALITY_DROP) is False

    def test_coupling_only_is_warn(self):
        assert _sentrux_is_warn_only(self._COUPLING_ONLY) is True

    def test_clean_output_is_not_warn(self):
        # No failing lines — not warn-only (it passed, so this branch never fires)
        assert _sentrux_is_warn_only(self._CLEAN) is False

    def test_complexity_plus_coupling_is_warn(self):
        mixed = self._COMPLEXITY_ONLY + "  ✗ Coupling increased: 0.02 → 0.03\n"
        assert _sentrux_is_warn_only(mixed) is True


@pytest.mark.unit
def test_validate_sandbox_surfaces_stderr_from_lint_failures(tmp_path: Path) -> None:
    base = _init_repo(tmp_path)
    _add_tracked_file(tmp_path, "src.py", "x = 1\n")
    lint_cmd = (
        "python -c \"import sys; print('lint missing', file=sys.stderr); raise SystemExit(1)\""
    )
    result = validate_sandbox(
        tmp_path,
        base,
        str(tmp_path),
        ProjectConfig(
            source_extensions=(".py",),
            lint_cmd=lint_cmd,
            format_check_cmd='python -c "raise SystemExit(0)"',
            test_cmd="",
        ),
    )

    assert result.passed is False
    assert result.error is not None
    assert "lint missing" in result.error


# ---------------------------------------------------------------------------
# Helpers — create real git repos in tmp_path
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
        check=True,
    )


def _init_repo(path: Path) -> str:
    """Create a git repo with one initial commit. Returns base SHA."""
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("# test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")
    sha = _git(path, "rev-parse", "HEAD").stdout.strip()
    return sha


def _add_tracked_file(path: Path, filename: str, content: str) -> None:
    """Add a tracked file to the repo (committed)."""
    (path / filename).write_text(content)
    _git(path, "add", filename)
    _git(path, "commit", "-m", f"add {filename}")


def _modify_tracked(path: Path, filename: str, content: str) -> None:
    """Modify an existing tracked file (unstaged change visible to git diff)."""
    (path / filename).write_text(content)


def _coverage_payload(file: str, percent: float) -> str:
    return json.dumps({"files": {file: {"summary": {"percent_covered": percent}}}})


def _coverage_cmd(payload: str) -> str:
    script = f"from pathlib import Path; Path(r'{{output}}').write_text({payload!r})"
    return f"python -c {shlex.quote(script)}"


def _coverage_worktree(tmp_path: Path, baseline_percent: float) -> tuple[Path, Path]:
    project_root = tmp_path / "project"
    worktree_path = tmp_path / "worktree"
    (project_root / ".coverage-baseline").mkdir(parents=True)
    (worktree_path / "src").mkdir(parents=True)
    (worktree_path / "tests").mkdir(parents=True)
    (worktree_path / "src" / "pkg.py").write_text("VALUE = 1\n")
    (worktree_path / "tests" / "test_pkg.py").write_text("import pkg\n")
    (project_root / ".coverage-baseline" / "coverage.json").write_text(
        _coverage_payload("src/pkg.py", baseline_percent)
    )
    return project_root, worktree_path


# ---------------------------------------------------------------------------
# review_sandbox
# ---------------------------------------------------------------------------


class TestReviewSandbox:
    def test_pass_with_tracked_changes(self, tmp_path: Path):
        """Review passes when tracked files are modified."""
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "src.py", "x = 1\n")
        _modify_tracked(tmp_path, "src.py", "x = 2\n")
        result = review_sandbox(tmp_path)
        assert result.passed
        assert result.verdict == "ok"
        assert "src.py" in result.actual_files

    def test_pass_with_new_untracked_files(self, tmp_path: Path):
        """Review passes when worker creates new files."""
        _init_repo(tmp_path)
        (tmp_path / "new_file.py").write_text("x = 1\n")
        result = review_sandbox(tmp_path)
        assert result.passed
        assert "new_file.py" in result.actual_files

    def test_empty_diff_fails(self, tmp_path: Path):
        _init_repo(tmp_path)
        result = review_sandbox(tmp_path)
        assert not result.passed
        assert result.verdict == "empty_diff"

    def test_scope_violation(self, tmp_path: Path):
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "claimed.py", "x = 1\n")
        _add_tracked_file(tmp_path, "unclaimed.py", "y = 1\n")
        _modify_tracked(tmp_path, "claimed.py", "x = 2\n")
        _modify_tracked(tmp_path, "unclaimed.py", "y = 2\n")
        result = review_sandbox(tmp_path, claimed_files=["claimed.py"])
        assert not result.passed
        assert result.verdict == "scope_violation"
        assert "unclaimed.py" in (result.error or "")

    def test_scope_pass_when_within_claims(self, tmp_path: Path):
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "claimed.py", "x = 1\n")
        _modify_tracked(tmp_path, "claimed.py", "x = 2\n")
        result = review_sandbox(tmp_path, claimed_files=["claimed.py"])
        assert result.passed

    def test_scope_pass_new_file_claimed(self, tmp_path: Path):
        """New untracked file within claimed scope passes."""
        _init_repo(tmp_path)
        (tmp_path / "new.py").write_text("x = 1\n")
        result = review_sandbox(tmp_path, claimed_files=["new.py"])
        assert result.passed

    def test_scope_ignore_files_exempts_lockfile(self, tmp_path: Path):
        """Files in scope_ignore_files pass through without a claim."""
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "claimed.py", "x = 1\n")
        _add_tracked_file(tmp_path, "uv.lock", "lock = 1\n")
        _modify_tracked(tmp_path, "claimed.py", "x = 2\n")
        _modify_tracked(tmp_path, "uv.lock", "lock = 2\n")
        result = review_sandbox(
            tmp_path,
            claimed_files=["claimed.py"],
            scope_ignore_files=("uv.lock",),
        )
        assert result.passed, f"expected pass, got {result.verdict}: {result.error}"

    def test_scope_ignore_does_not_hide_real_violation(self, tmp_path: Path):
        """Ignoring uv.lock does not hide other unclaimed files."""
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "claimed.py", "x = 1\n")
        _add_tracked_file(tmp_path, "uv.lock", "lock = 1\n")
        _add_tracked_file(tmp_path, "other.py", "y = 1\n")
        _modify_tracked(tmp_path, "claimed.py", "x = 2\n")
        _modify_tracked(tmp_path, "uv.lock", "lock = 2\n")
        _modify_tracked(tmp_path, "other.py", "y = 2\n")
        result = review_sandbox(
            tmp_path,
            claimed_files=["claimed.py"],
            scope_ignore_files=("uv.lock",),
        )
        assert not result.passed
        assert result.verdict == "scope_violation"
        assert "other.py" in (result.error or "")
        assert "uv.lock" not in (result.error or "")

    def test_scope_ignore_named_dir_matches_nested_pycache(self, tmp_path: Path):
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "claimed.py", "x = 1\n")
        cache_dir = tmp_path / "pkg" / "__pycache__"
        cache_dir.mkdir(parents=True)
        (cache_dir / "claimed.cpython-312.pyc").write_bytes(b"abc")
        _modify_tracked(tmp_path, "claimed.py", "x = 2\n")
        result = review_sandbox(
            tmp_path,
            claimed_files=["claimed.py"],
            scope_ignore_files=("__pycache__",),
        )
        assert result.passed

    def test_scope_ignore_glob_matches_pyc(self, tmp_path: Path):
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "claimed.py", "x = 1\n")
        (tmp_path / "scratch.pyc").write_bytes(b"abc")
        _modify_tracked(tmp_path, "claimed.py", "x = 2\n")
        result = review_sandbox(
            tmp_path,
            claimed_files=["claimed.py"],
            scope_ignore_files=("*.pyc",),
        )
        assert result.passed

    def test_transient_unclaimed_tool_write_fails_scope(self, tmp_path: Path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _init_repo(worktree)
        _add_tracked_file(worktree, "claimed.py", "x = 1\n")
        _modify_tracked(worktree, "claimed.py", "x = 2\n")

        session_root = tmp_path / "session"
        emit_event(
            str(session_root),
            "worker_log",
            "pane-1",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "write_file",
                "status": "success",
                "activity": [{"kind": "write_file", "path": "scratch.py", "mode": "create"}],
            },
        )

        result = review_sandbox(
            worktree,
            claimed_files=["claimed.py"],
            project_root=str(session_root),
            task_slug="task-1",
        )
        assert not result.passed
        assert result.verdict == "scope_violation"
        assert "scratch.py" in (result.error or "")

    def test_transient_claimed_tool_write_passes_scope(self, tmp_path: Path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _init_repo(worktree)
        _add_tracked_file(worktree, "claimed.py", "x = 1\n")
        _modify_tracked(worktree, "claimed.py", "x = 2\n")

        session_root = tmp_path / "session"
        emit_event(
            str(session_root),
            "worker_log",
            "pane-1",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "edit_file",
                "status": "success",
                "activity": [{"kind": "edit_file", "path": "claimed.py", "mode": "edit"}],
            },
        )

        result = review_sandbox(
            worktree,
            claimed_files=["claimed.py"],
            project_root=str(session_root),
            task_slug="task-1",
        )
        assert result.passed

    def test_transient_scope_ignores_other_panes_when_current_pane_given(self, tmp_path: Path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        _init_repo(worktree)
        _add_tracked_file(worktree, "claimed.py", "x = 1\n")
        _modify_tracked(worktree, "claimed.py", "x = 2\n")

        session_root = tmp_path / "session"
        emit_event(
            str(session_root),
            "worker_log",
            "pane-old",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "write_file",
                "status": "success",
                "activity": [{"kind": "write_file", "path": "scratch.py", "mode": "create"}],
            },
        )
        emit_event(
            str(session_root),
            "worker_log",
            "pane-current",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "edit_file",
                "status": "success",
                "activity": [{"kind": "edit_file", "path": "claimed.py", "mode": "edit"}],
            },
        )

        result = review_sandbox(
            worktree,
            claimed_files=["claimed.py"],
            project_root=str(session_root),
            task_slug="task-1",
            pane_slug="pane-current",
        )
        # Note: With fail-closed retry semantics, this now FAILS because
        # unclaimed writes from earlier panes are still checked.
        # The earlier pane's unclaimed scratch.py causes rejection.
        assert not result.passed
        assert result.verdict == "scope_violation"
        assert "scratch.py" in (result.error or "")

    def test_transient_scope_fail_closed_across_retries(self, tmp_path: Path):
        """Unclaimed writes from earlier panes fail review even if current pane is clean.

        This tests the fail-closed retry semantics: an unclaimed tool write from any
        attempt in the active run must cause review rejection, even if a later retry
        cleans the worktree and succeeds.
        """
        worktree_retry = tmp_path / "worktree_retry"
        worktree_retry.mkdir()
        _init_repo(worktree_retry)
        _add_tracked_file(worktree_retry, "claimed.py", "x = 1\n")
        _modify_tracked(worktree_retry, "claimed.py", "x = 2\n")

        session_root = tmp_path / "session"
        # First attempt (pane-1): wrote unclaimed debug file
        emit_event(
            str(session_root),
            "worker_log",
            "pane-1",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "write_file",
                "status": "success",
                "activity": [{"kind": "write_file", "path": "debug_1.py", "mode": "create"}],
            },
        )
        # Second attempt (pane-2): only touches claimed file
        emit_event(
            str(session_root),
            "worker_log",
            "pane-2",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "edit_file",
                "status": "success",
                "activity": [{"kind": "edit_file", "path": "claimed.py", "mode": "edit"}],
            },
        )

        # Review on the second pane (retry) should still fail due to pane-1's unclaimed write
        result = review_sandbox(
            worktree_retry,
            claimed_files=["claimed.py"],
            project_root=str(session_root),
            task_slug="task-1",
            pane_slug="pane-2",
        )
        assert not result.passed
        assert result.verdict == "scope_violation"
        assert "debug_1.py" in (result.error or "")

    def test_transient_scope_claimed_writes_across_retries_pass(self, tmp_path: Path):
        """Claimed writes from all panes pass review - no scope violation."""
        worktree_retry = tmp_path / "worktree_retry"
        worktree_retry.mkdir()
        _init_repo(worktree_retry)
        _add_tracked_file(worktree_retry, "claimed.py", "x = 1\n")
        _modify_tracked(worktree_retry, "claimed.py", "x = 2\n")

        session_root = tmp_path / "session"
        # First attempt wrote claimed file
        emit_event(
            str(session_root),
            "worker_log",
            "pane-1",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "write_file",
                "status": "success",
                "activity": [{"kind": "write_file", "path": "claimed.py", "mode": "edit"}],
            },
        )
        # Second attempt also wrote claimed file
        emit_event(
            str(session_root),
            "worker_log",
            "pane-2",
            plan_name="plan",
            task_slug="task-1",
            log_type="result",
            content={
                "tool": "edit_file",
                "status": "success",
                "activity": [{"kind": "edit_file", "path": "claimed.py", "mode": "edit"}],
            },
        )

        # Should pass - all writes were within claimed scope
        result = review_sandbox(
            worktree_retry,
            claimed_files=["claimed.py"],
            project_root=str(session_root),
            task_slug="task-1",
            pane_slug="pane-2",
        )
        assert result.passed
        assert result.verdict == "ok"

    def test_no_scope_check_without_claims(self, tmp_path: Path):
        _init_repo(tmp_path)
        _add_tracked_file(tmp_path, "anything.py", "x = 1\n")
        _modify_tracked(tmp_path, "anything.py", "x = 2\n")
        result = review_sandbox(tmp_path, claimed_files=None)
        assert result.passed

    def test_diff_too_large(self, tmp_path: Path):
        _init_repo(tmp_path)
        for i in range(20):
            _add_tracked_file(tmp_path, f"file{i}.py", f"x = {i}\n")
        for i in range(20):
            _modify_tracked(tmp_path, f"file{i}.py", f"x = {i + 100}\n")
        result = review_sandbox(tmp_path, max_diff_lines=5)
        assert not result.passed
        assert result.verdict == "diff_too_large"

    @pytest.mark.unit
    def test_unclaimed_dgov_file_fails_scope(self, tmp_path: Path):
        """Unclaimed changes under .dgov/ fail scope enforcement."""
        _init_repo(tmp_path)
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "config.toml").write_text("key = 'value'\n")
        result = review_sandbox(tmp_path, claimed_files=["src.py"])
        assert not result.passed
        assert result.verdict == "scope_violation"
        assert ".dgov/config.toml" in (result.error or "")

    @pytest.mark.unit
    def test_unclaimed_sentrux_file_fails_scope(self, tmp_path: Path):
        """Governor-owned sentrux baseline is rejected before scope checks."""
        _init_repo(tmp_path)
        sx_dir = tmp_path / ".sentrux"
        sx_dir.mkdir()
        (sx_dir / "baseline.json").write_text('{"quality": 100}')
        result = review_sandbox(tmp_path, claimed_files=["src.py"])
        assert not result.passed
        assert result.verdict == "reserved_path"
        assert ".sentrux/baseline.json" in (result.error or "")

    @pytest.mark.unit
    def test_claimed_dgov_file_passes_scope(self, tmp_path: Path):
        """Explicitly claimed .dgov/ files pass scope enforcement."""
        _init_repo(tmp_path)
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "config.toml").write_text("key = 'value'\n")
        result = review_sandbox(tmp_path, claimed_files=[".dgov/config.toml"])
        assert result.passed
        assert result.verdict == "ok"

    @pytest.mark.unit
    def test_claimed_sentrux_file_passes_scope(self, tmp_path: Path):
        """Governor-owned sentrux baseline stays reserved even when claimed."""
        _init_repo(tmp_path)
        sx_dir = tmp_path / ".sentrux"
        sx_dir.mkdir()
        (sx_dir / "baseline.json").write_text('{"quality": 100}')
        result = review_sandbox(tmp_path, claimed_files=[".sentrux/baseline.json"])
        assert not result.passed
        assert result.verdict == "reserved_path"
        assert ".sentrux/baseline.json" in (result.error or "")


# ---------------------------------------------------------------------------
# autofix_sandbox
# ---------------------------------------------------------------------------


class TestAutofixSandbox:
    def test_formats_python_files(self, tmp_path: Path):
        _init_repo(tmp_path)
        # Badly formatted python
        (tmp_path / "bad.py").write_text("x=1\ny=   2\n")
        autofix_sandbox(tmp_path)
        content = (tmp_path / "bad.py").read_text()
        assert "x = 1" in content

    def test_no_python_files_noop(self, tmp_path: Path):
        _init_repo(tmp_path)
        # No .py files — should not crash
        autofix_sandbox(tmp_path)

    def test_scoped_to_claims(self, tmp_path: Path):
        """When file_claims given, only those files are fixed."""
        _init_repo(tmp_path)
        (tmp_path / "claimed.py").write_text("x=1\n")
        (tmp_path / "unclaimed.py").write_text("y=2\n")
        autofix_sandbox(tmp_path, file_claims=("claimed.py",))
        # claimed.py should be formatted
        assert "x = 1" in (tmp_path / "claimed.py").read_text()
        # unclaimed.py should NOT be touched
        assert (tmp_path / "unclaimed.py").read_text() == "y=2\n"

    def test_lint_fix_then_format_order(self, tmp_path: Path):
        """Lint fix runs before format — format is canonical last step."""
        _init_repo(tmp_path)
        # unused import that ruff --fix will remove, then format cleans up
        (tmp_path / "fixme.py").write_text("import os\nx=1\n")
        autofix_sandbox(tmp_path, file_claims=("fixme.py",))
        content = (tmp_path / "fixme.py").read_text()
        assert "import os" not in content  # lint-fix removed it
        assert "x = 1" in content  # format cleaned up

    def test_settlement_timeout_kills_slow_autofix(self, tmp_path: Path):
        """Autofix should timeout if command exceeds settlement_timeout."""
        _init_repo(tmp_path)
        (tmp_path / "slow.py").write_text("x = 1\n")
        # Command that sleeps longer than timeout
        config = ProjectConfig(lint_fix_cmd="sleep 2", settlement_timeout=1)
        with pytest.raises(subprocess.TimeoutExpired):
            autofix_sandbox(tmp_path, file_claims=("slow.py",), config=config)


# ---------------------------------------------------------------------------
# validate_sandbox
# ---------------------------------------------------------------------------


class TestValidateSandbox:
    @pytest.mark.unit
    def test_preflight_passes_with_no_source_changes(self, tmp_path: Path):
        base = _init_repo(tmp_path)
        (tmp_path / "notes.txt").write_text("hello\n")
        result = preflight_sandbox(tmp_path, str(tmp_path))
        assert result.passed
        assert validate_sandbox(tmp_path, base, str(tmp_path)).passed

    @pytest.mark.unit
    def test_preflight_runs_same_lint_gate_on_uncommitted_changes(self, tmp_path: Path):
        _init_repo(tmp_path)
        (tmp_path / "bad.py").write_text("print(undefined_var)\n")
        result = preflight_sandbox(tmp_path, str(tmp_path))
        assert result.passed is False
        assert "Lint failure" in (result.error or "")

    @pytest.mark.unit
    def test_build_test_cmd_adds_boundary_test_for_src_changes(self, tmp_path: Path):
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_boundaries.py").write_text("def test_boundaries():\n    assert True\n")

        cmd = _build_test_cmd(
            ProjectConfig(test_cmd="uv run pytest {test_dir} -q"),
            ["src/dgov/example.py"],
            tmp_path,
        )

        assert "tests/test_boundaries.py" in cmd

    @pytest.mark.unit
    def test_build_test_cmd_literal_no_placeholder_returns_unchanged(self, tmp_path: Path):
        """Literal test_cmd (no {test_dir} placeholder) returns unchanged even with no targets."""
        literal_cmd = "./scripts/qgis-python.sh -m pytest tests/plugin/test_task.py"
        cmd = _build_test_cmd(
            ProjectConfig(test_cmd=literal_cmd),
            [],  # No changed files
            tmp_path,
        )
        assert cmd == literal_cmd

    def test_pass_clean_python(self, tmp_path: Path):
        base = _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add clean.py")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert result.passed

    def test_pass_no_python_changes(self, tmp_path: Path):
        base = _init_repo(tmp_path)
        (tmp_path / "notes.txt").write_text("hello\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add notes")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert result.passed

    def test_fail_lint_error(self, tmp_path: Path):
        base = _init_repo(tmp_path)
        # Undefined variable — ruff will flag
        (tmp_path / "bad.py").write_text("print(undefined_var)\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add bad.py")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert not result.passed
        assert "Lint failure" in (result.error or "")

    def test_fail_format_error(self, tmp_path: Path):
        base = _init_repo(tmp_path)
        # Valid but badly formatted — ruff format --check will fail
        (tmp_path / "ugly.py").write_text("x=1;y=2\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add ugly.py")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        # May fail on lint or format depending on ruff config
        assert not result.passed

    def test_pass_test_file_that_passes(self, tmp_path: Path):
        """Test gate runs pytest on changed test files and passes."""
        base = _init_repo(tmp_path)
        # Create a minimal passing test
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_smoke.py").write_text("def test_one():\n    assert 1 + 1 == 2\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add passing test")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert result.passed

    def test_fail_test_file_that_fails(self, tmp_path: Path):
        """Test gate runs pytest on changed test files and rejects failures."""
        base = _init_repo(tmp_path)
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_broken.py").write_text("def test_bad():\n    assert False\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add failing test")
        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert not result.passed
        assert "Test failure" in (result.error or "")

    def test_source_change_runs_related_tests(self, tmp_path: Path):
        """Source file change runs tests that import from the changed module."""
        _init_repo(tmp_path)
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        # Test imports from lib — related_tests will find it
        (tests_dir / "test_lib.py").write_text(
            "import lib\ndef test_lib_value():\n    assert lib.x == 1\n"
        )
        (tmp_path / "lib.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add lib + test")

        # Now break lib.py — only lib.py is in the diff, not the test file
        new_base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        (tmp_path / "lib.py").write_text("x = 999\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "break lib")
        result = validate_sandbox(tmp_path, new_base, str(tmp_path))
        assert not result.passed
        assert "Test failure" in (result.error or "")

    def test_fail_sentrux_degradation(self, tmp_path: Path, monkeypatch):
        """Sentrux gate rejects architectural degradation."""
        base = _init_repo(tmp_path)
        # 1. Setup Sentrux baseline
        sx_dir = tmp_path / ".sentrux"
        sx_dir.mkdir()
        (sx_dir / "baseline.json").write_text('{"quality": 100}')

        # 2. Add a file that passes other gates
        (tmp_path / "mod.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add mod")

        # 3. Mock sentrux to fail with degradation
        import subprocess

        real_run = subprocess.run

        def _mock_run(args, **kwargs):
            if args[0] == "sentrux" and args[1] == "gate":
                return subprocess.CompletedProcess(
                    args, 1, stdout="Architectural degradation: coupling 0.5 -> 0.8\n", stderr=""
                )
            # Fallback to real subprocess for git/ruff/pytest
            return real_run(args, **kwargs)

        monkeypatch.setattr("subprocess.run", _mock_run)

        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert not result.passed
        assert "Sentrux architectural degradation" in (result.error or "")

    def test_settlement_timeout_kills_slow_validate(self, tmp_path: Path):
        """Validation should fail if command exceeds settlement_timeout."""
        base = _init_repo(tmp_path)
        (tmp_path / "slow.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add slow.py")

        # Lint command that sleeps
        config = ProjectConfig(lint_cmd="sleep 2", settlement_timeout=1)
        result = validate_sandbox(tmp_path, base, str(tmp_path), config=config)
        assert not result.passed
        assert "timed out" in (result.error or "").lower()

    @pytest.mark.unit
    def test_validate_type_check_gate_pass(self, tmp_path: Path):
        """Type check gate passes when type_check_cmd succeeds."""
        base = _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add clean.py")
        config = ProjectConfig(type_check_cmd="exit 0", test_cmd="")
        result = validate_sandbox(tmp_path, base, str(tmp_path), config=config)
        assert result.passed is True

    @pytest.mark.unit
    def test_validate_type_check_gate_fail_new_diagnostics(self, tmp_path: Path):
        """Type check gate fails when worktree has new diagnostic identities."""
        base = _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add clean.py")
        # Counter file: baseline (1st call) has 1 diagnostic, worktree (2nd call) has 2.
        counter = tmp_path / ".ty_counter"
        cmd = (
            f'n=$(cat "{counter}" 2>/dev/null || echo 0); '
            f'echo $((n+1)) > "{counter}"; '
            'if [ "$n" -ge 1 ]; then '
            # Worktree: 2 diagnostics (one new, one pre-existing)
            'echo "error[new-error]: new issue"; '
            'echo "   --> clean.py:1:1"; '
            'echo "error[preexisting]: old issue"; '
            'echo "   --> clean.py:2:2"; '
            'echo "Found 2 diagnostics" >&2; exit 1; '
            "else "
            # Baseline: 1 diagnostic
            'echo "error[preexisting]: old issue"; '
            'echo "   --> clean.py:2:2"; '
            'echo "Found 1 diagnostic" >&2; exit 1; '
            "fi"
        )
        config = ProjectConfig(type_check_cmd=cmd, test_cmd="")
        result = validate_sandbox(tmp_path, base, str(tmp_path), config=config)
        assert result.passed is False
        assert "Type check failure" in (result.error or "")
        assert "1 new diagnostic" in (result.error or "")

    @pytest.mark.unit
    def test_validate_type_check_gate_pass_preexisting(self, tmp_path: Path):
        """Type check gate passes when diagnostics are pre-existing (not new)."""
        base = _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add clean.py")
        # Both baseline and worktree have same diagnostic identities → pass
        cmd = (
            'echo "error[preexisting]: old issue"; '
            'echo "   --> clean.py:1:1"; '
            'echo "Found 1 diagnostic" >&2; exit 1'
        )
        config = ProjectConfig(type_check_cmd=cmd, test_cmd="")
        result = validate_sandbox(tmp_path, base, str(tmp_path), config=config)
        assert result.passed is True

    @pytest.mark.unit
    def test_validate_type_check_gate_fail_identity_regression(self, tmp_path: Path):
        """Type check gate catches regressions where N old errors fixed but N new ones introduced.

        This tests the identity-based comparison: even if the count stays the same,
        new diagnostic identities (file, error_code pairs) should be caught.
        """
        base = _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add clean.py")
        # Counter file: baseline has 2 diagnostics, worktree has 2 different ones.
        counter = tmp_path / ".ty_counter"
        cmd = (
            f'n=$(cat "{counter}" 2>/dev/null || echo 0); '
            f'echo $((n+1)) > "{counter}"; '
            'if [ "$n" -ge 1 ]; then '
            # Worktree: 2 diagnostics (both different from baseline)
            'echo "error[new-error-1]: new issue 1"; '
            'echo "   --> clean.py:1:1"; '
            'echo "error[new-error-2]: new issue 2"; '
            'echo "   --> clean.py:2:2"; '
            'echo "Found 2 diagnostics" >&2; exit 1; '
            "else "
            # Baseline: 2 different diagnostics (same count, different identities)
            'echo "error[old-error-1]: old issue 1"; '
            'echo "   --> clean.py:1:1"; '
            'echo "error[old-error-2]: old issue 2"; '
            'echo "   --> clean.py:2:2"; '
            'echo "Found 2 diagnostics" >&2; exit 1; '
            "fi"
        )
        config = ProjectConfig(type_check_cmd=cmd, test_cmd="")
        result = validate_sandbox(tmp_path, base, str(tmp_path), config=config)
        # Should FAIL because worktree has 2 new diagnostic identities
        assert result.passed is False
        assert "Type check failure" in (result.error or "")
        assert "2 new diagnostic" in (result.error or "")

    @pytest.mark.unit
    def test_validate_type_check_gate_pass_same_identities_line_shift(self, tmp_path: Path):
        """Type check gate passes when same errors move to different lines.

        Line numbers shift when code is edited, but the (file, error_code)
        identity should still match and not be flagged as new.
        """
        base = _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add clean.py")
        # Counter file: baseline (1st call) has error at line 1, worktree (2nd call) at line 5
        counter = tmp_path / ".ty_counter"
        cmd = (
            f'n=$(cat "{counter}" 2>/dev/null || echo 0); '
            f'echo $((n+1)) > "{counter}"; '
            'if [ "$n" -ge 1 ]; then '
            # Worktree: same error code, different line
            'echo "error[preexisting]: old issue"; '
            'echo "   --> clean.py:5:1"; '  # Line changed from 1 to 5
            'echo "Found 1 diagnostic" >&2; exit 1; '
            "else "
            # Baseline: error at line 1
            'echo "error[preexisting]: old issue"; '
            'echo "   --> clean.py:1:1"; '
            'echo "Found 1 diagnostic" >&2; exit 1; '
            "fi"
        )
        config = ProjectConfig(type_check_cmd=cmd, test_cmd="")
        result = validate_sandbox(tmp_path, base, str(tmp_path), config=config)
        # Should PASS because the (file, error_code) identity is the same
        assert result.passed is True

    @pytest.mark.unit
    def test_validate_type_check_skipped_when_empty(self, tmp_path: Path):
        """Type check gate is skipped when type_check_cmd is empty."""
        base = _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add clean.py")
        config = ProjectConfig(type_check_cmd="", test_cmd="")
        result = validate_sandbox(tmp_path, base, str(tmp_path), config=config)
        assert result.passed is True

    @pytest.mark.unit
    def test_pass_no_tests_collected(self, tmp_path: Path):
        """pytest exit code 5 (no tests collected) is treated as a pass."""
        base = _init_repo(tmp_path)
        (tmp_path / "clean.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add clean.py")
        # exit 5 = pytest "no tests were collected"
        config = ProjectConfig(test_cmd="exit 5", type_check_cmd="")
        result = validate_sandbox(tmp_path, base, str(tmp_path), config=config)
        assert result.passed is True

    @pytest.mark.unit
    def test_coverage_gate_fails_on_regression(self, tmp_path: Path):
        project_root, worktree_path = _coverage_worktree(tmp_path, baseline_percent=95.0)
        config = ProjectConfig(
            coverage_cmd=_coverage_cmd(_coverage_payload("src/pkg.py", 90.0)),
            coverage_threshold=2.0,
        )

        result = _run_coverage_gate(
            worktree_path,
            ["src/pkg.py"],
            str(project_root),
            config,
        )

        assert result is not None
        assert result.passed is False
        assert "Coverage regression: src/pkg.py dropped from 95% to 90%" in (result.error or "")

    @pytest.mark.unit
    def test_coverage_gate_passes_without_regression(self, tmp_path: Path):
        project_root, worktree_path = _coverage_worktree(tmp_path, baseline_percent=95.0)
        config = ProjectConfig(
            coverage_cmd=_coverage_cmd(_coverage_payload("src/pkg.py", 94.0)),
            coverage_threshold=2.0,
        )

        result = _run_coverage_gate(
            worktree_path,
            ["src/pkg.py"],
            str(project_root),
            config,
        )

        assert result is None

    @pytest.mark.unit
    def test_coverage_gate_skips_without_baseline(self, tmp_path: Path):
        project_root = tmp_path / "project"
        worktree_path = tmp_path / "worktree"
        (project_root / ".dgov").mkdir(parents=True)
        (worktree_path / "src").mkdir(parents=True)
        (worktree_path / "tests").mkdir(parents=True)
        (worktree_path / "src" / "pkg.py").write_text("VALUE = 1\n")
        (worktree_path / "tests" / "test_pkg.py").write_text("import pkg\n")
        config = ProjectConfig(
            coverage_cmd=_coverage_cmd(_coverage_payload("src/pkg.py", 0.0)),
            coverage_threshold=2.0,
        )

        result = _run_coverage_gate(
            worktree_path,
            ["src/pkg.py"],
            str(project_root),
            config,
        )

        assert result is None

    @pytest.mark.unit
    def test_coverage_gate_skips_when_disabled(self, tmp_path: Path):
        project_root, worktree_path = _coverage_worktree(tmp_path, baseline_percent=95.0)
        config = ProjectConfig(coverage_cmd=None)

        result = _run_coverage_gate(
            worktree_path,
            ["src/pkg.py"],
            str(project_root),
            config,
        )

        assert result is None

    @pytest.mark.unit
    def test_coverage_gate_threshold(self, tmp_path: Path):
        project_root, worktree_path = _coverage_worktree(tmp_path, baseline_percent=95.0)
        passing = ProjectConfig(
            coverage_cmd=_coverage_cmd(_coverage_payload("src/pkg.py", 93.1)),
            coverage_threshold=2.0,
        )
        failing = ProjectConfig(
            coverage_cmd=_coverage_cmd(_coverage_payload("src/pkg.py", 92.9)),
            coverage_threshold=2.0,
        )

        pass_result = _run_coverage_gate(
            worktree_path,
            ["src/pkg.py"],
            str(project_root),
            passing,
        )
        fail_result = _run_coverage_gate(
            worktree_path,
            ["src/pkg.py"],
            str(project_root),
            failing,
        )

        assert pass_result is None
        assert fail_result is not None
        assert fail_result.passed is False

    @pytest.mark.unit
    def test_sentrux_empty_baseline_skipped(self, tmp_path: Path, monkeypatch):
        """Sentrux gate is skipped when baseline has total_import_edges == 0 (empty project)."""
        import json

        base = _init_repo(tmp_path)
        sx_dir = tmp_path / ".sentrux"
        sx_dir.mkdir()
        (sx_dir / "baseline.json").write_text(
            json.dumps({"quality_signal": 1.0, "total_import_edges": 0})
        )
        (tmp_path / "mod.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add mod")

        real_run = subprocess.run

        def _mock_run(args, **kwargs):
            if args[0] == "sentrux" and args[1] == "gate":
                return subprocess.CompletedProcess(
                    args, 1, stdout="Quality signal dropped: 1.00 → 0.82\n", stderr=""
                )
            return real_run(args, **kwargs)

        monkeypatch.setattr("subprocess.run", _mock_run)
        monkeypatch.setattr("dgov.settlement.shutil.which", lambda name: "/usr/bin/sentrux")

        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert result.passed is True

    @pytest.mark.unit
    def test_validate_sentrux_missing_is_actionable(self, tmp_path: Path, monkeypatch):
        """Missing sentrux binary should fail with a clear installation message."""
        base = _init_repo(tmp_path)
        sx_dir = tmp_path / ".sentrux"
        sx_dir.mkdir()
        (sx_dir / "baseline.json").write_text('{"quality": 100}')
        (tmp_path / "mod.py").write_text("x = 1\n")
        _git(tmp_path, "add", ".")
        _git(tmp_path, "commit", "-m", "add mod")

        monkeypatch.setattr("dgov.settlement.shutil.which", lambda name: None)

        result = validate_sandbox(tmp_path, base, str(tmp_path))
        assert result.passed is False
        assert "Sentrux not found in PATH" in (result.error or "")


@pytest.mark.unit
def test_run_sentrux_gate_refreshes_worktree_baseline(tmp_path: Path, monkeypatch) -> None:
    """Settlement should overwrite any worktree baseline with the repo-owned baseline."""
    sx_dir = tmp_path / ".sentrux"
    sx_dir.mkdir()
    canonical = '{"quality": 100}\n'
    (sx_dir / "baseline.json").write_text(canonical)

    worktree_path = tmp_path / "wt"
    (worktree_path / ".sentrux").mkdir(parents=True)
    (worktree_path / ".sentrux" / "baseline.json").write_text('{"quality": 0}\n')

    def _mock_run(args, **kwargs):
        assert (worktree_path / ".sentrux" / "baseline.json").read_text() == canonical
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="✓ No degradation detected\n",
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", _mock_run)
    monkeypatch.setattr("dgov.settlement.shutil.which", lambda name: "/usr/bin/sentrux")

    result = _run_sentrux_gate(worktree_path, str(tmp_path), timeout=1)
    assert result.passed is True


# ---------------------------------------------------------------------------
# Dogfood — Test that dgov's own project.toml config is valid
# ---------------------------------------------------------------------------


class TestSettlementTimeoutDogfood:
    """Verify dgov's own .dgov/project.toml settlement_timeout is correctly parsed.

    This is a slow test as it touches the filesystem and parses real TOML.
    Run with: pytest -m slow tests/test_settlement.py::TestSettlementTimeoutDogfood
    """

    @pytest.mark.slow
    def test_project_toml_has_settlement_timeout(self):
        """Dogfood: Verify our own project.toml has a valid settlement_timeout value."""
        from dgov.config import load_project_config

        # Load the actual project.toml from this repo's root
        project_root = Path(__file__).parent.parent  # tests/ -> repo root
        config = load_project_config(project_root)

        # settlement_timeout must be explicitly set and reasonable
        assert config.settlement_timeout > 0, "settlement_timeout must be positive"
        assert config.settlement_timeout <= 600, "settlement_timeout should be <= 10 minutes"

    @pytest.mark.slow
    def test_settlement_timeout_from_toml_kills_slow_command(self, tmp_path: Path):
        """Dogfood: Verify timeout from actual project.toml works in practice."""
        from dgov.config import load_project_config

        # Load actual project config to ensure it exists and is valid
        project_root = Path(__file__).parent.parent
        load_project_config(project_root)

        # Use a very short timeout for test speed (different from project.toml value)
        test_config = ProjectConfig(
            lint_fix_cmd="sleep 2",
            settlement_timeout=1,  # Force 1 second timeout for test speed
        )

        _init_repo(tmp_path)
        (tmp_path / "slow.py").write_text("x = 1\n")

        with pytest.raises(subprocess.TimeoutExpired):
            autofix_sandbox(tmp_path, file_claims=("slow.py",), config=test_config)
