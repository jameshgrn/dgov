"""Unit tests for dgov/lifecycle.py."""

from __future__ import annotations

import stat
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.backend import set_backend
from dgov.persistence import WorkerPane, add_pane, get_pane

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def mock_backend():
    import dgov.backend as _be

    prev = _be._backend
    mock = MagicMock()
    mock.create_pane.return_value = "%1"
    mock.is_alive.return_value = False
    mock.bulk_info.return_value = {}
    set_backend(mock)
    yield mock
    _be._backend = prev


def _add_pane(
    tmp_path: Path,
    slug: str,
    parent_slug: str = "",
    role: str = "worker",
    state: str = "active",
    **kw,
) -> None:
    add_pane(
        str(tmp_path),
        WorkerPane(
            slug=slug,
            prompt=kw.get("prompt", "test"),
            pane_id=kw.get("pane_id", f"%{slug}"),
            agent=kw.get("agent", "claude"),
            project_root=str(tmp_path),
            worktree_path=kw.get("worktree_path", str(tmp_path / slug)),
            branch_name=kw.get("branch_name", slug),
            owns_worktree=kw.get("owns_worktree", True),
            role=role,
            parent_slug=parent_slug,
            created_at=kw.get("created_at", time.time()),
            state=state,
        ),
    )


# ──────────────────────────────────────────────────────────────
# TestStateIcon
# ──────────────────────────────────────────────────────────────


class TestStateIcon:
    def test_active(self) -> None:
        from dgov.lifecycle import _state_icon

        assert _state_icon("active") == "~"

    def test_done(self) -> None:
        from dgov.lifecycle import _state_icon

        assert _state_icon("done") == "ok"

    def test_merged(self) -> None:
        from dgov.lifecycle import _state_icon

        assert _state_icon("merged") == "+"

    def test_timed_out(self) -> None:
        from dgov.lifecycle import _state_icon

        assert _state_icon("timed_out") == "!"

    def test_failed(self) -> None:
        from dgov.lifecycle import _state_icon

        assert _state_icon("failed") == "X"

    def test_unknown_state_returns_empty_string(self) -> None:
        from dgov.lifecycle import _state_icon

        assert _state_icon("bogus") == ""
        assert _state_icon("") == ""


# ──────────────────────────────────────────────────────────────
# TestBuildPaneTitle
# ──────────────────────────────────────────────────────────────


class TestBuildPaneTitle:
    def test_basic_title_format(self) -> None:
        from dgov.lifecycle import _build_pane_title

        title = _build_pane_title("pi", "fix-parser", "/tmp/proj")
        assert title == "[pi] fix-parser"

    def test_title_with_state_icon(self) -> None:
        from dgov.lifecycle import _build_pane_title

        title = _build_pane_title("claude", "add-tests", "/tmp/proj", state="active")
        assert title == "[claude] add-tests ~"

    def test_title_without_state_no_icon(self) -> None:
        from dgov.lifecycle import _build_pane_title

        title = _build_pane_title("claude", "add-tests", "/tmp/proj", state="")
        assert title == "[claude] add-tests"

    def test_title_unknown_state_no_icon(self) -> None:
        from dgov.lifecycle import _build_pane_title

        title = _build_pane_title("pi", "slug", "/tmp/proj", state="bogus")
        assert title == "[pi] slug"


# ──────────────────────────────────────────────────────────────
# TestEnsureDgovGitignored
# ──────────────────────────────────────────────────────────────


