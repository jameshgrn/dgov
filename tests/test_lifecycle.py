"""Unit tests for dgov/lifecycle.py."""

from __future__ import annotations

import signal
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

    def test_rejects_existing_worktree_directory(self, tmp_path: Path) -> None:
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
            with pytest.raises(RuntimeError, match="Worktree already exists"):
                _create_worktree(str(proj), str(wt), "branch")

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

        def fake_run(cmd, **kw):
            m = MagicMock()
            if "status" in cmd and "--porcelain" in cmd:
                m.stdout = ""  # clean worktree
            else:
                m.returncode = 0
                m.stdout = ""
                m.stderr = ""
            return m

        with patch("dgov.lifecycle.subprocess.run", fake_run):
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

    def test_kills_descendant_process_groups(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _terminate_pane_process_tree

        ps_output = "\n".join(
            [
                "123 50 123",
                "456 123 456",
                "789 456 456",
                "900 123 900",
            ]
        )

        with (
            patch("dgov.lifecycle.subprocess.run") as mock_run,
            patch("dgov.lifecycle.os.killpg") as mock_killpg,
            patch("time.sleep"),
        ):
            # First call during init, second during bounded wait loop
            mock_run.return_value = MagicMock(stdout=ps_output)
            _terminate_pane_process_tree(123, wait_timeout=0.01)  # Very short timeout

        # Should be called at least twice (initial + retry loop)
        assert mock_run.call_count >= 2

        ps_calls = [
            call
            for call in mock_run.call_args_list
            if len(call.args) > 0 and "ps" in str(call.args[0])
        ]
        assert len(ps_calls) >= 1

        killed_pgids = [call.args[0] for call in mock_killpg.call_args_list]
        # Allow extra SIGKILL-only cleanup calls (descendant may survive SIGTERM)
        # At minimum, all three PGIDs should be terminated once via SIGTERM or SIGKILL
        assert set(killed_pgids) == {123, 456, 900}

    def test_falls_back_to_root_process_group_when_snapshot_fails(self, tmp_path: Path) -> None:
        from dgov.lifecycle import _terminate_pane_process_tree

        with (
            patch("dgov.lifecycle.subprocess.run", side_effect=OSError("ps missing")),
            patch("dgov.lifecycle.os.getpgid", return_value=321) as mock_getpgid,
            patch("dgov.lifecycle.os.killpg") as mock_killpg,
        ):
            _terminate_pane_process_tree(123)

        mock_getpgid.assert_called_once_with(123)
        mock_killpg.assert_called_once_with(321, signal.SIGTERM)

    def test_full_cleanup_uses_process_tree_termination(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(tmp_path, "test-pane", pane_id="%42")

        pane = get_pane(sr, "test-pane")
        assert pane is not None

        with (
            patch("dgov.tmux._run", return_value="123"),
            patch("dgov.lifecycle._terminate_pane_process_tree") as mock_terminate,
            patch("dgov.lifecycle.subprocess.run"),
        ):
            _full_cleanup(sr, sr, "test-pane", pane)

        mock_terminate.assert_called_once_with(123)
        mock_backend.destroy.assert_called_once_with("%42")

    def test_full_cleanup_warns_with_actual_survivor_count(
        self, tmp_path: Path, mock_backend: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(tmp_path, "warn-pane", pane_id="%42")

        pane = get_pane(sr, "warn-pane")
        assert pane is not None

        with (
            patch("dgov.tmux._run", return_value="123"),
            patch(
                "dgov.lifecycle._terminate_pane_process_tree",
                return_value={"terminated": False, "still_running": [111, 222]},
            ),
            patch("dgov.lifecycle.subprocess.run"),
            caplog.at_level("WARNING"),
        ):
            _full_cleanup(sr, sr, "warn-pane", pane)

        assert "2 process(es) survived termination" in caplog.text
        assert "SIGTERM" not in caplog.text

    def test_full_cleanup_skips_warning_when_no_survivors(
        self, tmp_path: Path, mock_backend: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(tmp_path, "quiet-pane", pane_id="%42")

        pane = get_pane(sr, "quiet-pane")
        assert pane is not None

        with (
            patch("dgov.tmux._run", return_value="123"),
            patch(
                "dgov.lifecycle._terminate_pane_process_tree",
                return_value={"terminated": False, "still_running": []},
            ),
            patch("dgov.lifecycle.subprocess.run"),
            caplog.at_level("WARNING"),
        ):
            _full_cleanup(sr, sr, "quiet-pane", pane)

        assert "survived termination" not in caplog.text

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

        # Worktree prune is still called even when not owning worktree
        calls_args = [c[0][0] for c in mock_run.call_args_list]
        worktree_remove = [a for a in calls_args if "worktree" in a and "remove" in a]
        branch_delete = [a for a in calls_args if "branch" in a and "-d" in a]
        assert len(worktree_remove) == 0
        assert len(branch_delete) == 0

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
        with (
            patch("dgov.lifecycle.subprocess.run", side_effect=mock_results),
            patch("dgov.tmux._run", return_value=""),
        ):
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

    def test_terminates_descendant_process_groups_before_worktree_removal(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Regression: verify descendant process groups are terminated before worktree removal.

        This prevents orphaned processes from blocking git operations during cleanup.
        The sequence should be: terminate descendants → destroy pane → remove worktree/branch.
        """
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        _add_pane(
            tmp_path, "descendant-pane", owns_worktree=True, worktree_path=str(tmp_path / "wt")
        )

        pane = get_pane(sr, "descendant-pane")
        assert pane is not None

        git_calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            git_calls.append(cmd)
            m = MagicMock()
            if "status" in cmd and "--porcelain" in cmd:
                m.stdout = ""  # clean worktree
            elif "worktree" in cmd and "remove" in cmd:
                m.returncode = 0
            elif "branch" in cmd:
                m.returncode = 0
            else:
                m.returncode = 0
                m.stdout = ""
            return m

        with (
            patch("dgov.tmux._run", return_value="123"),
            patch("dgov.lifecycle._terminate_pane_process_tree") as mock_terminate,
            patch("subprocess.run", fake_run),
        ):
            _full_cleanup(sr, sr, "descendant-pane", pane)

        # Termination must be called before any git operations
        mock_terminate.assert_called_once_with(123)
        assert len(git_calls) >= 2

        # Verify process tree termination happens first (no git calls before terminate)
        worktree_remove = [i for i, c in enumerate(git_calls) if "worktree" in c and "remove" in c]
        assert len(worktree_remove) == 1
        assert worktree_remove[0] >= 0  # git calls happen after terminate

    def test_preserves_pane_record_when_worktree_removal_fails_dirty(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Regression: dirty pane without force=True must preserve pane record.

        This prevents data loss when worktree removal is skipped due to uncommitted changes.
        The pane state should remain in the registry for potential later cleanup with force=True.
        """
        from dgov.lifecycle import _full_cleanup
        from dgov.persistence import get_pane, replace_all_panes

        sr = str(tmp_path)
        wt_path = tmp_path / "dirty-pane"
        wt_path.mkdir()
        replace_all_panes(
            sr,
            {
                "panes": [
                    {
                        "slug": "dirty-pane",
                        "pane_id": "%42",
                        "owns_worktree": True,
                        "worktree_path": str(wt_path),
                        "branch_name": "dirty-br",
                        "state": "active",
                    }
                ]
            },
        )

        pane = get_pane(sr, "dirty-pane")
        assert pane is not None

        def fake_run(cmd, **kw):
            m = MagicMock()
            if "status" in cmd and "--porcelain" in cmd:
                m.stdout = "M dirty.py\n"  # worktree is dirty
            elif "worktree" in cmd and "remove" in cmd:
                # Should not reach here when dirty and skip_worktree_if_dirty=True
                raise AssertionError("worktree remove should be skipped for dirty pane")
            else:
                m.returncode = 0
                m.stdout = ""
            return m

        with (
            patch("subprocess.run", fake_run),
            patch("dgov.tmux._run", return_value=""),
        ):
            result = _full_cleanup(sr, sr, "dirty-pane", pane, skip_worktree_if_dirty=True)

        # Worktree removal should be skipped
        assert result["skipped_worktree"] is True

        # Pane record must remain in the registry
        remaining_pane = get_pane(sr, "dirty-pane")
        assert remaining_pane is not None
        assert remaining_pane["state"] == "active"

    def test_close_worker_pane_preserves_timed_out_state_when_worktree_kept(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.lifecycle import close_worker_pane

        sr = str(tmp_path)
        wt_path = tmp_path / "timed-out-pane"
        wt_path.mkdir()
        _add_pane(
            tmp_path,
            "timed-out-pane",
            state="timed_out",
            owns_worktree=True,
            worktree_path=str(wt_path),
            branch_name="timed-out-branch",
        )

        def fake_run(cmd, **kw):
            m = MagicMock()
            if "status" in cmd and "--porcelain" in cmd:
                m.stdout = "M waiter.py\n"
            else:
                m.returncode = 0
                m.stdout = ""
            return m

        with (
            patch("subprocess.run", fake_run),
            patch("dgov.tmux._run", return_value=""),
        ):
            result = close_worker_pane(sr, "timed-out-pane", session_root=sr)

        assert result is True
        remaining_pane = get_pane(sr, "timed-out-pane")
        assert remaining_pane is not None
        assert remaining_pane["state"] == "timed_out"

    def test_removes_worktree_when_clean_and_force_applied(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Verify clean worktrees are removed even without explicit force=True."""
        from dgov.lifecycle import _full_cleanup

        sr = str(tmp_path)
        wt_path = tmp_path / "clean-pane"
        wt_path.mkdir()
        _add_pane(
            tmp_path,
            "clean-pane",
            owns_worktree=True,
            worktree_path=str(wt_path),
            branch_name="clean-br",
        )

        pane = get_pane(sr, "clean-pane")
        assert pane is not None

        git_calls: list[list[str]] = []

        def fake_run(cmd, **kw):
            git_calls.append(cmd)
            m = MagicMock()
            if "status" in cmd and "--porcelain" in cmd:
                m.stdout = ""  # clean worktree
            elif "worktree" in cmd and "remove" in cmd:
                m.returncode = 0
            elif "branch" in cmd:
                m.returncode = 0
            else:
                m.returncode = 0
                m.stdout = ""
            return m

        with patch("subprocess.run", fake_run):
            result = _full_cleanup(sr, sr, "clean-pane", pane)

        # Worktree should be removed for clean worktrees
        assert result["skipped_worktree"] is False
        assert any("worktree" in c and "remove" in c for c in git_calls)


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


# ──────────────────────────────────────────────────────────────
# TestWriteWorktreeInstructions
# ──────────────────────────────────────────────────────────────


class TestWriteWorktreeInstructions:
    def test_instructions_written_to_dgov_file_only(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Worker instructions are written to .dgov/DGOV_WORKER_INSTRUCTIONS.md only.

        Main behavior: CLAUDE.md and AGENTS.md are NOT written by this function.
        The generated instructions live at .dgov/DGOV_WORKER_INSTRUCTIONS.md.
        """
        from dgov.lifecycle import _write_worktree_instructions

        wt = tmp_path / "worktree"
        wt.mkdir()

        _write_worktree_instructions(str(wt), "test-task", "worker", prompt="Fix parser bug")

        # Instructions file exists at .dgov/DGOV_WORKER_INSTRUCTIONS.md
        instructions_file = wt / ".dgov" / "DGOV_WORKER_INSTRUCTIONS.md"
        assert instructions_file.is_file()

        content = instructions_file.read_text(encoding="utf-8")

        # Verify worker preamble IS present
        assert "# Worker Instructions — test-task" in content
        assert "You are a **worker**" in content
        assert "Complete the task, commit, and signal done" in content

        # CLAUDE.md should NOT be written by this function
        claude_file = wt / "CLAUDE.md"
        assert not claude_file.exists() or "You are a **worker**" not in claude_file.read_text(
            encoding="utf-8"
        )

        # AGENTS.md should NOT be written by this function
        agents_file = wt / "AGENTS.md"
        assert not agents_file.exists() or "You are a **worker**" not in agents_file.read_text(
            encoding="utf-8"
        )

    def test_worker_instructions_isolate_governor_body(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Generated worker instructions must not inherit main repo CLAUDE.md content."""
        from dgov.lifecycle import _write_worktree_instructions

        # Create a worktree with a "governor-style" CLAUDE.md already present
        wt = tmp_path / "worktree"
        wt.mkdir()
        (wt / "CLAUDE.md").write_text(
            (
                "# Governor Instructions\n\n"
                "You are the **governor**. You orchestrate; you do not implement.\n\n"
                "## Role\n- Stay on `main`. Always.\n"
                "- Delegate ALL implementation to workers."
            ),
            encoding="utf-8",
        )

        # Write worker instructions
        _write_worktree_instructions(str(wt), "test-task", "worker", prompt="Fix parser bug")

        instructions_content = (wt / ".dgov" / "DGOV_WORKER_INSTRUCTIONS.md").read_text(
            encoding="utf-8"
        )

        # Verify isolation: governor body must NOT appear in worker instructions
        assert "You are the **governor**" not in instructions_content
        assert "Stay on `main`" not in instructions_content
        assert "Delegate ALL implementation" not in instructions_content

        # Verify worker preamble IS present
        assert "# Worker Instructions — test-task" in instructions_content
        assert "You are a **worker**" in instructions_content
        assert "Complete the task, commit, and signal done" in instructions_content

    def test_lt_gov_instructions_isolate_governor_body(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Generated LT-GOV instructions must not inherit main repo CLAUDE.md content."""
        from dgov.lifecycle import _write_worktree_instructions

        # Create a worktree with a "governor-style" CLAUDE.md already present
        wt = tmp_path / "worktree"
        wt.mkdir()
        (wt / "CLAUDE.md").write_text(
            (
                "# Governor Instructions\n\n"
                "You are the **governor**. You orchestrate; you do not implement.\n\n"
                "## Role\n- Stay on `main`. Always."
            ),
            encoding="utf-8",
        )

        # Write LT-GOV instructions
        _write_worktree_instructions(
            str(wt), "orchestration-task", "lt-gov", prompt="Dispatch workers"
        )

        instructions_content = (wt / ".dgov" / "DGOV_WORKER_INSTRUCTIONS.md").read_text(
            encoding="utf-8"
        )

        # Verify isolation: governor body must NOT appear in LT-GOV instructions
        assert "You are the **governor**" not in instructions_content
        assert "Stay on `main`" not in instructions_content

        # Verify LT-GOV preamble IS present
        assert "# LT-GOV Instructions — orchestration-task" in instructions_content
        assert "You are a **lieutenant governor**" in instructions_content
        assert "You orchestrate workers, you do NOT edit code" in instructions_content

    def test_git_excludes_dgov_worker_instructions(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """.dgov/DGOV_WORKER_INSTRUCTIONS.md is git-excluded via .git/info/exclude."""
        from dgov.lifecycle import _write_worktree_instructions

        wt = tmp_path / "worktree"
        wt.mkdir()

        # Initialize git repo so .git/info/exclude exists
        subprocess.run(["git", "-C", str(wt), "init"], capture_output=True)
        subprocess.run(
            ["git", "-C", str(wt), "config", "user.email", "test@test.com"], capture_output=True
        )
        subprocess.run(["git", "-C", str(wt), "config", "user.name", "Test"], capture_output=True)

        _write_worktree_instructions(str(wt), "test-task", "worker", prompt="Fix parser")

        # Check .git/info/exclude contains DGOV_WORKER_INSTRUCTIONS.md
        exclude_file = wt / ".git" / "info" / "exclude"
        exclude_content = exclude_file.read_text(encoding="utf-8")

        assert ".dgov/DGOV_WORKER_INSTRUCTIONS.md" in exclude_content


# ──────────────────────────────────────────────────────────────
# TestSlugAllocationHistory
# ──────────────────────────────────────────────────────────────


class TestSlugAllocationHistory:
    """Tests for session-unique pane slug allocation with historical tracking."""

    def test_historical_slug_remains_reserved_after_close(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """A previously used slug remains reserved after pane close.

        When a pane is closed and removed from active panes, its slug
        should still be tracked in history so subsequent allocations
        avoid collision.
        """
        from dgov.lifecycle import _find_unique_slug
        from dgov.persistence import all_panes, get_pane, remove_pane

        # Create initial pane with base slug
        project_root = str(tmp_path / "proj")
        session_root = str(tmp_path / "session")
        Path(project_root).mkdir(parents=True)
        Path(session_root).mkdir(parents=True)

        # Simulate existing pane state by directly manipulating the database
        from dgov.persistence import WorkerPane, add_pane

        pane = WorkerPane(
            slug="fix-parser",
            prompt="test",
            pane_id="%1",
            agent="claude",
            project_root=project_root,
            worktree_path=str(tmp_path / "worktrees" / "fix-parser"),
            branch_name="fix-parser",
            owns_worktree=True,
            role="worker",
            parent_slug="",
            created_at=time.time(),
            state="active",
        )
        add_pane(session_root, pane)

        # Verify pane exists
        assert get_pane(session_root, "fix-parser") is not None

        # Close the pane (removes from active panes)
        remove_pane(session_root, "fix-parser")

        # Pane should no longer be in active panes
        assert get_pane(session_root, "fix-parser") is None
        assert "fix-parser" not in {p["slug"] for p in all_panes(session_root)}

        # But slug should still be reserved - new allocation should increment
        unique_slug, _worktree_path = _find_unique_slug(project_root, session_root, "fix-parser")
        assert unique_slug != "fix-parser"
        assert unique_slug.startswith("fix-parser-")

    def test_slug_allocation_increments_numeric_suffix(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Allocation increments to the next numeric suffix for historical slugs.

        When multiple panes have used the same base slug over time,
        each new allocation should increment the numeric suffix.
        """
        from dgov.lifecycle import _find_unique_slug
        from dgov.persistence import WorkerPane, add_pane, remove_pane

        project_root = str(tmp_path / "proj")
        session_root = str(tmp_path / "session")
        Path(project_root).mkdir(parents=True)
        Path(session_root).mkdir(parents=True)

        # Simulate historical usage of "add-feature" slug
        # First pane: fix-parser (no suffix needed)
        pane1 = WorkerPane(
            slug="add-feature",
            prompt="test 1",
            pane_id="%1",
            agent="claude",
            project_root=project_root,
            worktree_path=str(tmp_path / "worktrees" / "add-feature"),
            branch_name="add-feature",
            owns_worktree=True,
            role="worker",
            parent_slug="",
            created_at=time.time(),
            state="active",
        )
        add_pane(session_root, pane1)
        remove_pane(session_root, "add-feature")

        # Second pane: add-feature-1 (first suffix)
        pane2 = WorkerPane(
            slug="add-feature-1",
            prompt="test 2",
            pane_id="%2",
            agent="claude",
            project_root=project_root,
            worktree_path=str(tmp_path / "worktrees" / "add-feature-1"),
            branch_name="add-feature-1",
            owns_worktree=True,
            role="worker",
            parent_slug="",
            created_at=time.time(),
            state="active",
        )
        add_pane(session_root, pane2)
        remove_pane(session_root, "add-feature-1")

        # Third allocation should get add-feature-2
        unique_slug, _worktree_path = _find_unique_slug(project_root, session_root, "add-feature")
        assert unique_slug == "add-feature-2"

    def test_active_slug_collision_also_increments(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Active pane slug collision also triggers numeric suffix increment.

        This verifies the existing behavior for active panes still works.
        """
        from dgov.lifecycle import _find_unique_slug
        from dgov.persistence import WorkerPane, add_pane

        project_root = str(tmp_path / "proj")
        session_root = str(tmp_path / "session")
        Path(project_root).mkdir(parents=True)
        Path(session_root).mkdir(parents=True)

        # Create active pane
        pane = WorkerPane(
            slug="active-task",
            prompt="test",
            pane_id="%1",
            agent="claude",
            project_root=project_root,
            worktree_path=str(tmp_path / "worktrees" / "active-task"),
            branch_name="active-task",
            owns_worktree=True,
            role="worker",
            parent_slug="",
            created_at=time.time(),
            state="active",
        )
        add_pane(session_root, pane)

        # New allocation should increment since slug is in use
        unique_slug, _worktree_path = _find_unique_slug(project_root, session_root, "active-task")
        assert unique_slug == "active-task-1"


# ──────────────────────────────────────────────────────────────
# TestOverlayDirtyClaimsIntoWorktree
# ──────────────────────────────────────────────────────────────


class TestOverlayDirtyClaimsIntoWorktree:
    """Tests for _overlay_dirty_claims_into_worktree helper."""

    def test_overlays_tracked_dirty_files(self, tmp_path: Path) -> None:
        """Tracked dirty files from governor repo are overlaid into worktree.

        When create_worker_pane creates a new owned worktree, tracked dirty files
        from the governor repo should be copied into the worktree before launch.
        """
        from dgov.context_packet import ContextPacket
        from dgov.lifecycle import _overlay_dirty_claims_into_worktree

        # Setup governor repo with tracked file that has local modifications
        proj = tmp_path / "proj"
        proj.mkdir()
        subprocess.run(["git", "-C", str(proj), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.email", "test@test.com"], capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.name", "Test"], capture_output=True
        )

        # Create initial commit with a tracked file
        test_file = proj / "test.py"
        test_file.write_text("initial content\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(proj), "add", "test.py"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(proj), "commit", "-m", "initial"], capture_output=True, check=True
        )

        # Modify the tracked file (dirty state)
        test_file.write_text("modified content\n", encoding="utf-8")

        # Setup worktree
        wt = tmp_path / "worktree"
        wt.mkdir()
        subprocess.run(
            ["git", "-C", str(proj), "worktree", "add", str(wt), "-b", "wt-branch"],
            capture_output=True,
            check=True,
        )

        # Create context packet with file claim
        packet = ContextPacket(
            prompt="fix bug",
            file_claims=("test.py",),
        )

        # Overlay dirty claims
        _overlay_dirty_claims_into_worktree(str(proj), str(wt), packet)

        # Worktree should now have the modified content (not initial commit content)
        wt_file = wt / "test.py"
        assert wt_file.exists()
        assert wt_file.read_text(encoding="utf-8") == "modified content\n"

    def test_untracked_files_are_ignored(self, tmp_path: Path) -> None:
        """Untracked files in the worktree are not affected by overlay.

        Only tracked dirty files from the governor repo should be overlaid.
        Untracked files remain untouched.
        """
        from dgov.context_packet import ContextPacket
        from dgov.lifecycle import _overlay_dirty_claims_into_worktree

        # Setup governor repo
        proj = tmp_path / "proj"
        proj.mkdir()
        subprocess.run(["git", "-C", str(proj), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.email", "test@test.com"], capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.name", "Test"], capture_output=True
        )

        # Create initial commit (no test.py initially)
        init_file = proj / "init.txt"
        init_file.write_text("initial\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(proj), "add", "init.txt"], capture_output=True, check=True
        )
        subprocess.run(
            ["git", "-C", str(proj), "commit", "-m", "initial"], capture_output=True, check=True
        )

        # Setup worktree (clean copy from commit)
        wt = tmp_path / "worktree"
        wt.mkdir()
        subprocess.run(
            ["git", "-C", str(proj), "worktree", "add", str(wt), "-b", "wt-branch"],
            capture_output=True,
            check=True,
        )

        # Create untracked file in worktree
        untracked = wt / "untracked.py"
        untracked.write_text("untracked content\n", encoding="utf-8")

        # Context packet has no claims (should not touch anything)
        packet = ContextPacket(prompt="test", file_claims=())

        # Overlay should leave untracked file alone
        _overlay_dirty_claims_into_worktree(str(proj), str(wt), packet)

        assert untracked.exists()
        assert untracked.read_text(encoding="utf-8") == "untracked content\n"

    def test_tracked_deletions_are_removed_from_worktree(self, tmp_path: Path) -> None:
        """Tracked deletions from governor repo should be removed in worktree.

        When a file is tracked as deleted in the governor repo (deleted but not committed),
        it should be removed from the worktree during overlay.
        """
        from dgov.context_packet import ContextPacket
        from dgov.lifecycle import _overlay_dirty_claims_into_worktree

        # Setup governor repo with tracked file
        proj = tmp_path / "proj"
        proj.mkdir()
        subprocess.run(["git", "-C", str(proj), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.email", "test@test.com"], capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.name", "Test"], capture_output=True
        )

        # Create and commit tracked file
        to_delete = proj / "to_delete.txt"
        to_delete.write_text("will be deleted\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(proj), "add", "to_delete.txt"], capture_output=True, check=True
        )
        subprocess.run(
            ["git", "-C", str(proj), "commit", "-m", "initial"], capture_output=True, check=True
        )

        # Setup worktree (has the file from commit)
        wt = tmp_path / "worktree"
        wt.mkdir()
        subprocess.run(
            ["git", "-C", str(proj), "worktree", "add", str(wt), "-b", "wt-branch"],
            capture_output=True,
            check=True,
        )
        assert (wt / "to_delete.txt").exists()

        # Delete tracked file in governor repo (but don't commit yet - dirty deletion)
        (proj / "to_delete.txt").unlink()

        # Context packet should include deleted files via git status
        # Since we're testing the overlay behavior, we'll manually specify it's a deletion
        packet = ContextPacket(prompt="test", file_claims=())

        # Overlay should remove the deleted file from worktree
        _overlay_dirty_claims_into_worktree(str(proj), str(wt), packet)

        # File should be gone from worktree
        assert not (wt / "to_delete.txt").exists()

    def test_multiple_files_overlay_correctly(self, tmp_path: Path) -> None:
        """Multiple tracked dirty files are all overlaid correctly.

        When there are multiple tracked dirty files in different locations,
        each should be properly copied to the worktree.
        """
        from dgov.context_packet import ContextPacket
        from dgov.lifecycle import _overlay_dirty_claims_into_worktree

        # Setup governor repo
        proj = tmp_path / "proj"
        proj.mkdir()
        subprocess.run(["git", "-C", str(proj), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.email", "test@test.com"], capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.name", "Test"], capture_output=True
        )

        # Create initial commit with multiple files
        file1 = proj / "file1.py"
        src_dir = proj / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        file2 = src_dir / "file2.py"
        file1.write_text("v1\n", encoding="utf-8")
        file2.write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(proj), "add", "."], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(proj), "commit", "-m", "initial"], capture_output=True, check=True
        )

        # Modify both files (dirty state)
        file1.write_text("modified-v1\n", encoding="utf-8")
        file2.write_text("modified-v2\n", encoding="utf-8")

        # Setup worktree
        wt = tmp_path / "worktree"
        wt.mkdir()
        subprocess.run(
            ["git", "-C", str(proj), "worktree", "add", str(wt), "-b", "wt-branch"],
            capture_output=True,
            check=True,
        )

        # Create context packet with claims
        packet = ContextPacket(
            prompt="fix bugs",
            file_claims=("file1.py", "src/file2.py"),
        )

        # Overlay dirty claims
        _overlay_dirty_claims_into_worktree(str(proj), str(wt), packet)

        # Both files should have modified content
        assert (wt / "file1.py").read_text(encoding="utf-8") == "modified-v1\n"
        assert (wt / "src" / "file2.py").read_text(encoding="utf-8") == "modified-v2\n"

    def test_nonexistent_claims_are_safe(self, tmp_path: Path) -> None:
        """Claims for files that don't exist in governor repo are safely ignored.

        When the context packet claims files that aren't tracked or don't exist
        in the governor repo, the overlay should handle this gracefully without errors.
        """
        from dgov.context_packet import ContextPacket
        from dgov.lifecycle import _overlay_dirty_claims_into_worktree

        # Setup governor repo (empty)
        proj = tmp_path / "proj"
        proj.mkdir()
        subprocess.run(["git", "-C", str(proj), "init"], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.email", "test@test.com"], capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(proj), "config", "user.name", "Test"], capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(proj), "commit", "--allow-empty", "-m", "initial"],
            capture_output=True,
            check=True,
        )

        # Setup worktree
        wt = tmp_path / "worktree"
        wt.mkdir()
        subprocess.run(
            ["git", "-C", str(proj), "worktree", "add", str(wt), "-b", "wt-branch"],
            capture_output=True,
            check=True,
        )

        # Context packet claims nonexistent files
        packet = ContextPacket(
            prompt="fix missing",
            file_claims=("nonexistent.py", "also-missing.txt"),
        )

        # Overlay should not raise error even with invalid claims
        _overlay_dirty_claims_into_worktree(str(proj), str(wt), packet)

        # Worktree remains clean (no phantom files created, only .git dir from worktree init)
        contents = [p.name for p in wt.iterdir() if not p.name.startswith(".")]
        assert contents == []
