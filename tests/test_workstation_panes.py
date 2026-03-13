"""Unit tests for dgov.panes — state management and helper functions."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from dgov.panes import (
    WorkerPane,
    _add_pane,
    _all_panes,
    _generate_slug,
    _get_pane,
    _has_new_commits,
    _is_done,
    _read_state,
    _remove_pane,
    _state_path,
    _trigger_hook,
    _write_state,
    capture_worker_output,
    classify_task,
    list_worker_panes,
    prune_stale_panes,
)

pytestmark = pytest.mark.unit


class TestWorkerPane:
    def test_defaults(self) -> None:
        wp = WorkerPane(
            slug="fix-bug",
            prompt="Fix the bug",
            pane_id="%5",
            agent="pi",
            project_root="/repo",
            worktree_path="/repo/.dgov/worktrees/fix-bug",
            branch_name="fix-bug",
        )
        assert wp.owns_worktree is True
        assert wp.base_sha == ""
        assert wp.created_at > 0

    def test_custom_fields(self) -> None:
        wp = WorkerPane(
            slug="fix",
            prompt="Fix",
            pane_id="%1",
            agent="claude",
            project_root="/repo",
            worktree_path="/wt",
            branch_name="br",
            owns_worktree=False,
            base_sha="abc123",
        )
        assert wp.owns_worktree is False
        assert wp.base_sha == "abc123"


# ---------------------------------------------------------------------------
# State file helpers
# ---------------------------------------------------------------------------


class TestStatePath:
    def test_returns_correct_path(self) -> None:
        result = _state_path("/tmp/session")
        assert result == Path("/tmp/session/.dgov/state.json")


class TestReadState:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        state = _read_state(str(tmp_path))
        assert state == {"panes": []}

    def test_reads_existing_file(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".dgov"
        state_dir.mkdir()
        (state_dir / "state.json").write_text(json.dumps({"panes": [{"slug": "test"}]}))
        state = _read_state(str(tmp_path))
        assert len(state["panes"]) == 1
        assert state["panes"][0]["slug"] == "test"


class TestWriteState:
    def test_creates_dirs_and_writes(self, tmp_path: Path) -> None:
        _write_state(str(tmp_path), {"panes": [{"slug": "a"}]})
        path = tmp_path / ".dgov" / "state.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["panes"][0]["slug"] == "a"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        _write_state(str(tmp_path), {"panes": [{"slug": "old"}]})
        _write_state(str(tmp_path), {"panes": [{"slug": "new"}]})
        data = json.loads((tmp_path / ".dgov" / "state.json").read_text())
        assert data["panes"][0]["slug"] == "new"


class TestAddPane:
    def test_adds_to_empty_state(self, tmp_path: Path) -> None:
        wp = WorkerPane(
            slug="test",
            prompt="Do something",
            pane_id="%1",
            agent="pi",
            project_root="/repo",
            worktree_path="/wt",
            branch_name="br",
        )
        _add_pane(str(tmp_path), wp)
        state = _read_state(str(tmp_path))
        assert len(state["panes"]) == 1
        assert state["panes"][0]["slug"] == "test"

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        wp1 = WorkerPane(
            slug="a",
            prompt="A",
            pane_id="%1",
            agent="pi",
            project_root="/r",
            worktree_path="/w1",
            branch_name="a",
        )
        wp2 = WorkerPane(
            slug="b",
            prompt="B",
            pane_id="%2",
            agent="pi",
            project_root="/r",
            worktree_path="/w2",
            branch_name="b",
        )
        _add_pane(str(tmp_path), wp1)
        _add_pane(str(tmp_path), wp2)
        state = _read_state(str(tmp_path))
        assert len(state["panes"]) == 2

    def test_upserts_duplicate_slug(self, tmp_path: Path) -> None:
        """Adding a pane with an existing slug should replace, not duplicate."""
        wp1 = WorkerPane(
            slug="gov",
            prompt="Old",
            pane_id="%1",
            agent="pi",
            project_root="/r",
            worktree_path="/w1",
            branch_name="gov",
        )
        wp2 = WorkerPane(
            slug="gov",
            prompt="New",
            pane_id="%2",
            agent="claude",
            project_root="/r",
            worktree_path="/w2",
            branch_name="gov",
        )
        _add_pane(str(tmp_path), wp1)
        _add_pane(str(tmp_path), wp2)
        state = _read_state(str(tmp_path))
        assert len(state["panes"]) == 1
        assert state["panes"][0]["pane_id"] == "%2"
        assert state["panes"][0]["prompt"] == "New"


class TestRemovePane:
    def test_removes_by_slug(self, tmp_path: Path) -> None:
        _write_state(
            str(tmp_path),
            {
                "panes": [
                    {"slug": "keep", "pane_id": "%1"},
                    {"slug": "remove", "pane_id": "%2"},
                ]
            },
        )
        _remove_pane(str(tmp_path), "remove")
        state = _read_state(str(tmp_path))
        assert len(state["panes"]) == 1
        assert state["panes"][0]["slug"] == "keep"

    def test_remove_nonexistent_noop(self, tmp_path: Path) -> None:
        _write_state(str(tmp_path), {"panes": [{"slug": "keep"}]})
        _remove_pane(str(tmp_path), "nope")
        assert len(_read_state(str(tmp_path))["panes"]) == 1


class TestGetPane:
    def test_found(self, tmp_path: Path) -> None:
        _write_state(str(tmp_path), {"panes": [{"slug": "target", "agent": "pi"}]})
        result = _get_pane(str(tmp_path), "target")
        assert result is not None
        assert result["agent"] == "pi"

    def test_not_found(self, tmp_path: Path) -> None:
        _write_state(str(tmp_path), {"panes": []})
        assert _get_pane(str(tmp_path), "nope") is None


class TestAllPanes:
    def test_returns_all(self, tmp_path: Path) -> None:
        _write_state(str(tmp_path), {"panes": [{"slug": "a"}, {"slug": "b"}]})
        result = _all_panes(str(tmp_path))
        assert len(result) == 2

    def test_empty(self, tmp_path: Path) -> None:
        assert _all_panes(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# classify_task / _generate_slug fallbacks
# ---------------------------------------------------------------------------


class TestClassifyTask:
    def test_fallback_to_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.panes._qwen_4b_request",
            lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("no qwen")),
        )
        assert classify_task("fix the lint error") == "claude"

    def test_returns_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.panes._qwen_4b_request",
            lambda *a, **kw: {"choices": [{"message": {"content": "claude"}}]},
        )
        assert classify_task("debug flaky test") == "claude"

    def test_returns_pi_on_pi_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dgov.panes._qwen_4b_request",
            lambda *a, **kw: {"choices": [{"message": {"content": "pi"}}]},
        )
        assert classify_task("format the file") == "pi"


class TestGenerateSlug:
    def test_fallback_strips_stopwords(self) -> None:
        # Force fallback by making _qwen_4b_request raise
        with patch("dgov.panes._qwen_4b_request", side_effect=ConnectionError):
            slug = _generate_slug("fix the broken test in scheduler")
        assert "the" not in slug.split("-")
        assert "in" not in slug.split("-")
        assert len(slug) > 0

    def test_fallback_limits_words(self) -> None:
        with patch("dgov.panes._qwen_4b_request", side_effect=ConnectionError):
            slug = _generate_slug("a b c d e f g h", max_words=3)
        assert len(slug.split("-")) <= 3

    def test_qwen_success(self) -> None:
        with patch(
            "dgov.panes._qwen_4b_request",
            return_value={"choices": [{"message": {"content": "fix-lint-errors"}}]},
        ):
            slug = _generate_slug("Fix all the lint errors")
        assert slug == "fix-lint-errors"

    def test_qwen_returns_too_long_fallback(self) -> None:
        with patch(
            "dgov.panes._qwen_4b_request",
            return_value={"choices": [{"message": {"content": "a" * 60}}]},
        ):
            slug = _generate_slug("fix the bug")
        # Should fall back to local extraction since slug > 50 chars
        assert len(slug) <= 50


# ---------------------------------------------------------------------------
# _has_new_commits
# ---------------------------------------------------------------------------


class TestHasNewCommits:
    def test_empty_base_sha_returns_false(self) -> None:
        assert _has_new_commits("/repo", "branch", "") is False

    def test_has_commits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = "abc123 commit message\n"
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        assert _has_new_commits("/repo", "branch", "base123") is True

    def test_no_commits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        assert _has_new_commits("/repo", "branch", "base123") is False

    def test_git_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock = MagicMock()
        mock.returncode = 128
        mock.stdout = ""
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        assert _has_new_commits("/repo", "branch", "base123") is False


# ---------------------------------------------------------------------------
# _is_done
# ---------------------------------------------------------------------------


class TestIsDone:
    def test_done_signal_file(self, tmp_path: Path) -> None:
        done_dir = tmp_path / ".dgov" / "done"
        done_dir.mkdir(parents=True)
        (done_dir / "test-slug").touch()
        assert _is_done(str(tmp_path), "test-slug") is True

    def test_no_pane_record_no_signal(self, tmp_path: Path) -> None:
        assert _is_done(str(tmp_path), "test-slug") is False

    def test_new_commits_signal(self, tmp_path: Path) -> None:
        record = {
            "project_root": "/repo",
            "branch_name": "br",
            "base_sha": "abc",
            "pane_id": "%5",
        }
        with (
            patch("dgov.panes._has_new_commits", return_value=True),
        ):
            assert _is_done(str(tmp_path), "slug", pane_record=record) is True

    def test_dead_pane_signal(self, tmp_path: Path) -> None:
        record = {
            "project_root": "/repo",
            "branch_name": "br",
            "base_sha": "abc",
            "pane_id": "%5",
        }
        with (
            patch("dgov.panes._has_new_commits", return_value=False),
            patch("dgov.panes.tmux.pane_exists", return_value=False),
        ):
            assert _is_done(str(tmp_path), "slug", pane_record=record) is True

    def test_alive_pane_no_commits(self, tmp_path: Path) -> None:
        record = {
            "project_root": "/repo",
            "branch_name": "br",
            "base_sha": "abc",
            "pane_id": "%5",
        }
        with (
            patch("dgov.panes._has_new_commits", return_value=False),
            patch("dgov.panes.tmux.pane_exists", return_value=True),
        ):
            assert _is_done(str(tmp_path), "slug", pane_record=record) is False


# ---------------------------------------------------------------------------
# _trigger_hook
# ---------------------------------------------------------------------------


class TestTriggerHook:
    def test_runs_executable_hook(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        hook_dir = tmp_path / ".dmux-hooks"
        hook_dir.mkdir(parents=True)
        hook = hook_dir / "post-merge"
        hook.write_text("#!/bin/bash\necho done")
        hook.chmod(0o755)

        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            mock = MagicMock()
            mock.returncode = 0
            return mock

        monkeypatch.setattr("subprocess.run", fake_run)
        _trigger_hook("post-merge", str(tmp_path), {"SLUG": "test"})
        assert "cmd" in captured

    def test_no_hook_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # No hook dirs exist — should not crash
        calls = []
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: calls.append(1))
        _trigger_hook("post-merge", str(tmp_path), {})
        assert len(calls) == 0

    def test_timeout_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import subprocess

        hook_dir = tmp_path / ".dmux-hooks"
        hook_dir.mkdir(parents=True)
        hook = hook_dir / "post-merge"
        hook.write_text("#!/bin/bash\nsleep 100")
        hook.chmod(0o755)

        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired("hook", 10)

        monkeypatch.setattr("subprocess.run", fake_run)
        # Should not raise
        _trigger_hook("post-merge", str(tmp_path), {})


# ---------------------------------------------------------------------------
# list_worker_panes
# ---------------------------------------------------------------------------


class TestListWorkerPanes:
    def test_empty_state(self, tmp_path: Path) -> None:
        result = list_worker_panes(str(tmp_path))
        assert result == []

    def test_deduplicates_by_slug_prefers_alive(self, tmp_path: Path) -> None:
        """When state has duplicate slugs, list should return one entry preferring alive."""
        _write_state(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "gov",
                        "agent": "pi",
                        "pane_id": "%1",
                        "project_root": str(tmp_path),
                        "worktree_path": "/wt",
                        "branch_name": "gov",
                        "prompt": "Old",
                    },
                    {
                        "slug": "gov",
                        "agent": "claude",
                        "pane_id": "%2",
                        "project_root": str(tmp_path),
                        "worktree_path": "/wt2",
                        "branch_name": "gov",
                        "prompt": "New",
                    },
                ]
            },
        )

        def fake_pane_exists(pid: str) -> bool:
            return pid == "%2"  # Only the second entry is alive

        with (
            patch("dgov.panes.tmux.pane_exists", side_effect=fake_pane_exists),
            patch("dgov.panes.tmux.current_command", return_value="claude"),
            patch("dgov.panes._is_done", return_value=False),
        ):
            result = list_worker_panes(str(tmp_path))
        assert len(result) == 1
        assert result[0]["slug"] == "gov"
        assert result[0]["pane_id"] == "%2"
        assert result[0]["alive"] is True

    def test_enriches_with_alive_status(self, tmp_path: Path) -> None:
        _write_state(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "test",
                        "agent": "pi",
                        "pane_id": "%5",
                        "project_root": str(tmp_path),
                        "worktree_path": "/wt",
                        "branch_name": "br",
                        "prompt": "Fix the bug",
                    }
                ]
            },
        )
        with (
            patch("dgov.panes.tmux.pane_exists", return_value=True),
            patch("dgov.panes.tmux.current_command", return_value="claude"),
            patch("dgov.panes._is_done", return_value=False),
        ):
            result = list_worker_panes(str(tmp_path))
        assert len(result) == 1
        assert result[0]["alive"] is True
        assert result[0]["current_command"] == "claude"
        assert result[0]["done"] is False


# ---------------------------------------------------------------------------
# prune_stale_panes
# ---------------------------------------------------------------------------


class TestPruneStale:
    def test_prunes_dead_pane_no_worktree(self, tmp_path: Path) -> None:
        _write_state(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "stale",
                        "pane_id": "%5",
                        "worktree_path": "/nonexistent/path",
                    }
                ]
            },
        )
        with patch("dgov.panes.tmux.pane_exists", return_value=False):
            pruned = prune_stale_panes(str(tmp_path))
        assert "stale" in pruned
        assert _all_panes(str(tmp_path)) == []

    def test_keeps_alive_pane(self, tmp_path: Path) -> None:
        _write_state(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "alive",
                        "pane_id": "%5",
                        "worktree_path": "/nonexistent",
                    }
                ]
            },
        )
        with patch("dgov.panes.tmux.pane_exists", return_value=True):
            pruned = prune_stale_panes(str(tmp_path))
        assert pruned == []
        assert len(_all_panes(str(tmp_path))) == 1

    def test_keeps_pane_with_worktree(self, tmp_path: Path) -> None:
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        _write_state(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "has-wt",
                        "pane_id": "%5",
                        "worktree_path": str(wt_dir),
                    }
                ]
            },
        )
        with patch("dgov.panes.tmux.pane_exists", return_value=False):
            pruned = prune_stale_panes(str(tmp_path))
        assert pruned == []


# ---------------------------------------------------------------------------
# capture_worker_output
# ---------------------------------------------------------------------------


class TestCaptureWorkerOutput:
    def test_missing_pane_returns_none(self, tmp_path: Path) -> None:
        assert capture_worker_output(str(tmp_path), "nonexistent") is None

    def test_dead_pane_returns_none(self, tmp_path: Path) -> None:
        _write_state(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
        with patch("dgov.panes.tmux.pane_exists", return_value=False):
            assert capture_worker_output(str(tmp_path), "test") is None

    def test_captures_output(self, tmp_path: Path) -> None:
        _write_state(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
        with (
            patch("dgov.panes.tmux.pane_exists", return_value=True),
            patch("dgov.panes.tmux.capture_pane", return_value="output here"),
        ):
            result = capture_worker_output(str(tmp_path), "test")
        assert result == "output here"


# ---------------------------------------------------------------------------
# _pick_resolver_agent
# ---------------------------------------------------------------------------


class TestPickResolverAgent:
    def test_prefers_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _pick_resolver_agent

        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        assert _pick_resolver_agent() == "claude"

    def test_falls_back_to_codex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _pick_resolver_agent

        def fake_which(name):
            return "/usr/bin/codex" if name == "codex" else None

        monkeypatch.setattr("shutil.which", fake_which)
        assert _pick_resolver_agent() == "codex"

    def test_defaults_claude_when_nothing_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _pick_resolver_agent

        monkeypatch.setattr("shutil.which", lambda name: None)
        assert _pick_resolver_agent() == "claude"


# ---------------------------------------------------------------------------
# _PROTECTED_FILES
# ---------------------------------------------------------------------------


class TestProtectedFiles:
    def test_contains_expected_files(self) -> None:
        from dgov.panes import _PROTECTED_FILES

        assert "CLAUDE.md" in _PROTECTED_FILES
        assert "CLAUDE.md.full" in _PROTECTED_FILES
        assert "THEORY.md" in _PROTECTED_FILES
        assert ".napkin.md" in _PROTECTED_FILES

    def test_is_set(self) -> None:
        from dgov.panes import _PROTECTED_FILES

        assert isinstance(_PROTECTED_FILES, set)


# ---------------------------------------------------------------------------
# close_worker_pane
# ---------------------------------------------------------------------------


class TestCloseWorkerPane:
    def test_not_found_returns_false(self, tmp_path: Path) -> None:
        from dgov.panes import close_worker_pane

        _write_state(str(tmp_path), {"panes": []})
        assert close_worker_pane(str(tmp_path), "nonexistent") is False

    def test_found_calls_cleanup(self, tmp_path: Path) -> None:
        from dgov.panes import close_worker_pane

        _write_state(
            str(tmp_path),
            {"panes": [{"slug": "test", "pane_id": "%5", "owns_worktree": False}]},
        )
        with patch("dgov.panes._full_cleanup") as mock_cleanup:
            result = close_worker_pane(str(tmp_path), "test")
        assert result is True
        mock_cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# _detect_conflicts
# ---------------------------------------------------------------------------


class TestDetectConflicts:
    def test_no_merge_base_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _detect_conflicts

        mock = MagicMock()
        mock.returncode = 1
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        assert _detect_conflicts("/repo", "branch") == []

    def test_detects_conflicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _detect_conflicts

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            m = MagicMock()
            if "merge-base" in cmd:
                m.returncode = 0
                m.stdout = "abc123"
            else:
                m.returncode = 0
                m.stdout = "changed in both 'src/main.py'\nchanged in both 'src/foo.py'\n"
            return m

        monkeypatch.setattr("subprocess.run", fake_run)
        result = _detect_conflicts("/repo", "feature")
        assert "'src/main.py'" in result or "src/main.py" in str(result)

    def test_no_conflicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _detect_conflicts

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "abc123" if "merge-base" in cmd else ""
            return m

        monkeypatch.setattr("subprocess.run", fake_run)
        assert _detect_conflicts("/repo", "branch") == []


# ---------------------------------------------------------------------------
# _commit_worktree
# ---------------------------------------------------------------------------


class TestCommitWorktree:
    def test_no_worktree_path(self) -> None:
        from dgov.panes import _commit_worktree

        result = _commit_worktree({})
        assert result == {"committed": False}

    def test_nonexistent_worktree(self, tmp_path: Path) -> None:
        from dgov.panes import _commit_worktree

        result = _commit_worktree({"worktree_path": str(tmp_path / "nope")})
        assert result == {"committed": False}

    def test_no_changes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _commit_worktree

        # status --porcelain -z returns empty
        mock = MagicMock()
        mock.stdout = b"\x00"
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        result = _commit_worktree({"worktree_path": str(tmp_path)})
        assert result == {"committed": False}


# ---------------------------------------------------------------------------
# _full_cleanup
# ---------------------------------------------------------------------------


class TestFullCleanup:
    def test_removes_state_and_dmux(self, tmp_path: Path) -> None:
        from dgov.panes import _full_cleanup

        _write_state(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
        # Create done signal
        done_dir = tmp_path / ".dgov" / "done"
        done_dir.mkdir(parents=True)
        (done_dir / "test").touch()

        pane_record = {"pane_id": "%5", "owns_worktree": False}

        with (
            patch("dgov.panes.tmux.kill_pane"),
            patch("dgov.panes.tmux.pane_exists", return_value=False),
            patch("dgov.panes.tmux.select_layout"),
        ):
            result = _full_cleanup(str(tmp_path), str(tmp_path), "test", pane_record)

        assert result["cleaned"] is True
        assert not (done_dir / "test").exists()
        assert _get_pane(str(tmp_path), "test") is None

    def test_skips_worktree_if_dirty(self, tmp_path: Path) -> None:
        from dgov.panes import _full_cleanup

        _write_state(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
        wt = tmp_path / "wt"
        wt.mkdir()

        pane_record = {
            "pane_id": "%5",
            "owns_worktree": True,
            "worktree_path": str(wt),
            "branch_name": "test-br",
        }

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            m = MagicMock()
            if "status" in cmd and "--porcelain" in cmd:
                m.stdout = "M dirty.py\n"
            m.returncode = 0
            return m

        with (
            patch("dgov.panes.tmux.kill_pane"),
            patch("dgov.panes.tmux.pane_exists", return_value=False),
            patch("dgov.panes.tmux.select_layout"),
            patch("subprocess.run", fake_run),
        ):
            result = _full_cleanup(
                str(tmp_path), str(tmp_path), "test", pane_record, skip_worktree_if_dirty=True
            )

        assert result["skipped_worktree"] is True


# ---------------------------------------------------------------------------
# merge_worker_pane_with_close
# ---------------------------------------------------------------------------


class TestMergeWorkerPaneWithClose:
    def test_error_passes_through(self, tmp_path: Path) -> None:
        from dgov.panes import merge_worker_pane_with_close

        with patch(
            "dgov.panes.merge_worker_pane",
            return_value={"error": "Pane not found: nope"},
        ):
            result = merge_worker_pane_with_close(str(tmp_path), "nope")
        assert "error" in result

    def test_successful_merge_calls_close(self, tmp_path: Path) -> None:
        from dgov.panes import merge_worker_pane_with_close

        with (
            patch(
                "dgov.panes.merge_worker_pane",
                return_value={"merged": "test", "branch": "test-br"},
            ),
            patch("dgov.panes.close_worker_pane", return_value=True) as mock_close,
        ):
            result = merge_worker_pane_with_close(str(tmp_path), "test")
        assert result["merged"] == "test"
        mock_close.assert_called_once()


# ---------------------------------------------------------------------------
# escalate_worker_pane
# ---------------------------------------------------------------------------


class TestEscalateWorkerPane:
    def test_not_found_returns_error(self, tmp_path: Path) -> None:
        from dgov.panes import escalate_worker_pane

        _write_state(str(tmp_path), {"panes": []})
        result = escalate_worker_pane(str(tmp_path), "nope")
        assert "error" in result

    def test_no_prompt_returns_error(self, tmp_path: Path) -> None:
        from dgov.panes import escalate_worker_pane

        _write_state(str(tmp_path), {"panes": [{"slug": "test", "prompt": ""}]})
        result = escalate_worker_pane(str(tmp_path), "test")
        assert "error" in result

    def test_escalation_calls_close_and_create(self, tmp_path: Path) -> None:
        from dgov.panes import WorkerPane, escalate_worker_pane

        _write_state(
            str(tmp_path),
            {"panes": [{"slug": "old", "prompt": "Fix the bug", "agent": "pi"}]},
        )
        new_pane = WorkerPane(
            slug="old-esc",
            prompt="Fix the bug",
            pane_id="%99",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path="/wt",
            branch_name="old-esc",
        )
        with (
            patch("dgov.panes.close_worker_pane"),
            patch("dgov.panes.create_worker_pane", return_value=new_pane),
        ):
            result = escalate_worker_pane(str(tmp_path), "old", target_agent="claude")
        assert result["escalated"] is True
        assert result["original_agent"] == "pi"
        assert result["agent"] == "claude"


# ---------------------------------------------------------------------------
# review_worker_pane
# ---------------------------------------------------------------------------


class TestReviewWorkerPane:
    def test_not_found_returns_error(self, tmp_path: Path) -> None:
        from dgov.panes import review_worker_pane

        _write_state(str(tmp_path), {"panes": []})
        result = review_worker_pane(str(tmp_path), "nope")
        assert "error" in result

    def test_no_worktree_returns_error(self, tmp_path: Path) -> None:
        from dgov.panes import review_worker_pane

        _write_state(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "test",
                        "worktree_path": "/nonexistent",
                        "branch_name": "br",
                        "base_sha": "abc",
                    }
                ]
            },
        )
        result = review_worker_pane(str(tmp_path), "test")
        assert "error" in result

    def test_no_base_sha_returns_error(self, tmp_path: Path) -> None:
        from dgov.panes import review_worker_pane

        wt = tmp_path / "wt"
        wt.mkdir()
        _write_state(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "test",
                        "worktree_path": str(wt),
                        "branch_name": "br",
                        "base_sha": "",
                    }
                ]
            },
        )
        result = review_worker_pane(str(tmp_path), "test")
        assert "error" in result


# ---------------------------------------------------------------------------
# rebase_governor
# ---------------------------------------------------------------------------


class TestRebaseGovernor:
    def test_rebase_failure_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import rebase_governor

        def fake_run(cmd, **kw):
            m = MagicMock()
            if "status" in cmd and "--porcelain" in cmd:
                m.stdout = ""
                m.returncode = 0
            elif "fetch" in cmd:
                m.returncode = 0
            elif "rebase" in cmd and "--abort" not in cmd:
                m.returncode = 1
                m.stderr = "CONFLICT in main.py"
            else:
                m.returncode = 0
                m.stderr = ""
            return m

        monkeypatch.setattr("subprocess.run", fake_run)
        result = rebase_governor("/tmp", onto="main")
        assert result["rebased"] is False
        assert "error" in result

    def test_rebase_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import rebase_governor

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        monkeypatch.setattr("subprocess.run", fake_run)
        result = rebase_governor("/tmp", onto="main")
        assert result["rebased"] is True
        assert result["base"] == "main"


# ---------------------------------------------------------------------------
# _qwen_4b_request
# ---------------------------------------------------------------------------


class TestQwen4bRequest:
    def test_localhost_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _qwen_4b_request

        response = {"choices": [{"message": {"content": "ok"}}]}
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps(response).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: fake_resp)
        result = _qwen_4b_request([{"role": "user", "content": "test"}])
        assert result["choices"][0]["message"]["content"] == "ok"

    def test_fallback_to_ssh_on_local_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.panes import _qwen_4b_request

        # Local urlopen fails, SSH succeeds
        monkeypatch.setattr(
            "urllib.request.urlopen",
            MagicMock(side_effect=ConnectionError("refused")),
        )

        response = {"choices": [{"message": {"content": "ssh-result"}}]}
        mock_run = MagicMock()
        mock_run.returncode = 0
        mock_run.stdout = json.dumps(response)
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock_run)

        result = _qwen_4b_request([{"role": "user", "content": "test"}])
        assert result["choices"][0]["message"]["content"] == "ssh-result"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestPaneConstants:
    def test_state_dir(self) -> None:
        from dgov.panes import _STATE_DIR

        assert _STATE_DIR == ".dgov"

    def test_qwen_4b_url(self) -> None:
        from dgov.panes import _QWEN_4B_URL

        assert "8082" in _QWEN_4B_URL


class TestMergeWorkerPane:
    def test_pane_not_found(self, tmp_path: Path) -> None:
        from dgov.panes import merge_worker_pane

        result = merge_worker_pane(str(tmp_path), "nonexistent")
        assert "error" in result
        assert "not found" in result["error"]

    @patch("dgov.panes._full_cleanup")
    @patch("dgov.panes._plumbing_merge")
    @patch("dgov.panes._restore_protected_files")
    @patch("dgov.panes._commit_worktree", return_value={"committed": False})
    @patch("dgov.panes.subprocess.run")
    def test_successful_merge(
        self, mock_run, mock_commit, mock_restore, mock_merge, mock_cleanup, tmp_path: Path
    ) -> None:
        from dgov.models import MergeResult
        from dgov.panes import merge_worker_pane

        mock_merge.return_value = MergeResult(success=True)
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        pane = WorkerPane(
            slug="mergeable",
            prompt="x",
            pane_id="%1",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="feat",
            base_sha="abc",
        )
        _add_pane(str(tmp_path), pane)
        result = merge_worker_pane(str(tmp_path), "mergeable")
        assert result["merged"] == "mergeable"
        assert result["branch"] == "feat"


# ---------------------------------------------------------------------------
# review_worker_pane
# ---------------------------------------------------------------------------


class TestPruneStalePane:
    @patch("dgov.panes.tmux")
    def test_prunes_dead_no_worktree(self, mock_tmux, tmp_path: Path) -> None:
        from dgov.panes import prune_stale_panes

        mock_tmux.pane_exists.return_value = False
        pane = WorkerPane(
            slug="stale",
            prompt="x",
            pane_id="%1",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "nonexistent-wt"),
            branch_name="b",
        )
        _add_pane(str(tmp_path), pane)
        pruned = prune_stale_panes(str(tmp_path))
        assert "stale" in pruned
        assert _get_pane(str(tmp_path), "stale") is None

    @patch("dgov.panes.tmux")
    def test_keeps_alive_pane(self, mock_tmux, tmp_path: Path) -> None:
        from dgov.panes import prune_stale_panes

        mock_tmux.pane_exists.return_value = True
        pane = WorkerPane(
            slug="alive",
            prompt="x",
            pane_id="%1",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "nonexistent"),
            branch_name="b",
        )
        _add_pane(str(tmp_path), pane)
        pruned = prune_stale_panes(str(tmp_path))
        assert pruned == []
        assert _get_pane(str(tmp_path), "alive") is not None

    @patch("dgov.panes.tmux")
    def test_keeps_pane_with_worktree(self, mock_tmux, tmp_path: Path) -> None:
        from dgov.panes import prune_stale_panes

        mock_tmux.pane_exists.return_value = False
        wt = tmp_path / "existing-wt"
        wt.mkdir()
        pane = WorkerPane(
            slug="has-wt",
            prompt="x",
            pane_id="%1",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(wt),
            branch_name="b",
        )
        _add_pane(str(tmp_path), pane)
        pruned = prune_stale_panes(str(tmp_path))
        assert pruned == []


# ---------------------------------------------------------------------------
# capture_worker_output
# ---------------------------------------------------------------------------


class TestRestoreProtectedFiles:
    def test_no_worktree(self) -> None:
        from dgov.panes import _restore_protected_files

        # Should not raise
        _restore_protected_files("/repo", {})

    def test_no_base_sha(self) -> None:
        from dgov.panes import _restore_protected_files

        _restore_protected_files("/repo", {"worktree_path": "/wt", "branch_name": "b"})

    @patch("dgov.panes.subprocess.run")
    def test_restores_changed_protected(self, mock_run, tmp_path: Path) -> None:
        from dgov.panes import _restore_protected_files

        def side_effect(*args, **kwargs):
            cmd = args[0]
            if "diff" in cmd and "--name-only" in cmd:
                return Mock(returncode=0, stdout="CLAUDE.md\nsrc/foo.py\n")
            return Mock(returncode=0)

        mock_run.side_effect = side_effect
        record = {
            "worktree_path": str(tmp_path),
            "branch_name": "feat",
            "base_sha": "abc",
        }
        _restore_protected_files(str(tmp_path), record)
        # Should have called checkout and commit --amend
        cmds = [call[0][0] for call in mock_run.call_args_list]
        assert any("checkout" in c for c in cmds)
        assert any("--amend" in c for c in cmds)

    @patch("dgov.panes.subprocess.run")
    def test_no_protected_changed(self, mock_run, tmp_path: Path) -> None:
        from dgov.panes import _restore_protected_files

        mock_run.return_value = Mock(returncode=0, stdout="src/foo.py\nsrc/bar.py\n")
        record = {
            "worktree_path": str(tmp_path),
            "branch_name": "feat",
            "base_sha": "abc",
        }
        _restore_protected_files(str(tmp_path), record)
        # Only one call (diff), no checkout/amend
        assert mock_run.call_count == 1


class TestWorkerPaneDataclass:
    def test_defaults(self) -> None:
        pane = WorkerPane(
            slug="s",
            prompt="p",
            pane_id="%1",
            agent="pi",
            project_root="/r",
            worktree_path="/w",
            branch_name="b",
        )
        assert pane.owns_worktree is True
        assert pane.base_sha == ""
        assert isinstance(pane.created_at, float)

    def test_custom_fields(self) -> None:
        pane = WorkerPane(
            slug="s",
            prompt="p",
            pane_id="%1",
            agent="claude",
            project_root="/r",
            worktree_path="/w",
            branch_name="b",
            owns_worktree=False,
            base_sha="abc123",
        )
        assert pane.owns_worktree is False
        assert pane.base_sha == "abc123"


# ---------------------------------------------------------------------------
# _validate_state
# ---------------------------------------------------------------------------


class TestValidateState:
    def test_accepts_all_valid_states(self) -> None:
        from dgov.panes import PANE_STATES, _validate_state

        for state in PANE_STATES:
            assert _validate_state(state) == state

    def test_rejects_unknown_state(self) -> None:
        from dgov.panes import _validate_state

        with pytest.raises(ValueError, match="Unknown pane state"):
            _validate_state("bogus")

    def test_rejects_empty_string(self) -> None:
        from dgov.panes import _validate_state

        with pytest.raises(ValueError):
            _validate_state("")


# ---------------------------------------------------------------------------
# _update_pane_state
# ---------------------------------------------------------------------------


class TestUpdatePaneState:
    def test_updates_state_in_json(self, tmp_path: Path) -> None:
        from dgov.panes import _update_pane_state

        _write_state(
            str(tmp_path),
            {"panes": [{"slug": "test", "state": "active"}]},
        )
        _update_pane_state(str(tmp_path), "test", "done")
        state = _read_state(str(tmp_path))
        assert state["panes"][0]["state"] == "done"

    def test_rejects_invalid_state(self, tmp_path: Path) -> None:
        from dgov.panes import _update_pane_state

        _write_state(str(tmp_path), {"panes": [{"slug": "test", "state": "active"}]})
        with pytest.raises(ValueError, match="Unknown pane state"):
            _update_pane_state(str(tmp_path), "test", "invalid")

    def test_noop_for_missing_slug(self, tmp_path: Path) -> None:
        from dgov.panes import _update_pane_state

        _write_state(str(tmp_path), {"panes": [{"slug": "other", "state": "active"}]})
        _update_pane_state(str(tmp_path), "missing", "done")
        state = _read_state(str(tmp_path))
        assert state["panes"][0]["state"] == "active"


# ---------------------------------------------------------------------------
# WorkerPane state validation
# ---------------------------------------------------------------------------


class TestWorkerPaneStateValidation:
    def test_default_state_is_active(self) -> None:
        pane = WorkerPane(
            slug="s",
            prompt="p",
            pane_id="%1",
            agent="pi",
            project_root="/r",
            worktree_path="/w",
            branch_name="b",
        )
        assert pane.state == "active"

    def test_rejects_bad_state(self) -> None:
        with pytest.raises(ValueError, match="Unknown pane state"):
            WorkerPane(
                slug="s",
                prompt="p",
                pane_id="%1",
                agent="pi",
                project_root="/r",
                worktree_path="/w",
                branch_name="b",
                state="invalid_state",
            )

    def test_accepts_valid_state(self) -> None:
        pane = WorkerPane(
            slug="s",
            prompt="p",
            pane_id="%1",
            agent="pi",
            project_root="/r",
            worktree_path="/w",
            branch_name="b",
            state="done",
        )
        assert pane.state == "done"


# ---------------------------------------------------------------------------
# _emit_event
# ---------------------------------------------------------------------------


class TestEmitEvent:
    def test_creates_events_file_and_appends(self, tmp_path: Path) -> None:
        from dgov.panes import _emit_event

        _emit_event(str(tmp_path), "pane_created", "my-slug", agent="pi")
        events_path = tmp_path / ".dgov" / "events.jsonl"
        assert events_path.exists()
        lines = events_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "pane_created"
        assert record["pane"] == "my-slug"
        assert record["agent"] == "pi"
        assert "ts" in record

    def test_appends_multiple_events(self, tmp_path: Path) -> None:
        from dgov.panes import _emit_event

        _emit_event(str(tmp_path), "pane_created", "slug-1")
        _emit_event(str(tmp_path), "pane_done", "slug-1")
        events_path = tmp_path / ".dgov" / "events.jsonl"
        lines = events_path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "pane_created"
        assert json.loads(lines[1])["event"] == "pane_done"

    def test_rejects_unknown_event(self, tmp_path: Path) -> None:
        from dgov.panes import _emit_event

        with pytest.raises(ValueError, match="Unknown event"):
            _emit_event(str(tmp_path), "bogus_event", "slug")

    def test_create_worker_pane_emits_event(self, tmp_path: Path) -> None:
        from dgov.panes import create_worker_pane

        with (
            patch("dgov.panes.subprocess.run") as mock_run,
            patch("dgov.panes.tmux.setup_pane_borders"),
            patch("dgov.panes.tmux.split_pane", return_value="%99"),
            patch("dgov.panes.tmux._run"),
            patch("dgov.panes.tmux.set_title"),
            patch("dgov.panes.tmux.select_layout"),
            patch("dgov.panes.tmux.send_command"),
            patch("dgov.panes.tmux.send_prompt_via_buffer"),
            patch("dgov.panes._trigger_hook", return_value=False),
            patch("dgov.panes._generate_slug", return_value="test-slug"),
        ):
            mock_run.return_value = Mock(returncode=0, stdout="abc123\n", stderr="")
            create_worker_pane(
                project_root=str(tmp_path),
                prompt="Fix the thing",
                agent="claude",
                session_root=str(tmp_path),
            )
        events_path = tmp_path / ".dgov" / "events.jsonl"
        assert events_path.exists()
        lines = events_path.read_text().strip().splitlines()
        records = [json.loads(ln) for ln in lines]
        created = [r for r in records if r["event"] == "pane_created"]
        assert len(created) == 1
        assert created[0]["agent"] == "claude"
        assert created[0]["pane"] == "test-slug"


# ---------------------------------------------------------------------------
# _compute_freshness
# ---------------------------------------------------------------------------


class TestComputeFreshness:
    def test_fresh_no_main_changes(self, tmp_path: Path) -> None:
        from dgov.panes import _compute_freshness

        record = {
            "base_sha": "abc",
            "created_at": time.time(),
            "worktree_path": str(tmp_path),
        }

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("dgov.panes.subprocess.run", fake_run):
            result = _compute_freshness(str(tmp_path), record)
        assert result["freshness"] == "fresh"
        assert result["commits_since_base"] == 0
        assert result["overlapping_files"] == []

    def test_warn_main_advanced(self, tmp_path: Path) -> None:
        from dgov.panes import _compute_freshness

        # Age > 4h triggers warn even without overlap
        record = {
            "base_sha": "abc",
            "created_at": time.time() - 5 * 3600,  # 5 hours ago
            "worktree_path": str(tmp_path),
        }

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            return m

        with patch("dgov.panes.subprocess.run", fake_run):
            result = _compute_freshness(str(tmp_path), record)
        assert result["freshness"] == "warn"
        assert result["pane_age_hours"] > 4

    def test_stale_overlap_many_commits(self, tmp_path: Path) -> None:
        from dgov.panes import _compute_freshness

        record = {
            "base_sha": "abc",
            "created_at": time.time() - 15 * 3600,  # 15 hours ago
            "worktree_path": str(tmp_path),
        }

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            if "log" in cmd:
                m.stdout = "\n".join(f"commit{i} msg" for i in range(8))
            elif "--name-only" in cmd:
                # Both main and worker changed the same file
                m.stdout = "src/shared.py\n"
            else:
                m.stdout = ""
            return m

        with patch("dgov.panes.subprocess.run", fake_run):
            result = _compute_freshness(str(tmp_path), record)
        assert result["freshness"] == "stale"
        assert result["commits_since_base"] == 8
        assert "src/shared.py" in result["overlapping_files"]


# ---------------------------------------------------------------------------
# VALID_EVENTS
# ---------------------------------------------------------------------------


class TestValidEvents:
    def test_contains_expected_events(self) -> None:
        from dgov.panes import VALID_EVENTS

        expected = {
            "pane_created",
            "pane_done",
            "pane_timed_out",
            "pane_merged",
            "pane_merge_failed",
            "pane_escalated",
            "pane_superseded",
            "pane_closed",
            "pane_retry_spawned",
            "checkpoint_created",
            "review_pass",
            "review_fail",
        }
        assert expected == VALID_EVENTS