class TestEnsureDgovGitignored:
    def test_creates_gitignore_if_missing(self, tmp_path: Path) -> None:
        from dgov.lifecycle import ensure_dgov_gitignored

        ensure_dgov_gitignored(str(tmp_path))

        gi = tmp_path / ".gitignore"
        assert gi.is_file()
        assert ".dgov/\n" in gi.read_text()

    def test_appends_to_existing_gitignore(self, tmp_path: Path) -> None:
        from dgov.lifecycle import ensure_dgov_gitignored

        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules/\n", encoding="utf-8")

        ensure_dgov_gitignored(str(tmp_path))

        content = gi.read_text()
        assert "node_modules/\n" in content
        assert ".dgov/\n" in content

    def test_skips_if_already_present(self, tmp_path: Path) -> None:
        from dgov.lifecycle import ensure_dgov_gitignored

        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules/\n.dgov/\n", encoding="utf-8")

        ensure_dgov_gitignored(str(tmp_path))

        # Should not duplicate
        content = gi.read_text()
        assert content.count(".dgov/") == 1

    def test_adds_newline_if_file_lacks_trailing_newline(self, tmp_path: Path) -> None:
        from dgov.lifecycle import ensure_dgov_gitignored

        gi = tmp_path / ".gitignore"
        gi.write_text("node_modules/", encoding="utf-8")  # no trailing newline

        ensure_dgov_gitignored(str(tmp_path))

        content = gi.read_text()
        # Should have newline before .dgov/
        assert "node_modules/\n.dgov/\n" in content


# ──────────────────────────────────────────────────────────────
# TestTriggerHook
# ──────────────────────────────────────────────────────────────


class TestTriggerHook:
    def test_runs_hook_from_dgov_hooks(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _trigger_hook

        hooks_dir = tmp_path / ".dgov" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "worktree_created"
        hook.write_text("#!/bin/sh\necho ran\n", encoding="utf-8")
        hook.chmod(0o755)

        result = _trigger_hook("worktree_created", str(tmp_path), {})
        assert result is True

    def test_returns_false_when_no_hook_exists(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _trigger_hook

        result = _trigger_hook("worktree_created", str(tmp_path), {})
        assert result is False

    def test_returns_false_on_timeout(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _trigger_hook

        hooks_dir = tmp_path / ".dgov" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "worktree_created"
        hook.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
        hook.chmod(0o755)

        result = _trigger_hook("worktree_created", str(tmp_path), {}, timeout=1)
        assert result is False

    def test_returns_false_on_nonzero_exit(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _trigger_hook

        hooks_dir = tmp_path / ".dgov" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "worktree_created"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        result = _trigger_hook("worktree_created", str(tmp_path), {})
        assert result is False

    def test_skips_non_executable_hook(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _trigger_hook

        hooks_dir = tmp_path / ".dgov" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook = hooks_dir / "worktree_created"
        hook.write_text("#!/bin/sh\necho ran\n", encoding="utf-8")
        # No chmod -- not executable

        result = _trigger_hook("worktree_created", str(tmp_path), {})
        assert result is False

    def test_priority_dgov_hooks_over_dgov_dash_hooks(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _trigger_hook

        # .dgov-hooks/ has a hook
        dash_hooks = tmp_path / ".dgov-hooks"
        dash_hooks.mkdir()
        hook_dash = dash_hooks / "worktree_created"
        hook_dash.write_text("#!/bin/sh\necho dash\n", encoding="utf-8")
        hook_dash.chmod(0o755)

        # .dgov/hooks/ has a hook (higher priority)
        hooks_dir = tmp_path / ".dgov" / "hooks"
        hooks_dir.mkdir(parents=True)
        hook_local = hooks_dir / "worktree_created"
        hook_local.write_text("#!/bin/sh\necho local\n", encoding="utf-8")
        hook_local.chmod(0o755)

        result = _trigger_hook("worktree_created", str(tmp_path), {})
        assert result is True

    def test_priority_dgov_dash_hooks_over_home(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _trigger_hook

        # .dgov-hooks/ has a hook
        dash_hooks = tmp_path / ".dgov-hooks"
        dash_hooks.mkdir()
        hook_dash = dash_hooks / "worktree_created"
        hook_dash.write_text("#!/bin/sh\necho dash\n", encoding="utf-8")
        hook_dash.chmod(0o755)

        # ~/.dgov/hooks/ would have one but .dgov-hooks/ wins
        home_hooks = Path.home() / ".dgov" / "hooks"
        home_hooks.mkdir(parents=True, exist_ok=True)
        home_hook = home_hooks / "worktree_created"
        home_hook.write_text("#!/bin/sh\necho home\n", encoding="utf-8")
        home_hook.chmod(0o755)

        try:
            result = _trigger_hook("worktree_created", str(tmp_path), {})
            assert result is True
        finally:
            home_hook.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────
# TestInstallWorkerHooks
# ──────────────────────────────────────────────────────────────


class TestInstallWorkerHooks:
    def test_creates_hooks_dir(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _install_worker_hooks

        with patch("dgov.lifecycle.subprocess.run"):
            _install_worker_hooks(str(tmp_path))

        hooks_dir = tmp_path / ".dgov-worker-hooks"
        assert hooks_dir.is_dir()

    def test_writes_pre_merge_commit_hook(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _PRE_MERGE_COMMIT_HOOK, _install_worker_hooks

        with patch("dgov.lifecycle.subprocess.run"):
            _install_worker_hooks(str(tmp_path))

        hook_file = tmp_path / ".dgov-worker-hooks" / "pre-merge-commit"
        assert hook_file.is_file()
        assert hook_file.read_text(encoding="utf-8") == _PRE_MERGE_COMMIT_HOOK

    def test_hook_is_executable(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _install_worker_hooks

        with patch("dgov.lifecycle.subprocess.run"):
            _install_worker_hooks(str(tmp_path))

        hook_file = tmp_path / ".dgov-worker-hooks" / "pre-merge-commit"
        assert hook_file.stat().st_mode & stat.S_IXUSR

    def test_sets_core_hooks_path(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _install_worker_hooks

        with patch("dgov.lifecycle.subprocess.run") as mock_run:
            _install_worker_hooks(str(tmp_path))

        calls = mock_run.call_args_list
        assert len(calls) == 1
        args = calls[0][0][0]
        assert args == [
            "git",
            "-C",
            str(tmp_path),
            "config",
            "core.hooksPath",
            str(tmp_path / ".dgov-worker-hooks"),
        ]


# ──────────────────────────────────────────────────────────────
# TestCreateWorktree
# ──────────────────────────────────────────────────────────────


class TestCreateWorktree:
    def test_creates_new_branch_when_not_exists(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _create_worktree

        proj = tmp_path / "proj"
        proj.mkdir()
        wt = tmp_path / "wt"

        # First call (rev-parse) returns non-zero → branch doesn't exist
        # Second call (worktree add -b) succeeds
        mock_results = [
            MagicMock(returncode=1, stdout="", stderr=""),  # rev-parse
            MagicMock(returncode=0, stdout="", stderr=""),  # worktree add -b
        ]
        with patch("dgov.lifecycle.subprocess.run", side_effect=mock_results) as mock_run:
            _create_worktree(str(proj), str(wt), "new-branch")

        # Should call rev-parse first, then worktree add -b
        assert mock_run.call_count == 2
        add_args = mock_run.call_args_list[1][0][0]
        assert add_args[0:6] == ["git", "-C", str(proj), "worktree", "add", "-b"]
        assert "new-branch" in add_args

    def test_reuses_existing_branch(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _create_worktree

        proj = tmp_path / "proj"
        proj.mkdir()
        wt = tmp_path / "wt"  # directory doesn't exist, so no dir check subprocess call

        # rev-parse returns 0 → branch exists
        mock_results = [
            MagicMock(returncode=0, stdout="", stderr=""),  # rev-parse
            MagicMock(returncode=0, stdout="", stderr=""),  # worktree add (no -b)
        ]
        with patch("dgov.lifecycle.subprocess.run", side_effect=mock_results) as mock_run:
            _create_worktree(str(proj), str(wt), "existing-branch")

        add_args = mock_run.call_args_list[1][0][0]
        # Should use worktree add (without -b) for existing branch
        assert "-b" not in add_args
        assert "existing-branch" in add_args

    def test_reuses_existing_worktree_directory(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _create_worktree

        proj = tmp_path / "proj"
        proj.mkdir()
        wt = tmp_path / "wt"
        wt.mkdir()  # directory already exists

        # rev-parse in wt succeeds → worktree is valid, skip
        mock_results = [
            MagicMock(returncode=0, stdout="", stderr=""),
        ]
        with patch("dgov.lifecycle.subprocess.run", side_effect=mock_results) as mock_run:
            _create_worktree(str(proj), str(wt), "branch")

        # Only the dir check should run; no worktree add
        assert mock_run.call_count == 1

    def test_raises_runtime_error_on_failure(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _create_worktree

        proj = tmp_path / "proj"
        proj.mkdir()
        wt = tmp_path / "wt"

        mock_results = [
            MagicMock(returncode=1, stdout="", stderr=""),  # rev-parse
            MagicMock(returncode=1, stdout="", stderr="fail"),  # worktree add fails
        ]
        mock_results[1].stderr = "fatal: some error"
        # CalledProcessError for the check=True call
        with patch("dgov.lifecycle.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=1, stdout="", stderr=""),
                subprocess.CalledProcessError(1, "git", stderr="fatal: some error"),
            ]
            with pytest.raises(RuntimeError, match="Failed to create worktree"):
                _create_worktree(str(proj), str(wt), "bad-branch")


# ──────────────────────────────────────────────────────────────
# TestFullCleanup
# ──────────────────────────────────────────────────────────────


class TestFullCleanup:
    def test_deletes_signal_and_log_files(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _full_cleanup
        from dgov.persistence import STATE_DIR

        sr = str(tmp_path)
        _add_pane(tmp_path, "test-pane")

        # Create signal and log files
        done_dir = tmp_path / STATE_DIR / "done"
        done_dir.mkdir(parents=True)
        (done_dir / "test-pane").touch()
        (done_dir / "test-pane.exit").touch()
        logs_dir = tmp_path / STATE_DIR / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "test-pane.log").touch()

        pane = get_pane(sr, "test-pane")
        assert pane is not None

        with patch("dgov.lifecycle.subprocess.run"):
            result = _full_cleanup(sr, sr, "test-pane", pane)

        assert (done_dir / "test-pane").exists() is False
        assert (done_dir / "test-pane.exit").exists() is False
        assert (logs_dir / "test-pane.log").exists() is False
        assert result["cleaned"] is True

    def test_kills_tmux_pane(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(tmp_path, "test-pane", pane_id="%42")

        pane = get_pane(sr, "test-pane")
        with patch("dgov.lifecycle.subprocess.run"):
            _full_cleanup(sr, sr, "test-pane", pane)

        mock_backend.destroy.assert_called_once_with("%42")

    def test_removes_worktree_and_branch(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(tmp_path, "owned-pane", owns_worktree=True)

        pane = get_pane(sr, "owned-pane")
        with patch("dgov.lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _full_cleanup(sr, sr, "owned-pane", pane)

        # Should have called worktree remove and branch delete
        calls_args = [c[0][0] for c in mock_run.call_args_list]
        worktree_remove = [a for a in calls_args if "worktree" in a and "remove" in a]
        branch_delete = [a for a in calls_args if "branch" in a and "-d" in a]
        assert len(worktree_remove) >= 1
        assert len(branch_delete) >= 1

    def test_skips_worktree_removal_when_not_owned(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(tmp_path, "borrowed-pane", owns_worktree=False)

        pane = get_pane(sr, "borrowed-pane")
        with patch("dgov.lifecycle.subprocess.run") as mock_run:
            _full_cleanup(sr, sr, "borrowed-pane", pane)

        # No worktree remove or branch delete should be called
        calls_args = [c[0][0] for c in mock_run.call_args_list]
        worktree_remove = [a for a in calls_args if "worktree" in a and "remove" in a]
        assert len(worktree_remove) == 0

    def test_skip_worktree_if_dirty(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        wt_path = tmp_path / "dirty-pane"
        wt_path.mkdir()
        _add_pane(tmp_path, "dirty-pane", worktree_path=str(wt_path), owns_worktree=True)

        pane = get_pane(sr, "dirty-pane")

        # git status --porcelain returns output (dirty)
        mock_results = [
            MagicMock(returncode=0, stdout="M file.py\n", stderr=""),  # status --porcelain
            MagicMock(returncode=0, stdout="", stderr=""),  # worktree prune
        ]
        with patch("dgov.lifecycle.subprocess.run", side_effect=mock_results):
            result = _full_cleanup(sr, sr, "dirty-pane", pane, skip_worktree_if_dirty=True)

        assert result["skipped_worktree"] is True

    def test_uses_d_flag_for_merged_state(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(tmp_path, "merged-pane", state="merged", owns_worktree=True)

        pane = get_pane(sr, "merged-pane")
        with patch("dgov.lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _full_cleanup(sr, sr, "merged-pane", pane)

        calls_args = [c[0][0] for c in mock_run.call_args_list]
        branch_calls = [a for a in calls_args if "branch" in a and a[0] == "git"]
        assert any("-D" in a for a in branch_calls)

    def test_uses_d_flag_for_non_merged_state(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(tmp_path, "done-pane", state="done", owns_worktree=True)

        pane = get_pane(sr, "done-pane")
        with patch("dgov.lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _full_cleanup(sr, sr, "done-pane", pane)

        calls_args = [c[0][0] for c in mock_run.call_args_list]
        branch_calls = [a for a in calls_args if "branch" in a and a[0] == "git"]
        assert any("-d" in a and "-D" not in a for a in branch_calls)

    def test_branch_kept_when_delete_fails(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(tmp_path, "keep-branch-pane", owns_worktree=True)

        pane = get_pane(sr, "keep-branch-pane")

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "branch" in cmd and ("-d" in cmd or "-D" in cmd):
                return MagicMock(returncode=1, stdout="", stderr="error")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("dgov.lifecycle.subprocess.run", side_effect=side_effect):
            result = _full_cleanup(sr, sr, "keep-branch-pane", pane)

        assert result["branch_kept"] is True


# ──────────────────────────────────────────────────────────────
# TestPiExtensionFlags
# ──────────────────────────────────────────────────────────────


class TestPiExtensionFlags:
    def test_returns_empty_string_when_no_extensions_dir(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _pi_extension_flags

        with patch("importlib.resources.files") as mock_files:
            ext_path = tmp_path / "nonexistent"
            mock_files.return_value.__truediv__ = MagicMock(return_value=ext_path)
            result = _pi_extension_flags(str(tmp_path))

        assert result == ""

    def test_returns_extension_flags_for_ts_files(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _pi_extension_flags

        ext_dir = tmp_path / "pi-extensions"
        ext_dir.mkdir()
        (ext_dir / "b.ts").touch()
        (ext_dir / "a.ts").touch()
        (ext_dir / "ignore.txt").touch()

        with patch("importlib.resources.files") as mock_files:
            mock_files.return_value.__truediv__ = MagicMock(return_value=ext_dir)
            result = _pi_extension_flags(str(tmp_path))

        assert "--extension" in result
        assert ".ts" in result
        # .txt should not be included
        assert ".txt" not in result


# ──────────────────────────────────────────────────────────────
# TestResumeWorkerPane
# ──────────────────────────────────────────────────────────────


class TestResumeWorkerPane:
    def test_error_for_missing_pane(self, tmp_path: Path) -> None:
        from dgov.lifecycle import resume_worker_pane

        result = resume_worker_pane(str(tmp_path), "nonexistent")
        assert "error" in result
        assert "nonexistent" in result["error"]

    def test_error_for_missing_worktree(self, tmp_path: Path) -> None:
        from dgov.lifecycle import resume_worker_pane

        sr = str(tmp_path)
        _add_pane(tmp_path, "no-wt-pane", worktree_path="/tmp/does-not-exist")

        result = resume_worker_pane(sr, "no-wt-pane")
        assert "error" in result
        assert "Worktree no longer exists" in result["error"]

    def test_error_for_missing_branch(self, tmp_path: Path) -> None:
        from dgov.lifecycle import resume_worker_pane

        sr = str(tmp_path)
        wt_path = tmp_path / "wt-exists"
        wt_path.mkdir()
        _add_pane(
            tmp_path, "no-branch-pane", worktree_path=str(wt_path), branch_name="gone-branch"
        )

        with patch("dgov.lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            result = resume_worker_pane(sr, "no-branch-pane")

        assert "error" in result
        assert "Branch no longer exists" in result["error"]

    def test_successful_resume_updates_state(self, tmp_path: Path) -> None:
        from dgov.lifecycle import resume_worker_pane

        sr = str(tmp_path)
        wt_path = tmp_path / "wt-resume"
        wt_path.mkdir()
        _add_pane(tmp_path, "resume-pane", worktree_path=str(wt_path), branch_name="resume-branch")

        mock_results = [
            MagicMock(returncode=0, stdout="", stderr=""),  # rev-parse branch
            MagicMock(
                returncode=0, stdout="", stderr=""
            ),  # git rev-parse HEAD (base_sha in _setup)
        ]

        with (
            patch("dgov.lifecycle.subprocess.run", side_effect=mock_results),
            patch("dgov.lifecycle.load_registry") as mock_registry,
            patch("dgov.lifecycle.get_backend") as mock_get_be,
            patch("dgov.lifecycle._setup_and_launch_agent"),
            patch("dgov.tmux.wait_for_shell_ready", return_value=True),
        ):
            mock_be = MagicMock()
            mock_be.is_alive.return_value = False
            mock_be.create_worker_pane.return_value = "%99"
            mock_get_be.return_value = mock_be

            agent_def = MagicMock()
            agent_def.env = {}
            agent_def.health_check = None
            agent_def.max_concurrent = None
            agent_def.interactive = False
            agent_def.prompt_transport = "command"
            agent_def.prompt_command = "pi"
            mock_registry.return_value = {"pi": agent_def}

            result = resume_worker_pane(sr, "resume-pane", agent="pi")

        assert result.get("resumed") is True
        assert result.get("slug") == "resume-pane"
        assert result.get("agent") == "pi"

        # Verify state was updated
        pane = get_pane(sr, "resume-pane")
        assert pane is not None
        assert pane["state"] == "active"
        assert pane["pane_id"] == "%99"

    def test_resume_uses_original_agent_when_none_specified(self, tmp_path: Path) -> None:
        from dgov.lifecycle import resume_worker_pane

        sr = str(tmp_path)
        wt_path = tmp_path / "wt-resume2"
        wt_path.mkdir()
        _add_pane(
            tmp_path,
            "resume-pane2",
            agent="claude",
            worktree_path=str(wt_path),
            branch_name="resume-branch2",
        )

        mock_results = [
            MagicMock(returncode=0, stdout="", stderr=""),  # rev-parse branch
            MagicMock(returncode=0, stdout="", stderr=""),  # git rev-parse HEAD
        ]

        with (
            patch("dgov.lifecycle.subprocess.run", side_effect=mock_results),
            patch("dgov.lifecycle.load_registry") as mock_registry,
            patch("dgov.lifecycle.get_backend") as mock_get_be,
            patch("dgov.lifecycle._setup_and_launch_agent"),
            patch("dgov.tmux.wait_for_shell_ready", return_value=True),
        ):
            mock_be = MagicMock()
            mock_be.is_alive.return_value = False
            mock_be.create_worker_pane.return_value = "%88"
            mock_get_be.return_value = mock_be

            agent_def = MagicMock()
            agent_def.env = {}
            agent_def.health_check = None
            agent_def.max_concurrent = None
            agent_def.interactive = False
            agent_def.prompt_transport = "command"
            agent_def.prompt_command = "claude"
            mock_registry.return_value = {"claude": agent_def}

            result = resume_worker_pane(sr, "resume-pane2")  # no agent param

        assert result.get("resumed") is True
        assert result.get("agent") == "claude"
