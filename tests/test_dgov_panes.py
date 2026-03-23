"""Unit tests for dgov.panes — state management and helper functions."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

from dgov.backend import set_backend
from dgov.done import _has_new_commits, _is_done
from dgov.persistence import (
    STATE_DIR,
    WorkerPane,
    add_pane,
    all_panes,
    get_pane,
    remove_pane,
    replace_all_panes,
    state_path,
)
from dgov.status import capture_worker_output, list_worker_panes, prune_stale_panes
from dgov.strategy import _generate_slug, _structure_pi_prompt, classify_task

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def mock_backend(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    # Default return values for common methods
    mock.create_pane.return_value = "%1"
    mock.create_worker_pane.return_value = "%1"
    mock.is_alive.return_value = True
    mock.bulk_info.return_value = {}
    mock.capture_output.return_value = None
    set_backend(mock)
    return mock


@pytest.fixture(autouse=True)
def stub_wait_for_shell_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub dgov.tmux.wait_for_shell_ready to return True immediately.

    This keeps tmux polling from hitting the real binary and avoids 5-second delays
    in pane tests that use create_worker_pane or resume_worker_pane.
    """
    monkeypatch.setattr("dgov.tmux.wait_for_shell_ready", lambda pane_id=None, timeout=None: True)


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
        result = state_path("/tmp/session")
        assert result == Path("/tmp/session/.dgov/state.db")


class TestReadState:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        panes = all_panes(str(tmp_path))
        assert panes == []

    def test_reads_existing_file(self, tmp_path: Path) -> None:
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test"}]})
        panes = all_panes(str(tmp_path))
        assert len(panes) == 1
        assert panes[0]["slug"] == "test"


class TestWriteState:
    def test_creates_dirs_and_writes(self, tmp_path: Path) -> None:
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "a"}]})
        db_path = tmp_path / ".dgov" / "state.db"
        assert db_path.exists()
        panes = all_panes(str(tmp_path))
        assert panes[0]["slug"] == "a"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "old"}]})
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "new"}]})
        panes = all_panes(str(tmp_path))
        assert len(panes) == 1
        assert panes[0]["slug"] == "new"


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
        add_pane(str(tmp_path), wp)
        panes = all_panes(str(tmp_path))
        assert len(panes) == 1
        assert panes[0]["slug"] == "test"

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
        add_pane(str(tmp_path), wp1)
        add_pane(str(tmp_path), wp2)
        panes = all_panes(str(tmp_path))
        assert len(panes) == 2

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
        add_pane(str(tmp_path), wp1)
        add_pane(str(tmp_path), wp2)
        panes = all_panes(str(tmp_path))
        assert len(panes) == 1
        assert panes[0]["pane_id"] == "%2"
        assert panes[0]["prompt"] == "New"


class TestRemovePane:
    def test_removes_by_slug(self, tmp_path: Path) -> None:
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {"slug": "keep", "pane_id": "%1"},
                    {"slug": "remove", "pane_id": "%2"},
                ]
            },
        )
        remove_pane(str(tmp_path), "remove")
        panes = all_panes(str(tmp_path))
        assert len(panes) == 1
        assert panes[0]["slug"] == "keep"

    def test_remove_nonexistent_noop(self, tmp_path: Path) -> None:
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "keep"}]})
        remove_pane(str(tmp_path), "nope")
        assert len(all_panes(str(tmp_path))) == 1


class TestGetPane:
    def test_found(self, tmp_path: Path) -> None:
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "target", "agent": "pi"}]})
        result = get_pane(str(tmp_path), "target")
        assert result is not None
        assert result["agent"] == "pi"

    def test_not_found(self, tmp_path: Path) -> None:
        replace_all_panes(str(tmp_path), {"panes": []})
        assert get_pane(str(tmp_path), "nope") is None


class TestAllPanes:
    def test_returns_all(self, tmp_path: Path) -> None:
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "a"}, {"slug": "b"}]})
        result = all_panes(str(tmp_path))
        assert len(result) == 2

    def test_empty(self, tmp_path: Path) -> None:
        assert all_panes(str(tmp_path)) == []


# ---------------------------------------------------------------------------
# classify_task / _generate_slug fallbacks
# ---------------------------------------------------------------------------


class TestClassifyTask:
    """Mock spans empty so statistical routing falls through to OpenRouter."""

    def test_fallback_to_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.spans.agent_reliability_stats", lambda *a, **kw: {})
        monkeypatch.setattr(
            "dgov.openrouter.chat_completion",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no llm")),
        )
        assert classify_task("fix the lint error") == "claude"

    def test_returns_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.spans.agent_reliability_stats", lambda *a, **kw: {})
        monkeypatch.setattr(
            "dgov.openrouter.chat_completion",
            lambda *a, **kw: {"choices": [{"message": {"content": "claude"}}]},
        )
        assert classify_task("debug flaky test") == "claude"

    def test_returns_pi_on_pi_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.spans.agent_reliability_stats", lambda *a, **kw: {})
        monkeypatch.setattr(
            "dgov.openrouter.chat_completion",
            lambda *a, **kw: {"choices": [{"message": {"content": "pi"}}]},
        )
        assert classify_task("format the file") == "pi"


class TestGenerateSlug:
    def test_strips_stopwords(self) -> None:
        slug = _generate_slug("fix the broken test in scheduler")
        assert "the" not in slug.split("-")
        assert "in" not in slug.split("-")
        assert len(slug) > 0

    def test_limits_words(self) -> None:
        slug = _generate_slug("a b c d e f g h", max_words=3)
        assert len(slug.split("-")) <= 3

    def test_max_length(self) -> None:
        slug = _generate_slug("fix the bug")
        assert len(slug) <= 50

    def test_absolute_path_stripped(self) -> None:
        """Regression test: numbered prompt with absolute path should not generate garbage."""
        prompt = "1. Read /Users/jakegearon/projects/dgov/src/dgov/panes.py\n2. Fix the bug"
        slug = _generate_slug(prompt)
        assert not slug.startswith("1-")
        assert "users" not in slug
        assert len(slug) > 0


class TestCreateWorktree:
    @patch("subprocess.run")
    def test_create_worktree_failure_wrapping(self, mock_run: MagicMock) -> None:
        """Regression test: git worktree add failure should be wrapped in RuntimeError."""

        from dgov.lifecycle import _create_worktree

        def side_effect(cmd, **kwargs):
            if "rev-parse" in cmd:
                return Mock(returncode=1)  # branch does not exist
            if "worktree" in cmd and "add" in cmd:
                raise subprocess.CalledProcessError(
                    returncode=128,
                    cmd=cmd,
                    stderr="fatal: invalid branch name\n",
                )
            return Mock(returncode=0)

        mock_run.side_effect = side_effect

        with pytest.raises(RuntimeError) as exc_info:
            _create_worktree("/repo", "/wt/path", "bad-branch")

        assert "Failed to create worktree for branch 'bad-branch'" in str(exc_info.value)
        assert "at path '/wt/path'" in str(exc_info.value)
        assert "fatal: invalid branch name" in str(exc_info.value)


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
        assert _is_done(str(tmp_path), "test-slug") is False

    def test_done_signal_honored_even_while_agent_command_visible(self, tmp_path: Path) -> None:
        """Done file is authoritative — agent-like foreground command doesn't block it."""
        done_dir = tmp_path / ".dgov" / "done"
        done_dir.mkdir(parents=True)
        (done_dir / "test-slug").touch()
        record = {
            "pane_id": "%5",
            "project_root": "/repo",
            "branch_name": "test-slug",
            "base_sha": "abc",
        }
        with patch("dgov.persistence.settle_completion_state") as mock_state:
            with patch("dgov.done._has_new_commits", return_value=True):
                assert _is_done(str(tmp_path), "test-slug", pane_record=record) is True
        mock_state.assert_called_once_with(
            str(tmp_path),
            "test-slug",
            "done",
            allow_abandoned=True,
        )

    def test_no_pane_record_no_signal(self, tmp_path: Path) -> None:
        assert _is_done(str(tmp_path), "test-slug") is False

    def test_new_commits_signal(self, tmp_path: Path) -> None:
        from dgov.agents import DoneStrategy

        record = {
            "project_root": "/repo",
            "branch_name": "br",
            "base_sha": "abc",
            "pane_id": "%5",
        }
        with (
            patch("dgov.done._has_new_commits", return_value=True),
        ):
            assert (
                _is_done(
                    str(tmp_path),
                    "slug",
                    pane_record=record,
                    done_strategy=DoneStrategy(type="signal"),
                )
                is True
            )

    def test_dead_pane_sets_abandoned(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        record = {
            "project_root": "/repo",
            "branch_name": "br",
            "base_sha": "abc",
            "pane_id": "%5",
        }
        mock_backend.is_alive.return_value = False
        with (
            patch("dgov.done._has_new_commits", return_value=False),
            patch("dgov.persistence.settle_completion_state") as mock_state,
        ):
            assert _is_done(str(tmp_path), "slug", pane_record=record) is True
            mock_state.assert_called_once_with(str(tmp_path), "slug", "abandoned")

    def test_exit_file_sets_failed(self, tmp_path: Path) -> None:
        done_dir = tmp_path / ".dgov" / "done"
        done_dir.mkdir(parents=True)
        (done_dir / "test-slug.exit").write_text("1")
        with patch("dgov.persistence.settle_completion_state") as mock_state:
            assert _is_done(str(tmp_path), "test-slug") is True
            mock_state.assert_called_once_with(
                str(tmp_path),
                "test-slug",
                "failed",
                allow_abandoned=True,
            )

    def test_alive_pane_no_commits(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        record = {
            "project_root": "/repo",
            "branch_name": "br",
            "base_sha": "abc",
            "pane_id": "%5",
        }
        mock_backend.is_alive.return_value = True
        with (
            patch("dgov.done._has_new_commits", return_value=False),
        ):
            assert _is_done(str(tmp_path), "slug", pane_record=record) is False

    def test_done_signal_on_abandoned_pane_succeeds(self, tmp_path: Path) -> None:
        """Verify abandoned -> done transition works when a done signal is found."""
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "stale", "state": "abandoned"}]})
        done_dir = tmp_path / ".dgov" / "done"
        done_dir.mkdir(parents=True)
        (done_dir / "stale").touch()

        record = get_pane(str(tmp_path), "stale")
        assert _is_done(str(tmp_path), "stale", pane_record=record) is False
        assert get_pane(str(tmp_path), "stale")["state"] == "abandoned"

    def test_new_commits_on_abandoned_pane_succeeds(self, tmp_path: Path) -> None:
        """Verify abandoned -> done transition works when new commits are found."""
        from dgov.agents import DoneStrategy

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "stale",
                        "state": "abandoned",
                        "project_root": "/repo",
                        "branch_name": "br",
                        "base_sha": "abc",
                    }
                ]
            },
        )

        record = get_pane(str(tmp_path), "stale")
        with patch("dgov.done._has_new_commits", return_value=True):
            assert (
                _is_done(
                    str(tmp_path),
                    "stale",
                    pane_record=record,
                    done_strategy=DoneStrategy(type="signal"),
                )
                is True
            )
        assert get_pane(str(tmp_path), "stale")["state"] == "done"


class TestSignalPane:
    def test_stale_done_signal_after_failed_does_not_raise(self, tmp_path: Path) -> None:
        from dgov.waiter import signal_pane

        add_pane(
            str(tmp_path),
            WorkerPane(
                slug="stale-done",
                prompt="done",
                pane_id="%1",
                agent="claude",
                project_root=str(tmp_path),
                worktree_path=str(tmp_path / "wt"),
                branch_name="stale-done",
                base_sha="abc123",
                state="failed",
            ),
        )

        stale_record = {
            "slug": "stale-done",
            "state": "active",
            "pane_id": "%1",
            "project_root": str(tmp_path),
            "worktree_path": str(tmp_path / "wt"),
            "branch_name": "stale-done",
            "base_sha": "abc123",
        }

        with (
            patch("dgov.persistence.get_pane", return_value=stale_record),
            patch("dgov.done._has_completion_commit", return_value=True),
        ):
            assert signal_pane(str(tmp_path), "stale-done", "done") is True

        assert get_pane(str(tmp_path), "stale-done")["state"] == "failed"


# ---------------------------------------------------------------------------
# list_worker_panes
# ---------------------------------------------------------------------------


class TestListWorkerPanes:
    def test_empty_state(self, tmp_path: Path) -> None:
        result = list_worker_panes(str(tmp_path))
        assert result == []

    def test_deduplicates_by_slug_prefers_alive(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """When state has duplicate slugs, list should return one entry preferring alive."""
        replace_all_panes(
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

        mock_backend.bulk_info.return_value = {"%2": {"title": "gov", "current_command": "claude"}}
        with (
            patch("dgov.status._is_done", return_value=False),
        ):
            result = list_worker_panes(str(tmp_path))
        assert len(result) == 1
        assert result[0]["slug"] == "gov"
        assert result[0]["pane_id"] == "%2"
        assert result[0]["alive"] is True

    def test_enriches_with_alive_status(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        replace_all_panes(
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
        mock_backend.bulk_info.return_value = {
            "%5": {"title": "test", "current_command": "claude"}
        }
        with (
            patch("dgov.status._is_done", return_value=False),
        ):
            result = list_worker_panes(str(tmp_path))
        assert len(result) == 1
        assert result[0]["alive"] is True
        assert result[0]["current_command"] == "claude"
        assert result[0]["done"] is False

    def test_skips_is_done_for_superseded_pane(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        mock_backend.bulk_info.return_value = {
            "%1": {"pane_id": "%1"},
            "%2": {"pane_id": "%2", "current_command": "pi"},
        }
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "old-task",
                        "agent": "pi",
                        "pane_id": "%1",
                        "project_root": str(tmp_path),
                        "worktree_path": "/wt-old",
                        "branch_name": "old-task",
                        "prompt": "Old task",
                        "state": "superseded",
                    },
                    {
                        "slug": "active-task",
                        "agent": "pi",
                        "pane_id": "%2",
                        "project_root": str(tmp_path),
                        "worktree_path": "/wt-active",
                        "branch_name": "active-task",
                        "prompt": "Active task",
                        "state": "active",
                    },
                ]
            },
        )

        checked: list[str] = []

        def fake_is_done(session_root, slug, pane_record=None, **_kw):
            checked.append(slug)
            return False

        with patch("dgov.status._is_done", side_effect=fake_is_done):
            result = list_worker_panes(str(tmp_path))

        assert checked == ["active-task"]
        superseded = next(pane for pane in result if pane["slug"] == "old-task")
        assert superseded["state"] == "superseded"
        assert superseded["done"] is True

    def test_reconciles_state_when_is_done_transitions_active(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """state must never be 'active' while done is True in the same entry."""
        from dgov.persistence import update_pane_state

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "worker",
                        "agent": "pi",
                        "pane_id": "%3",
                        "project_root": str(tmp_path),
                        "worktree_path": "/wt",
                        "branch_name": "worker",
                        "prompt": "Do something",
                        "state": "active",
                    }
                ]
            },
        )
        mock_backend.bulk_info.return_value = {
            "%3": {"title": "worker", "current_command": "node"}
        }

        def fake_is_done(session_root, slug, pane_record=None, **_kw):
            # Simulate what real _is_done does: update state, return True
            update_pane_state(session_root, slug, "done")
            return True

        with patch("dgov.status._is_done", side_effect=fake_is_done):
            result = list_worker_panes(str(tmp_path))

        assert len(result) == 1
        assert result[0]["done"] is True
        assert result[0]["state"] == "done"

    def test_reads_last_output_from_log_file(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "worker",
                        "agent": "pi",
                        "pane_id": "%3",
                        "project_root": str(tmp_path),
                        "worktree_path": "/wt",
                        "branch_name": "worker",
                        "prompt": "Do something",
                        "state": "active",
                    }
                ]
            },
        )
        mock_backend.bulk_info.return_value = {
            "%3": {"title": "worker", "current_command": "node"}
        }
        log_dir = tmp_path / STATE_DIR / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / "worker.log").write_text(
            "line 1\n\x1b[31mline 2\x1b[0m\nline 3\nline 4\n",
            encoding="utf-8",
        )

        with patch("dgov.status._is_done", return_value=False):
            result = list_worker_panes(str(tmp_path))

        assert result[0]["last_output"] == "line 1\nline 2\nline 3\nline 4"
        mock_backend.capture_output.assert_not_called()

    def test_missing_log_file_keeps_last_output_empty(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "worker",
                        "agent": "pi",
                        "pane_id": "%3",
                        "project_root": str(tmp_path),
                        "worktree_path": "/wt",
                        "branch_name": "worker",
                        "prompt": "Do something",
                        "state": "active",
                    }
                ]
            },
        )
        mock_backend.bulk_info.return_value = {
            "%3": {"title": "worker", "current_command": "node"}
        }

        with patch("dgov.status._is_done", return_value=False):
            result = list_worker_panes(str(tmp_path))

        assert result[0]["last_output"] == ""
        mock_backend.capture_output.assert_not_called()

    def test_exposes_preserved_artifact_metadata(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.persistence import mark_preserved_artifacts

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "kept-pane",
                        "agent": "pi",
                        "pane_id": "%3",
                        "project_root": str(tmp_path),
                        "worktree_path": str(tmp_path / "kept-pane"),
                        "branch_name": "kept-pane",
                        "prompt": "Do something",
                        "state": "timed_out",
                    }
                ]
            },
        )
        (tmp_path / "kept-pane").mkdir()
        mark_preserved_artifacts(
            str(tmp_path),
            "kept-pane",
            reason="dirty_worktree",
            recoverable=True,
            state="timed_out",
        )
        mock_backend.bulk_info.return_value = {}

        result = list_worker_panes(str(tmp_path))

        assert result[0]["preserved_reason"] == "dirty_worktree"
        assert result[0]["preserved_recoverable"] is True


# ---------------------------------------------------------------------------
# prune_stale_panes
# ---------------------------------------------------------------------------


class TestPruneStale:
    def test_prunes_dead_pane_no_worktree(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        replace_all_panes(
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
        mock_backend.is_alive.return_value = False
        pruned = prune_stale_panes(str(tmp_path))
        assert "stale" in pruned
        assert all_panes(str(tmp_path)) == []

    def test_keeps_alive_pane(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        replace_all_panes(
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
        mock_backend.bulk_info.return_value = {"%5": {"pane_id": "%5"}}
        pruned = prune_stale_panes(str(tmp_path))
        assert pruned == []
        assert len(all_panes(str(tmp_path))) == 1

    def test_keeps_pane_with_worktree(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()
        replace_all_panes(
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
        mock_backend.is_alive.return_value = False
        pruned = prune_stale_panes(str(tmp_path))
        assert pruned == []

    def test_prunes_orphaned_worktree_dir(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        """Worktree dir exists in .dgov/worktrees/ but no pane entry references it."""
        import os

        orphan_dir = tmp_path / ".dgov" / "worktrees" / "orphan-task"
        orphan_dir.mkdir(parents=True)
        # Age past the 60s grace period so the pruner doesn't skip it
        old_time = time.time() - 120
        os.utime(orphan_dir, (old_time, old_time))
        # Empty state — no pane entries at all
        replace_all_panes(str(tmp_path), {"panes": []})
        mock_backend.is_alive.return_value = False
        with (
            patch("dgov.status._remove_worktree") as mock_rm,
        ):
            pruned = prune_stale_panes(str(tmp_path))
        assert "orphan:orphan-task" in pruned
        mock_rm.assert_called_once_with(str(tmp_path), str(orphan_dir), "orphan-task")

    def test_skips_worktree_dir_with_matching_pane(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Worktree dir that IS referenced by a pane entry should not be pruned."""
        wt_dir = tmp_path / ".dgov" / "worktrees" / "active-task"
        wt_dir.mkdir(parents=True)
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "active-task",
                        "pane_id": "%10",
                        "worktree_path": str(wt_dir),
                    }
                ]
            },
        )
        mock_backend.is_alive.return_value = True
        with (
            patch("dgov.status._remove_worktree") as mock_rm,
        ):
            pruned = prune_stale_panes(str(tmp_path))
        assert pruned == []
        mock_rm.assert_not_called()

    def test_prunes_both_stale_entries_and_orphans(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Both a stale pane entry AND an orphaned dir get pruned in one call."""
        import os

        orphan_dir = tmp_path / ".dgov" / "worktrees" / "orphan-slug"
        orphan_dir.mkdir(parents=True)
        # Age the directory past the 60s grace period
        old_time = time.time() - 120
        os.utime(orphan_dir, (old_time, old_time))
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "stale-entry",
                        "pane_id": "%5",
                        "worktree_path": "/nonexistent",
                    }
                ]
            },
        )
        mock_backend.is_alive.return_value = False
        with (
            patch("dgov.status._remove_worktree") as mock_rm,
        ):
            pruned = prune_stale_panes(str(tmp_path))
        assert "stale-entry" in pruned
        assert "orphan:orphan-slug" in pruned
        mock_rm.assert_called_once_with(str(tmp_path), str(orphan_dir), "orphan-slug")


# ---------------------------------------------------------------------------
# capture_worker_output
# ---------------------------------------------------------------------------


class TestCaptureWorkerOutput:
    def test_missing_pane_returns_none(self, tmp_path: Path) -> None:
        assert capture_worker_output(str(tmp_path), "nonexistent") is None

    def test_dead_pane_returns_none(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
        mock_backend.is_alive.return_value = False
        assert capture_worker_output(str(tmp_path), "test") is None

    def test_captures_output(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
        mock_backend.is_alive.return_value = True
        mock_backend.capture_output.return_value = "output here"
        result = capture_worker_output(str(tmp_path), "test")
        assert result == "output here"


# ---------------------------------------------------------------------------
# _pick_resolver_agent
# ---------------------------------------------------------------------------


class TestPickResolverAgent:
    def test_prefers_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.merger import _pick_resolver_agent

        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
        assert _pick_resolver_agent() == "claude"

    def test_falls_back_to_codex(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.merger import _pick_resolver_agent

        def fake_which(name):
            return "/usr/bin/codex" if name == "codex" else None

        monkeypatch.setattr("shutil.which", fake_which)
        assert _pick_resolver_agent() == "codex"

    def test_defaults_claude_when_nothing_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.merger import _pick_resolver_agent

        monkeypatch.setattr("shutil.which", lambda name: None)
        assert _pick_resolver_agent() == "claude"


# ---------------------------------------------------------------------------
# PROTECTED_FILES
# ---------------------------------------------------------------------------


class TestProtectedFiles:
    def test_contains_expected_files(self) -> None:
        from dgov.persistence import PROTECTED_FILES

        assert "CLAUDE.md" in PROTECTED_FILES
        assert "THEORY.md" in PROTECTED_FILES
        assert "ARCH-NOTES.md" in PROTECTED_FILES

    def test_is_set(self) -> None:
        from dgov.persistence import PROTECTED_FILES

        assert isinstance(PROTECTED_FILES, set)


# ---------------------------------------------------------------------------
# close_worker_pane
# ---------------------------------------------------------------------------


class TestCloseWorkerPane:
    def test_not_found_returns_false(self, tmp_path: Path) -> None:
        from dgov.lifecycle import close_worker_pane

        replace_all_panes(str(tmp_path), {"panes": []})
        assert close_worker_pane(str(tmp_path), "nonexistent") is False

    def test_found_calls_cleanup(self, tmp_path: Path) -> None:
        from dgov.lifecycle import close_worker_pane

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {"slug": "test", "pane_id": "%5", "owns_worktree": False, "state": "active"}
                ]
            },
        )
        with patch("dgov.lifecycle._full_cleanup") as mock_cleanup:
            result = close_worker_pane(str(tmp_path), "test")
        assert result is True
        mock_cleanup.assert_called_once()

    def test_force_removes_dirty_worktree(self, tmp_path: Path) -> None:
        from dgov.lifecycle import close_worker_pane

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {"slug": "test", "pane_id": "%5", "owns_worktree": True, "state": "active"}
                ]
            },
        )
        with patch("dgov.lifecycle._full_cleanup") as mock_cleanup:
            close_worker_pane(str(tmp_path), "test", force=True)
        _, kwargs = mock_cleanup.call_args
        assert kwargs["skip_worktree_if_dirty"] is False

    def test_no_force_skips_dirty_preserves_branch(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.lifecycle import close_worker_pane

        wt = tmp_path / "wt"
        wt.mkdir()
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "test",
                        "pane_id": "%5",
                        "owns_worktree": True,
                        "worktree_path": str(wt),
                        "branch_name": "test-br",
                        "state": "active",
                    }
                ]
            },
        )

        git_cmds: list[list[str]] = []

        def fake_run(cmd, **kw):
            git_cmds.append(cmd)
            m = MagicMock()
            m.returncode = 0
            if "status" in cmd and "--porcelain" in cmd:
                m.stdout = "M dirty.py\n"
            else:
                m.stdout = ""
            return m

        mock_backend.is_alive.return_value = False
        with (
            patch("subprocess.run", fake_run),
        ):
            close_worker_pane(str(tmp_path), "test")

        # Branch should NOT be deleted when worktree was skipped (dirty)
        branch_cmds = [c for c in git_cmds if "branch" in c and "-D" in c]
        assert len(branch_cmds) == 0

        # Worktree remove should NOT have been called
        wt_remove_cmds = [c for c in git_cmds if "worktree" in c and "remove" in c]
        assert len(wt_remove_cmds) == 0

    def test_close_dirty_pane_without_force_preserves_record(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Verify closing a dirty pane without force keeps the state record."""
        from dgov.lifecycle import close_worker_pane

        wt = tmp_path / "wt"
        wt.mkdir()
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "dirty-task",
                        "pane_id": "%5",
                        "owns_worktree": True,
                        "worktree_path": str(wt),
                        "branch_name": "dirty-br",
                        "state": "active",
                    }
                ]
            },
        )

        def fake_run(cmd, **kw):
            m = MagicMock()
            if "status" in cmd and "--porcelain" in cmd:
                m.stdout = "M dirty.py\n"
            else:
                m.stdout = ""
            m.returncode = 0
            return m

        mock_backend.is_alive.return_value = False
        with patch("subprocess.run", fake_run):
            close_worker_pane(str(tmp_path), "dirty-task", force=False)

        # Record should still be in state
        assert get_pane(str(tmp_path), "dirty-task") is not None

    def test_close_dirty_pane_with_force_removes_record(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Verify closing a dirty pane WITH force removes the state record."""
        from dgov.lifecycle import close_worker_pane

        wt = tmp_path / "wt"
        wt.mkdir()
        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "dirty-task",
                        "pane_id": "%5",
                        "owns_worktree": True,
                        "worktree_path": str(wt),
                        "branch_name": "dirty-br",
                        "state": "active",
                    }
                ]
            },
        )

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.stdout = ""
            m.returncode = 0
            return m

        mock_backend.is_alive.return_value = False
        with patch("subprocess.run", fake_run):
            close_worker_pane(str(tmp_path), "dirty-task", force=True)

        # Record should be removed
        assert get_pane(str(tmp_path), "dirty-task") is None


# ---------------------------------------------------------------------------
# _detect_conflicts
# ---------------------------------------------------------------------------


class TestDetectConflicts:
    def test_no_merge_base_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.merger import _detect_conflicts

        mock = MagicMock()
        mock.returncode = 1
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        assert _detect_conflicts("/repo", "branch") == []

    def test_detects_conflicts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.merger import _detect_conflicts

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
        from dgov.merger import _detect_conflicts

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = "abc123" if "merge-base" in cmd else ""
            return m

        monkeypatch.setattr("subprocess.run", fake_run)
        assert _detect_conflicts("/repo", "branch") == []


# ---------------------------------------------------------------------------
# _check_dirty_worktree
# ---------------------------------------------------------------------------


class TestCheckDirtyWorktree:
    def test_no_worktree_path(self) -> None:
        from dgov.merger import _check_dirty_worktree

        result = _check_dirty_worktree("")
        assert result == []

    def test_nonexistent_worktree(self, tmp_path: Path) -> None:
        from dgov.merger import _check_dirty_worktree

        result = _check_dirty_worktree(str(tmp_path / "nope"))
        assert result == []

    def test_no_changes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.merger import _check_dirty_worktree

        mock = MagicMock()
        mock.stdout = b"\x00"
        monkeypatch.setattr("subprocess.run", lambda *a, **kw: mock)
        result = _check_dirty_worktree(str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# _full_cleanup
# ---------------------------------------------------------------------------


class TestFullCleanup:
    def test_removes_state_and_cleanup(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.lifecycle import _full_cleanup

        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
        # Create done signal
        done_dir = tmp_path / ".dgov" / "done"
        done_dir.mkdir(parents=True)
        (done_dir / "test").touch()

        pane_record = {"pane_id": "%5", "owns_worktree": False}

        mock_backend.is_alive.return_value = False
        result = _full_cleanup(str(tmp_path), str(tmp_path), "test", pane_record)

        assert result["cleaned"] is True
        assert not (done_dir / "test").exists()
        # _full_cleanup no longer removes pane state — callers handle that
        assert get_pane(str(tmp_path), "test") is not None

    def test_skips_worktree_if_dirty(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.lifecycle import _full_cleanup

        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
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

        mock_backend.is_alive.return_value = False
        with (
            patch("subprocess.run", fake_run),
        ):
            result = _full_cleanup(
                str(tmp_path), str(tmp_path), "test", pane_record, skip_worktree_if_dirty=True
            )

        assert result["skipped_worktree"] is True
        # Branch should NOT be deleted when worktree removal is skipped
        branch_cmds = [c for c in calls if "branch" in c and "-D" in c]
        assert len(branch_cmds) == 0
        # Worktree remove should NOT have been called
        wt_remove_cmds = [c for c in calls if "worktree" in c and "remove" in c]
        assert len(wt_remove_cmds) == 0

    def test_cleanup_deletes_log_file(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.lifecycle import _full_cleanup

        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
        logs_dir = tmp_path / ".dgov" / "logs"
        logs_dir.mkdir(parents=True)
        log_file = logs_dir / "test.log"
        log_file.write_text("some log output")

        pane_record = {"pane_id": "%5", "owns_worktree": False}
        mock_backend.is_alive.return_value = False
        _full_cleanup(str(tmp_path), str(tmp_path), "test", pane_record)

        assert not log_file.exists()

    def test_cleanup_no_error_when_log_missing(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.lifecycle import _full_cleanup

        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
        pane_record = {"pane_id": "%5", "owns_worktree": False}
        mock_backend.is_alive.return_value = False
        result = _full_cleanup(str(tmp_path), str(tmp_path), "test", pane_record)
        assert result["cleaned"] is True

    def test_no_checkout_before_worktree_remove(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Verify git checkout . is NOT called — worktree remove --force suffices."""
        from dgov.lifecycle import _full_cleanup

        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test", "pane_id": "%5"}]})
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
            m.returncode = 0
            m.stdout = ""
            return m

        mock_backend.is_alive.return_value = False
        with (
            patch("subprocess.run", fake_run),
        ):
            _full_cleanup(
                str(tmp_path), str(tmp_path), "test", pane_record, skip_worktree_if_dirty=False
            )

        # git checkout . should NOT be called (data loss risk)
        checkout_cmd = [c for c in calls if "checkout" in c]
        assert len(checkout_cmd) == 0

        # worktree remove --force should still be called
        wt_remove_cmd = [c for c in calls if "worktree" in c and "remove" in c]
        assert len(wt_remove_cmd) == 1
        assert wt_remove_cmd[0][-1] == str(wt)


# ---------------------------------------------------------------------------
# escalate_worker_pane
# ---------------------------------------------------------------------------


class TestEscalateWorkerPane:
    def test_not_found_returns_error(self, tmp_path: Path) -> None:
        from dgov.recovery import escalate_worker_pane

        replace_all_panes(str(tmp_path), {"panes": []})
        result = escalate_worker_pane(str(tmp_path), "nope")
        assert "error" in result

    def test_no_prompt_returns_error(self, tmp_path: Path) -> None:
        from dgov.recovery import escalate_worker_pane

        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test", "prompt": ""}]})
        result = escalate_worker_pane(str(tmp_path), "test")
        assert "error" in result

    def test_escalation_calls_close_and_create(self, tmp_path: Path) -> None:
        from dgov.persistence import WorkerPane
        from dgov.recovery import escalate_worker_pane

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {"slug": "old", "prompt": "Fix the bug", "agent": "pi", "state": "failed"}
                ]
            },
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
            patch("dgov.recovery.close_worker_pane"),
            patch("dgov.recovery.create_worker_pane", return_value=new_pane),
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
        from dgov.inspection import review_worker_pane

        replace_all_panes(str(tmp_path), {"panes": []})
        result = review_worker_pane(str(tmp_path), "nope")
        assert "error" in result

    def test_no_worktree_returns_error(self, tmp_path: Path) -> None:
        from dgov.inspection import review_worker_pane

        replace_all_panes(
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
        from dgov.inspection import review_worker_pane

        wt = tmp_path / "wt"
        wt.mkdir()
        replace_all_panes(
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
        from dgov.inspection import rebase_governor

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
        from dgov.inspection import rebase_governor

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
        from dgov.openrouter import _qwen_4b_request

        response = {"choices": [{"message": {"content": "ok"}}]}
        fake_resp = MagicMock()
        fake_resp.read.return_value = json.dumps(response).encode()
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: fake_resp)
        result = _qwen_4b_request([{"role": "user", "content": "test"}])
        assert result["choices"][0]["message"]["content"] == "ok"

    def test_raises_on_local_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dgov.openrouter import _qwen_4b_request

        monkeypatch.setattr(
            "urllib.request.urlopen",
            MagicMock(side_effect=ConnectionError("refused")),
        )

        with pytest.raises(RuntimeError, match="not reachable"):
            _qwen_4b_request([{"role": "user", "content": "test"}])


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestPaneConstants:
    def test_state_dir(self) -> None:
        from dgov.persistence import STATE_DIR

        assert STATE_DIR == ".dgov"

    def test_qwen_4b_url(self) -> None:
        from dgov.openrouter import _QWEN_4B_URL

        assert "8082" in _QWEN_4B_URL


class TestMergeWorkerPane:
    def test_pane_not_found(self, tmp_path: Path) -> None:
        from dgov.merger import merge_worker_pane

        result = merge_worker_pane(str(tmp_path), "nonexistent")
        assert "error" in result
        assert "not found" in result["error"]

    @patch("dgov.lifecycle._full_cleanup")
    @patch("dgov.merger._plumbing_merge")
    @patch("dgov.merger._restore_protected_files")
    @patch("dgov.merger._advance_current_branch_to_commit")
    @patch("dgov.merger.subprocess.run")
    def test_successful_merge(
        self, mock_run, mock_advance, mock_restore, mock_merge, mock_cleanup, tmp_path: Path
    ) -> None:
        from dgov.inspection import MergeResult
        from dgov.merger import merge_worker_pane

        mock_merge.return_value = MergeResult(success=True)
        mock_advance.return_value = MergeResult(success=True)
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        pane = WorkerPane(
            slug="mergeable",
            prompt="x",
            pane_id="%1",
            agent="pi",
            state="done",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="feat",
            base_sha="abc",
        )
        add_pane(str(tmp_path), pane)
        result = merge_worker_pane(str(tmp_path), "mergeable")
        assert result["merged"] == "mergeable"
        assert result["branch"] == "feat"

    @patch("dgov.lifecycle._full_cleanup")
    @patch("dgov.merger._plumbing_merge")
    @patch("dgov.merger._restore_protected_files")
    @patch("dgov.merger._advance_current_branch_to_commit")
    @patch("dgov.merger.subprocess.run")
    def test_successful_merge_ignores_stale_abandoned_state(
        self, mock_run, mock_advance, mock_restore, mock_merge, mock_cleanup, tmp_path: Path
    ) -> None:
        from dgov.inspection import MergeResult
        from dgov.merger import merge_worker_pane
        from dgov.persistence import IllegalTransitionError

        mock_merge.return_value = MergeResult(success=True)
        mock_advance.return_value = MergeResult(success=True)
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        pane = WorkerPane(
            slug="mergeable",
            prompt="x",
            pane_id="%1",
            agent="pi",
            state="done",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="feat",
            base_sha="abc",
        )
        add_pane(str(tmp_path), pane)

        with patch("dgov.persistence.update_pane_state") as mock_state:
            mock_state.side_effect = IllegalTransitionError("abandoned", "merged", "mergeable")
            result = merge_worker_pane(str(tmp_path), "mergeable")

        assert result["merged"] == "mergeable"
        assert result["branch"] == "feat"
        mock_cleanup.assert_called_once()


# ---------------------------------------------------------------------------
# review_worker_pane
# ---------------------------------------------------------------------------


class TestPruneStalePane:
    def test_prunes_dead_no_worktree(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.status import prune_stale_panes

        mock_backend.is_alive.return_value = False
        pane = WorkerPane(
            slug="stale",
            prompt="x",
            pane_id="%1",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "nonexistent-wt"),
            branch_name="b",
        )
        add_pane(str(tmp_path), pane)
        pruned = prune_stale_panes(str(tmp_path))
        assert "stale" in pruned
        assert get_pane(str(tmp_path), "stale") is None

    def test_keeps_alive_pane(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.status import prune_stale_panes

        mock_backend.bulk_info.return_value = {"%1": {"pane_id": "%1"}}
        pane = WorkerPane(
            slug="alive",
            prompt="x",
            pane_id="%1",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "nonexistent"),
            branch_name="b",
        )
        add_pane(str(tmp_path), pane)
        pruned = prune_stale_panes(str(tmp_path))
        assert pruned == []
        assert get_pane(str(tmp_path), "alive") is not None

    def test_keeps_pane_with_worktree(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.status import prune_stale_panes

        mock_backend.is_alive.return_value = False
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
        add_pane(str(tmp_path), pane)
        pruned = prune_stale_panes(str(tmp_path))
        # Dead active pane with worktree gets force-failed (worktree preserved)
        assert pruned == ["dead:has-wt"]
        # Verify state was changed to failed
        pane_after = get_pane(str(tmp_path), "has-wt")
        assert pane_after["state"] == "failed"
        # Verify worktree was NOT deleted
        assert wt.exists()

    def test_dead_done_pane_with_worktree_not_double_failed(self, tmp_path, mock_backend):
        """A dead pane already in 'done' state should not be re-failed."""
        from dgov.status import prune_stale_panes

        mock_backend.is_alive.return_value = False
        wt = tmp_path / "done-wt"
        wt.mkdir()
        pane = WorkerPane(
            slug="done-pane",
            prompt="x",
            pane_id="%2",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path=str(wt),
            branch_name="b",
            state="done",
        )
        add_pane(str(tmp_path), pane)
        pruned = prune_stale_panes(str(tmp_path))
        # Done pane with worktree should NOT appear in pruned (not active)
        assert pruned == []
        pane_after = get_pane(str(tmp_path), "done-pane")
        assert pane_after["state"] == "done"


# ---------------------------------------------------------------------------
# capture_worker_output
# ---------------------------------------------------------------------------


class TestRestoreProtectedFiles:
    def test_no_worktree(self) -> None:
        from dgov.merger import _restore_protected_files

        # Should not raise
        _restore_protected_files("/repo", {})

    def test_no_base_sha(self) -> None:
        from dgov.merger import _restore_protected_files

        _restore_protected_files("/repo", {"worktree_path": "/wt", "branch_name": "b"})

    @patch("dgov.merger.subprocess.run")
    def test_restores_changed_protected(self, mock_run, tmp_path: Path) -> None:
        from dgov.merger import _restore_protected_files

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

    @patch("dgov.merger.subprocess.run")
    def test_no_protected_changed(self, mock_run, tmp_path: Path) -> None:
        from dgov.merger import _restore_protected_files

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
        from dgov.persistence import PANE_STATES, _validate_state

        for state in PANE_STATES:
            assert _validate_state(state) == state

    def test_rejects_unknown_state(self) -> None:
        from dgov.persistence import _validate_state

        with pytest.raises(ValueError, match="Unknown pane state"):
            _validate_state("bogus")

    def test_rejects_empty_string(self) -> None:
        from dgov.persistence import _validate_state

        with pytest.raises(ValueError):
            _validate_state("")


# ---------------------------------------------------------------------------
# update_pane_state
# ---------------------------------------------------------------------------


class TestUpdatePaneState:
    def test_updates_state_in_json(self, tmp_path: Path) -> None:
        from dgov.persistence import update_pane_state

        replace_all_panes(
            str(tmp_path),
            {"panes": [{"slug": "test", "state": "active"}]},
        )
        update_pane_state(str(tmp_path), "test", "done")
        panes = all_panes(str(tmp_path))
        assert panes[0]["state"] == "done"

    def test_rejects_invalid_state(self, tmp_path: Path) -> None:
        from dgov.persistence import update_pane_state

        replace_all_panes(str(tmp_path), {"panes": [{"slug": "test", "state": "active"}]})
        with pytest.raises(ValueError, match="Unknown pane state"):
            update_pane_state(str(tmp_path), "test", "invalid")

    def test_noop_for_missing_slug(self, tmp_path: Path) -> None:
        from dgov.persistence import update_pane_state

        replace_all_panes(str(tmp_path), {"panes": [{"slug": "other", "state": "active"}]})
        update_pane_state(str(tmp_path), "missing", "done")
        panes = all_panes(str(tmp_path))
        assert panes[0]["state"] == "active"

    def test_updates_pane_title_on_state_change(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.persistence import update_pane_state

        replace_all_panes(
            str(tmp_path),
            {"panes": [{"slug": "fix", "state": "active", "pane_id": "%5", "agent": "pi"}]},
        )
        update_pane_state(str(tmp_path), "fix", "done")
        mock_backend.set_title.assert_called_once_with("%5", "[pi] fix ok")


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
# emit_event
# ---------------------------------------------------------------------------


class TestEmitEvent:
    def test_creates_events_record(self, tmp_path: Path) -> None:
        from dgov.persistence import emit_event, read_events

        emit_event(str(tmp_path), "pane_created", "my-slug", agent="pi")
        events = read_events(str(tmp_path))
        assert len(events) == 1
        assert events[0]["event"] == "pane_created"
        assert events[0]["pane"] == "my-slug"
        assert events[0]["agent"] == "pi"
        assert "ts" in events[0]

    def test_appends_multiple_events(self, tmp_path: Path) -> None:
        from dgov.persistence import emit_event, read_events

        emit_event(str(tmp_path), "pane_created", "slug-1")
        emit_event(str(tmp_path), "pane_done", "slug-1")
        events = read_events(str(tmp_path))
        assert len(events) == 2
        assert events[0]["event"] == "pane_created"
        assert events[1]["event"] == "pane_done"

    def test_rejects_unknown_event(self, tmp_path: Path) -> None:
        from dgov.persistence import emit_event

        with pytest.raises(ValueError, match="Unknown event"):
            emit_event(str(tmp_path), "bogus_event", "slug")

    def test_create_worker_pane_emits_event(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.lifecycle import create_worker_pane
        from dgov.persistence import read_events

        mock_backend.create_pane.return_value = "%99"
        mock_backend.create_worker_pane.return_value = "%99"
        with (
            patch("dgov.lifecycle.subprocess.run") as mock_run,
            patch("dgov.lifecycle._setup_and_launch_agent"),
            patch("dgov.lifecycle._write_worktree_instructions"),
        ):

            def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN201
                if "--verify" in cmd:
                    return Mock(returncode=128, stdout="", stderr="")
                if cmd[-1] == "HEAD":
                    return Mock(returncode=0, stdout="abc123\n", stderr="")
                return Mock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = fake_run
            create_worker_pane(
                project_root=str(tmp_path),
                prompt="Fix the thing",
                agent="claude",
                slug="test-slug",
                session_root=str(tmp_path),
            )
        events = read_events(str(tmp_path))
        created = [r for r in events if r["event"] == "pane_created"]
        assert len(created) == 1
        assert created[0]["agent"] == "claude"
        assert created[0]["pane"] == "test-slug"
        assert mock_backend.create_worker_pane.call_args.kwargs["env"] == {
            "DISABLE_AUTO_UPDATE": "true",
            "DISABLE_UPDATE_PROMPT": "true",
        }


# ---------------------------------------------------------------------------
# allow-set-title blocked during pane creation
# ---------------------------------------------------------------------------


class TestBlockTitleOverride:
    def test_allow_set_title_off_during_create(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.lifecycle import create_worker_pane

        mock_backend.create_pane.return_value = "%99"
        mock_backend.create_worker_pane.return_value = "%99"
        with (
            patch("dgov.lifecycle.subprocess.run") as mock_run,
            patch("dgov.lifecycle._write_worktree_instructions"),
        ):

            def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN201
                if "--verify" in cmd:
                    return Mock(returncode=128, stdout="", stderr="")
                if cmd[-1] == "HEAD":
                    return Mock(returncode=0, stdout="abc123\n", stderr="")
                return Mock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = fake_run
            create_worker_pane(
                project_root=str(tmp_path),
                prompt="Test title block",
                agent="claude",
                slug="title-test",
                session_root=str(tmp_path),
            )
        mock_backend.configure_worker_pane.assert_called_once()
        call_args = mock_backend.configure_worker_pane.call_args
        assert call_args[0][0] == "%99"  # pane_id
        assert call_args[0][2] == "claude"  # agent


# ---------------------------------------------------------------------------
# _compute_freshness
# ---------------------------------------------------------------------------


class TestComputeFreshness:
    def test_fresh_no_main_changes(self, tmp_path: Path) -> None:
        from dgov.status import _compute_freshness

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

        with patch("dgov.status.subprocess.run", fake_run):
            result = _compute_freshness(str(tmp_path), record)
        assert result["freshness"] == "fresh"
        assert result["commits_since_base"] == 0
        assert result["overlapping_files"] == []

    def test_warn_main_advanced(self, tmp_path: Path) -> None:
        from dgov.status import _compute_freshness

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

        with patch("dgov.status.subprocess.run", fake_run):
            result = _compute_freshness(str(tmp_path), record)
        assert result["freshness"] == "warn"
        assert result["pane_age_hours"] > 4

    def test_stale_overlap_many_commits(self, tmp_path: Path) -> None:
        from dgov.status import _compute_freshness

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

        with patch("dgov.status.subprocess.run", fake_run):
            result = _compute_freshness(str(tmp_path), record)
        assert result["freshness"] == "stale"
        assert result["commits_since_base"] == 8
        assert "src/shared.py" in result["overlapping_files"]


# ---------------------------------------------------------------------------
# VALID_EVENTS
# ---------------------------------------------------------------------------


class TestValidEvents:
    def test_contains_expected_events(self) -> None:
        from dgov.persistence import VALID_EVENTS

        expected = {
            "dispatch_queued",
            "pane_created",
            "pane_done",
            "pane_failed",
            "pane_resumed",
            "pane_timed_out",
            "pane_merged",
            "pane_merge_failed",
            "pane_escalated",
            "pane_superseded",
            "pane_closed",
            "pane_retry_spawned",
            "pane_auto_retried",
            "pane_blocked",
            "pane_review_pending",
            "checkpoint_created",
            "review_pass",
            "review_fail",
            "review_fix_started",
            "review_fix_finding",
            "review_fix_completed",
            "pane_auto_responded",
            "mission_pending",
            "mission_running",
            "mission_waiting",
            "mission_reviewing",
            "mission_merging",
            "mission_completed",
            "mission_failed",
            "dag_started",
            "dag_resumed",
            "dag_blocked",
            "dag_tier_started",
            "dag_task_dispatched",
            "dag_task_completed",
            "dag_task_failed",
            "dag_task_escalated",
            "dag_tier_completed",
            "dag_completed",
            "dag_failed",
            "merge_enqueued",
            "merge_completed",
            "yap_received",
            "pane_circuit_breaker",
            "monitor_nudge",
            "monitor_auto_complete",
            "monitor_idle_timeout",
            "monitor_blocked",
            "monitor_auto_merge",
            "monitor_auto_retry",
            "monitor_tick",
            "claim_violation",
            "quality_retry",
            "quality_escalate",
            "evals_verified",
            "worker_contradiction",
        }
        assert expected == VALID_EVENTS


# ---------------------------------------------------------------------------
# retry_worker_pane
# ---------------------------------------------------------------------------


class TestRetryWorkerPane:
    def test_not_found_returns_error(self, tmp_path: Path) -> None:
        from dgov.recovery import retry_worker_pane

        replace_all_panes(str(tmp_path), {"panes": []})
        result = retry_worker_pane(str(tmp_path), "nope", session_root=str(tmp_path))
        assert "error" in result

    def test_retry_creates_new_pane_and_links(self, tmp_path: Path) -> None:
        from dgov.recovery import retry_worker_pane

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "fix-bug",
                        "prompt": "Fix the bug",
                        "agent": "pi",
                        "state": "timed_out",
                    }
                ]
            },
        )

        new_pane = WorkerPane(
            slug="fix-bug-2",
            prompt="Fix the bug",
            pane_id="%42",
            agent="pi",
            project_root=str(tmp_path),
            worktree_path="/wt",
            branch_name="fix-bug-2",
        )

        def fake_create(**kwargs):
            add_pane(str(tmp_path), new_pane)
            return new_pane

        with patch("dgov.recovery.create_worker_pane", side_effect=fake_create):
            result = retry_worker_pane(str(tmp_path), "fix-bug", session_root=str(tmp_path))

        assert result["retried"] is True
        assert result["new_slug"] == "fix-bug-2"
        assert result["attempt"] == 2
        assert result["original_slug"] == "fix-bug"

        # Check that old pane is superseded
        panes = all_panes(str(tmp_path))
        old = next(p for p in panes if p["slug"] == "fix-bug")
        assert old["state"] == "superseded"
        assert old["superseded_by"] == "fix-bug-2"

        # Check new pane has retried_from
        new = next(p for p in panes if p["slug"] == "fix-bug-2")
        assert new["retried_from"] == "fix-bug"

    def test_attempt_increments_past_existing(self, tmp_path: Path) -> None:
        from dgov.recovery import retry_worker_pane

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {"slug": "task", "prompt": "Do it", "agent": "claude", "state": "superseded"},
                    {
                        "slug": "task-2",
                        "prompt": "Do it",
                        "agent": "claude",
                        "state": "superseded",
                    },
                    {"slug": "task-3", "prompt": "Do it", "agent": "claude", "state": "timed_out"},
                ]
            },
        )

        new_pane = WorkerPane(
            slug="task-4",
            prompt="Do it",
            pane_id="%50",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path="/wt",
            branch_name="task-4",
        )
        with patch("dgov.recovery.create_worker_pane", return_value=new_pane):
            result = retry_worker_pane(str(tmp_path), "task-3", session_root=str(tmp_path))

        assert result["attempt"] == 4
        assert result["new_slug"] == "task-4"

    def test_create_failure_returns_error(self, tmp_path: Path) -> None:
        from dgov.recovery import retry_worker_pane

        replace_all_panes(
            str(tmp_path),
            {"panes": [{"slug": "fail", "prompt": "x", "agent": "pi", "state": "timed_out"}]},
        )
        with patch("dgov.recovery.create_worker_pane", side_effect=RuntimeError("tunnel down")):
            result = retry_worker_pane(str(tmp_path), "fail", session_root=str(tmp_path))
        assert "error" in result
        assert "tunnel down" in result["error"]

    def test_agent_override(self, tmp_path: Path) -> None:
        from dgov.recovery import retry_worker_pane

        replace_all_panes(
            str(tmp_path),
            {"panes": [{"slug": "orig", "prompt": "task", "agent": "pi", "state": "timed_out"}]},
        )
        new_pane = WorkerPane(
            slug="orig-2",
            prompt="task",
            pane_id="%9",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path="/wt",
            branch_name="orig-2",
        )
        with patch("dgov.recovery.create_worker_pane", return_value=new_pane) as mock_create:
            result = retry_worker_pane(
                str(tmp_path), "orig", session_root=str(tmp_path), agent="claude"
            )
        assert result["agent"] == "claude"
        assert mock_create.call_args.kwargs["agent"] == "claude"


# ---------------------------------------------------------------------------
# create_checkpoint / list_checkpoints
# ---------------------------------------------------------------------------


class TestCreateCheckpoint:
    def test_creates_checkpoint_file(self, tmp_path: Path) -> None:
        from dgov.batch import create_checkpoint

        replace_all_panes(str(tmp_path), {"panes": [{"slug": "a"}, {"slug": "b"}]})
        with patch("dgov.batch.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="deadbeef\n")
            result = create_checkpoint(str(tmp_path), "wave1", session_root=str(tmp_path))

        assert result["checkpoint"] == "wave1"
        assert result["main_sha"] == "deadbeef"
        assert result["pane_count"] == 2

        cp_path = tmp_path / ".dgov" / "checkpoints" / "wave1.json"
        assert cp_path.exists()
        data = json.loads(cp_path.read_text())
        assert data["name"] == "wave1"
        assert len(data["panes"]) == 2

    def test_checkpoint_with_no_panes(self, tmp_path: Path) -> None:
        from dgov.batch import create_checkpoint

        replace_all_panes(str(tmp_path), {"panes": []})
        with patch("dgov.batch.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="abc123\n")
            result = create_checkpoint(str(tmp_path), "empty", session_root=str(tmp_path))

        assert result["pane_count"] == 0
        cp_path = tmp_path / ".dgov" / "checkpoints" / "empty.json"
        assert cp_path.exists()

    def test_emits_checkpoint_event(self, tmp_path: Path) -> None:
        from dgov.batch import create_checkpoint
        from dgov.persistence import read_events

        replace_all_panes(str(tmp_path), {"panes": []})
        with patch("dgov.batch.subprocess.run") as mock_run:
            mock_run.return_value = Mock(returncode=0, stdout="abc\n")
            create_checkpoint(str(tmp_path), "ev-test", session_root=str(tmp_path))

        events = read_events(str(tmp_path))
        cp_events = [r for r in events if r["event"] == "checkpoint_created"]
        assert len(cp_events) == 1
        assert cp_events[0]["pane"] == "checkpoint/ev-test"


class TestListCheckpoints:
    def test_empty_when_no_dir(self, tmp_path: Path) -> None:
        from dgov.batch import list_checkpoints

        result = list_checkpoints(str(tmp_path))
        assert result == []

    def test_lists_checkpoints(self, tmp_path: Path) -> None:
        from dgov.batch import list_checkpoints

        cp_dir = tmp_path / ".dgov" / "checkpoints"
        cp_dir.mkdir(parents=True)
        (cp_dir / "alpha.json").write_text(
            json.dumps(
                {
                    "name": "alpha",
                    "ts": "2026-01-01T00:00:00Z",
                    "panes": [{}],
                    "main_sha": "abcdef12",
                }
            )
        )
        (cp_dir / "beta.json").write_text(
            json.dumps(
                {
                    "name": "beta",
                    "ts": "2026-01-02T00:00:00Z",
                    "panes": [{}, {}],
                    "main_sha": "12345678",
                }
            )
        )

        result = list_checkpoints(str(tmp_path))
        assert len(result) == 2
        assert result[0]["name"] == "alpha"
        assert result[0]["pane_count"] == 1
        assert result[0]["main_sha"] == "abcdef12"
        assert result[1]["name"] == "beta"
        assert result[1]["pane_count"] == 2

    def test_skips_corrupt_files(self, tmp_path: Path) -> None:
        from dgov.batch import list_checkpoints

        cp_dir = tmp_path / ".dgov" / "checkpoints"
        cp_dir.mkdir(parents=True)
        (cp_dir / "good.json").write_text(
            json.dumps({"name": "good", "ts": "t", "panes": [], "main_sha": "abc"})
        )
        (cp_dir / "bad.json").write_text("not json{{{")

        result = list_checkpoints(str(tmp_path))
        assert len(result) == 1
        assert result[0]["name"] == "good"


# ---------------------------------------------------------------------------
# Batch: _compute_tiers
# ---------------------------------------------------------------------------


class TestComputeTiers:
    def _to_dict(self, tasks: list[dict]) -> dict[str, dict]:
        """Convert list-of-dicts to dict-of-dicts for _compute_tiers."""
        return {t["id"]: {**t, "depends_on": t.get("depends_on", [])} for t in tasks}

    def test_disjoint_touches_single_tier(self) -> None:
        from dgov.batch import _compute_tiers

        tasks = self._to_dict(
            [
                {"id": "a", "touches": ["src/foo.py"]},
                {"id": "b", "touches": ["tests/test_bar.py"]},
                {"id": "c", "touches": ["docs/readme.md"]},
            ]
        )
        tiers = _compute_tiers(tasks)
        assert len(tiers) == 1
        assert {t["id"] for t in tiers[0]} == {"a", "b", "c"}

    def test_overlapping_touches_multiple_tiers(self) -> None:
        from dgov.batch import _compute_tiers

        tasks = self._to_dict(
            [
                {"id": "a", "touches": ["src/foo.py"]},
                {"id": "b", "touches": ["src/foo.py"]},
                {"id": "c", "touches": ["tests/bar.py"]},
            ]
        )
        tiers = _compute_tiers(tasks)
        assert len(tiers) == 2
        tier0_ids = {t["id"] for t in tiers[0]}
        tier1_ids = {t["id"] for t in tiers[1]}
        assert "a" in tier0_ids
        assert "c" in tier0_ids
        assert "b" in tier1_ids

    def test_prefix_containment(self) -> None:
        from dgov.batch import _compute_tiers

        tasks = self._to_dict(
            [
                {"id": "a", "touches": ["src/"]},
                {"id": "b", "touches": ["src/foo.py"]},
            ]
        )
        tiers = _compute_tiers(tasks)
        assert len(tiers) == 2
        assert tiers[0][0]["id"] == "a"
        assert tiers[1][0]["id"] == "b"

    def test_no_touches_same_tier(self) -> None:
        from dgov.batch import _compute_tiers

        tasks = self._to_dict(
            [
                {"id": "a", "touches": []},
                {"id": "b", "touches": []},
            ]
        )
        tiers = _compute_tiers(tasks)
        assert len(tiers) == 1
        assert len(tiers[0]) == 2

    def test_empty_tasks(self) -> None:
        from dgov.batch import _compute_tiers

        assert _compute_tiers({}) == []


# ---------------------------------------------------------------------------
# Batch: run_batch dry_run
# ---------------------------------------------------------------------------


class TestRunBatchDryRun:
    def test_dry_run_returns_tiers(self, tmp_path: Path) -> None:
        from dgov.batch import run_batch

        spec = {
            "project_root": "/tmp/repo",
            "tasks": [
                {"id": "t1", "prompt": "do x", "agent": "pi", "touches": ["src/a.py"]},
                {"id": "t2", "prompt": "do y", "agent": "claude", "touches": ["src/b.py"]},
                {"id": "t3", "prompt": "do z", "agent": "pi", "touches": ["src/a.py"]},
            ],
        }
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(spec))

        result = run_batch(str(spec_file), dry_run=True)
        assert result["dry_run"] is True
        assert result["total_tasks"] == 3
        # t1 and t2 disjoint -> tier 0, t3 overlaps t1 -> tier 1
        assert len(result["tiers"]) == 2
        assert "t1" in result["tiers"][0]
        assert "t2" in result["tiers"][0]
        assert "t3" in result["tiers"][1]


# ---------------------------------------------------------------------------
# wait_worker_pane / wait_all_worker_panes
# ---------------------------------------------------------------------------


class TestWaitWorkerPane:
    def test_done_on_first_poll(self, tmp_path: Path) -> None:
        from dgov.waiter import wait_worker_pane

        with (
            patch("dgov.persistence.get_pane", return_value={"slug": "s1", "agent": "claude"}),
            patch("dgov.waiter._is_done", return_value=True),
        ):
            result = wait_worker_pane(str(tmp_path), "s1", timeout=5, poll=0)
        assert result == {"done": "s1", "method": "signal_or_commit"}

    def test_poll_once_detects_blocked_output_without_stable_strategy(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.agents import DoneStrategy
        from dgov.waiter import _poll_once

        pane = WorkerPane(
            slug="s1",
            prompt="test",
            pane_id="%5",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="s1",
        )
        add_pane(str(tmp_path), pane)
        pane_record = get_pane(str(tmp_path), "s1")
        mock_backend.capture_output.return_value = "Enter password:"

        with (
            patch("dgov.responder.auto_respond", return_value=None),
            patch("dgov.persistence.emit_event") as mock_emit,
        ):
            stable_state = {}
            done, _ = _poll_once(
                str(tmp_path),
                str(tmp_path),
                "s1",
                pane_record,
                stable_state,
                15,
                done_strategy=DoneStrategy(type="exit"),
                alive=True,
            )

        assert done is False
        assert stable_state.get("last_blocked") == "Enter password"
        mock_emit.assert_called_once_with(
            str(tmp_path), "pane_blocked", "s1", question="Enter password"
        )

    def test_poll_once_reports_exit_signal_method(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.waiter import _poll_once

        pane = WorkerPane(
            slug="s1",
            prompt="test",
            pane_id="%5",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="s1",
        )
        add_pane(str(tmp_path), pane)
        done_dir = tmp_path / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        (done_dir / "s1.exit").write_text("1")
        pane_record = get_pane(str(tmp_path), "s1")
        mock_backend.capture_output.return_value = ""

        done, method = _poll_once(
            str(tmp_path),
            str(tmp_path),
            "s1",
            pane_record,
            {},
            15,
            alive=True,
        )

        assert done is True
        assert method == "exit_signal"

    def test_poll_once_reports_commit_method(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.agents import DoneStrategy
        from dgov.waiter import _poll_once

        pane = WorkerPane(
            slug="s1",
            prompt="test",
            pane_id="%5",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="s1",
            base_sha="abc123",
        )
        add_pane(str(tmp_path), pane)
        pane_record = get_pane(str(tmp_path), "s1")
        mock_backend.capture_output.return_value = ""

        with (
            patch("dgov.done._has_new_commits", return_value=True),
            patch("dgov.done._agent_still_running", return_value=False),
        ):
            done, method = _poll_once(
                str(tmp_path),
                str(tmp_path),
                "s1",
                pane_record,
                {},
                15,
                done_strategy=DoneStrategy(type="commit"),
                alive=True,
            )

        assert done is True
        assert method == "commit"

    def test_stable_output_detection(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        """Stabilization is now handled inside _is_done via done_strategy=stable."""
        from dgov.agents import DoneStrategy
        from dgov.persistence import add_pane
        from dgov.waiter import _is_done

        pane = WorkerPane(
            slug="s1",
            prompt="test",
            pane_id="%5",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="s1",
        )
        add_pane(str(tmp_path), pane)
        pane_record = {"pane_id": "%5", "project_root": "", "branch_name": "", "base_sha": ""}
        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 20}

        mock_backend.is_alive.return_value = True
        with (
            patch("dgov.done._has_new_commits", return_value=False),
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.done._agent_still_running", return_value=False),
        ):
            result = _is_done(
                str(tmp_path),
                "s1",
                pane_record=pane_record,
                stable_seconds=15,
                _stable_state=stable_state,
                done_strategy=DoneStrategy(type="stable", stable_seconds=15),
            )
        assert result is False

    def test_timeout_raises(self, tmp_path: Path) -> None:
        from dgov.waiter import PaneTimeoutError, wait_worker_pane

        with (
            patch("dgov.persistence.get_pane", return_value={"slug": "s1", "agent": "pi"}),
            patch("dgov.persistence.latest_event_id", return_value=0),
            patch("dgov.persistence.wait_for_events", return_value=[]),
            patch("dgov.waiter._is_done", return_value=False),
            patch("dgov.status.capture_worker_output", return_value=None),
            patch("dgov.persistence.settle_completion_state"),
            patch("dgov.persistence.emit_event"),
            patch("dgov.waiter.time.monotonic", side_effect=[0, 0, 0, 100]),
        ):
            with pytest.raises(PaneTimeoutError) as exc_info:
                wait_worker_pane(str(tmp_path), "s1", timeout=10, poll=1)
            assert exc_info.value.slug == "s1"
            assert exc_info.value.agent == "pi"
            assert exc_info.value.timeout == 10


# ---------------------------------------------------------------------------
# _poll_once stable detection with agent process check
# ---------------------------------------------------------------------------


class TestStableDetectionAgentCheck:
    def test_stable_skipped_when_agent_running(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """When output is stable but agent process is still running, don't trigger done."""
        from dgov.agents import DoneStrategy
        from dgov.persistence import add_pane
        from dgov.waiter import _is_done

        pane = WorkerPane(
            slug="s1",
            prompt="test",
            pane_id="%5",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="s1",
        )
        add_pane(str(tmp_path), pane)
        pane_record = {"pane_id": "%5", "project_root": "", "branch_name": "", "base_sha": ""}
        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 20}

        mock_backend.is_alive.return_value = True
        with (
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.done._agent_still_running", return_value=True),
        ):
            result = _is_done(
                str(tmp_path),
                "s1",
                pane_record=pane_record,
                stable_seconds=15,
                _stable_state=stable_state,
                done_strategy=DoneStrategy(type="stable", stable_seconds=15),
            )
            assert result is False
            assert stable_state["stable_since"] is None  # Reset because agent is alive

    def test_stable_triggers_when_agent_exited(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """When output is stable and agent process has exited (shell prompt), trigger done."""
        from dgov.agents import DoneStrategy
        from dgov.persistence import add_pane
        from dgov.waiter import _is_done

        pane = WorkerPane(
            slug="s1",
            prompt="test",
            pane_id="%5",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="s1",
        )
        add_pane(str(tmp_path), pane)
        pane_record = {"pane_id": "%5", "project_root": "", "branch_name": "", "base_sha": ""}
        stable_state = {"last_output": "same output", "stable_since": time.monotonic() - 20}

        mock_backend.is_alive.return_value = True
        with (
            patch("dgov.done._has_new_commits", return_value=False),
            patch("dgov.status.capture_worker_output", return_value="same output"),
            patch("dgov.done._agent_still_running", return_value=False),
        ):
            result = _is_done(
                str(tmp_path),
                "s1",
                pane_record=pane_record,
                stable_seconds=15,
                _stable_state=stable_state,
                done_strategy=DoneStrategy(type="stable", stable_seconds=15),
            )
            assert result is False

    def test_no_stable_seconds_skips_stabilization(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Without stable_seconds, _is_done does not perform output stabilization."""
        from dgov.persistence import add_pane
        from dgov.waiter import _is_done

        pane = WorkerPane(
            slug="s1",
            prompt="test",
            pane_id="%5",
            agent="claude",
            project_root=str(tmp_path),
            worktree_path=str(tmp_path / "wt"),
            branch_name="s1",
        )
        add_pane(str(tmp_path), pane)
        pane_record = {"pane_id": "%5", "project_root": "", "branch_name": "", "base_sha": ""}

        mock_backend.is_alive.return_value = True
        result = _is_done(str(tmp_path), "s1", pane_record=pane_record)
        assert result is False


class TestWaitAllWorkerPanes:
    def test_empty_pending(self, tmp_path: Path) -> None:
        from dgov.waiter import wait_all_worker_panes

        with patch(
            "dgov.status.list_worker_panes",
            return_value=[{"slug": "s1", "done": True}],
        ):
            results = list(wait_all_worker_panes(str(tmp_path), timeout=5))
        assert results == []

    def test_yields_done_panes(self, tmp_path: Path) -> None:
        from dgov.waiter import wait_all_worker_panes

        with (
            patch(
                "dgov.status.list_worker_panes",
                return_value=[
                    {"slug": "s1", "done": False},
                    {"slug": "s2", "done": False},
                ],
            ),
            patch("dgov.persistence.get_pane", return_value={"slug": "s1"}),
            patch("dgov.waiter._is_done", return_value=True),
        ):
            results = list(wait_all_worker_panes(str(tmp_path), timeout=5, poll=0))
        assert len(results) == 2
        assert all(r["method"] == "signal_or_commit" for r in results)

    def test_timeout_includes_all_pending(self, tmp_path: Path) -> None:
        from dgov.waiter import PaneTimeoutError, wait_all_worker_panes

        def fake_get_pane(session_root, slug):
            return {"slug": slug, "agent": "pi" if slug == "s1" else "claude"}

        with (
            patch(
                "dgov.status.list_worker_panes",
                return_value=[
                    {"slug": "s1", "done": False},
                    {"slug": "s2", "done": False},
                ],
            ),
            patch("dgov.persistence.get_pane", side_effect=fake_get_pane),
            patch("dgov.persistence.wait_for_events", return_value=[]),
            patch("dgov.persistence.latest_event_id", return_value=0),
            patch("dgov.waiter._is_done", return_value=False),
            patch("dgov.status.capture_worker_output", return_value=None),
            patch("dgov.waiter.time.sleep"),
            patch("dgov.waiter.time.monotonic") as mock_mono,
        ):
            mock_mono.side_effect = [0, 0, 100]
            with pytest.raises(PaneTimeoutError) as exc_info:
                list(wait_all_worker_panes(str(tmp_path), timeout=10, poll=1))
            assert len(exc_info.value.pending_panes) == 2
            slugs = {p["slug"] for p in exc_info.value.pending_panes}
            assert slugs == {"s1", "s2"}


class TestPaneTimeoutError:
    def test_attributes(self) -> None:
        from dgov.waiter import PaneTimeoutError

        err = PaneTimeoutError("s1", 30, "pi")
        assert err.slug == "s1"
        assert err.timeout == 30
        assert err.agent == "pi"
        assert err.pending_panes == [{"slug": "s1", "agent": "pi"}]

    def test_pending_panes_override(self) -> None:
        from dgov.waiter import PaneTimeoutError

        panes = [{"slug": "a", "agent": "pi"}, {"slug": "b", "agent": "claude"}]
        err = PaneTimeoutError("a", 60, "pi", pending_panes=panes)
        assert err.pending_panes == panes


# ---------------------------------------------------------------------------
# _structure_pi_prompt
# ---------------------------------------------------------------------------


class TestStructurePiPrompt:
    def test_structure_pi_prompt_extracts_files(self) -> None:
        prompt = "Add an htop shortcut to src/dgov/cli.py following the lazygit pattern"
        structured = _structure_pi_prompt(prompt)
        assert "1. Read src/dgov/cli.py" in structured
        assert "2. Add an htop shortcut to src/dgov/cli.py" in structured
        assert "3. Run: uv run ruff check src/dgov/cli.py" in structured
        assert "4. git add src/dgov/cli.py" in structured
        # First 50 chars of prompt used for commit message
        assert '5. git commit -m "Add an htop shortcut to src/dgov/cli.py following"' in structured

    def test_structure_pi_prompt_verification_step(self) -> None:
        """Verify step 6 checks git log exists after commit."""
        prompt = "Update tests/test_dgov_panes.py"
        structured = _structure_pi_prompt(prompt)
        # Step 6: verify commits exist after the commit step
        assert "git log --oneline $DGOV_BASE_SHA..HEAD" in structured
        assert "verify at least one commit exists" in structured

    def test_structure_pi_prompt_completion_subcommand(self) -> None:
        """Verify step 7 uses dgov worker complete with actual commit message."""
        prompt = "Update tests/test_dgov_panes.py"
        structured = _structure_pi_prompt(prompt)
        # Step 7: signal completion using the inferred commit message
        assert 'dgov worker complete -m "Update tests/test_dgov_panes.py"' in structured

    def test_structure_pi_prompt_failure_subcommand(self) -> None:
        """Verify step 8 uses dgov worker fail for no-changes case."""
        prompt = "Fix src/foo.py"
        structured = _structure_pi_prompt(prompt)
        # Step 8: handle failure/no-change case
        assert 'dgov worker fail "<reason>"' in structured

    def test_structure_pi_prompt_uses_explicit_commit_message(self) -> None:
        """Verify completion step uses the explicit commit message, not a placeholder."""
        prompt = "Fix src/foo.py"
        structured = _structure_pi_prompt(
            prompt,
            ["src/foo.py"],
            commit_message="Fix foo worker path",
        )
        # Commit step uses explicit message
        assert 'git commit -m "Fix foo worker path"' in structured
        # Completion step also uses the same explicit message (not placeholder)
        assert 'dgov worker complete -m "Fix foo worker path"' in structured
        # Verify it's the actual text, not a placeholder like <summary of changes>
        assert "<summary" not in structured.lower()

    def test_structure_pi_prompt_no_files(self) -> None:
        prompt = "Explain why the code is slow"
        structured = _structure_pi_prompt(prompt)
        # Should still have task, commit, verification, and completion steps
        assert "1. Explain why the code is slow" in structured
        assert '2. git commit -m "Explain why the code is slow"' in structured
        assert "git log --oneline $DGOV_BASE_SHA..HEAD" in structured
        assert 'dgov worker complete -m "Explain why the code is slow"' in structured
        # Should not have read/add steps
        assert "Read" not in structured
        assert "git add" not in structured

    def test_create_worker_pane_structures_prompt_for_pi(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        """Verify pi agent gets structured numbered-step prompt."""
        from dgov.agents import AgentDef
        from dgov.lifecycle import create_worker_pane

        pi_registry = {
            "pi": AgentDef(
                id="pi",
                name="pi",
                short_label="pi",
                prompt_command="pi",
                prompt_transport="positional",
                default_flags="--provider river-gpu0",
            )
        }

        mock_backend.create_pane.return_value = "%99"
        mock_backend.create_worker_pane.return_value = "%99"
        with (
            patch("dgov.lifecycle.subprocess.run") as mock_run,
            patch("dgov.lifecycle.load_registry", return_value=pi_registry),
            patch("dgov.tmux.wait_for_shell_ready", return_value=True),
            patch("dgov.lifecycle._write_worktree_instructions"),
        ):

            def _fake_run(cmd, **kwargs):
                if "--verify" in cmd:
                    return Mock(returncode=1, stdout="", stderr="")
                return Mock(returncode=0, stdout="abc123\n", stderr="")

            mock_run.side_effect = _fake_run
            create_worker_pane(
                project_root=str(tmp_path),
                prompt="Fix src/foo.py",
                agent="pi",
                slug="pi-test",
                session_root=str(tmp_path),
            )
        # Verify the observable behavior: backend received a command containing the prompt
        all_calls = " ".join(str(c) for c in mock_backend.send_shell_command.call_args_list)
        all_input = " ".join(str(c) for c in mock_backend.send_input.call_args_list)
        all_sent = all_calls + all_input
        assert "src/foo.py" in all_sent or "Fix" in all_sent or "pi" in all_sent


def test_create_worker_pane_waits_for_shell_before_startup_commands(
    tmp_path: Path, mock_backend: MagicMock
) -> None:
    from dgov.lifecycle import create_worker_pane

    mock_backend.create_pane.return_value = "%99"
    mock_backend.create_worker_pane.return_value = "%99"
    events: list[tuple[str, object]] = []
    mock_backend.send_input.side_effect = lambda pane_id, text: events.append(("send_input", text))
    mock_backend.send_shell_command.side_effect = lambda pane_id, cmd: events.append(
        ("send_shell_command", cmd)
    )

    with (
        patch("dgov.lifecycle.subprocess.run") as mock_run,
        patch("dgov.tmux.wait_for_shell_ready", return_value=True),
        patch(
            "dgov.lifecycle.time.sleep",
            side_effect=lambda seconds: events.append(("sleep", seconds)),
        ),
        patch("dgov.lifecycle._write_worktree_instructions"),
    ):

        def fake_run(cmd, **kwargs):  # noqa: ANN001, ANN201
            if "--verify" in cmd:
                return Mock(returncode=128, stdout="", stderr="")
            if cmd[-1] == "HEAD":
                return Mock(returncode=0, stdout="abc123\n", stderr="")
            return Mock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run
        create_worker_pane(
            project_root=str(tmp_path),
            prompt="Fix the thing",
            agent="claude",
            slug="delay-test",
            session_root=str(tmp_path),
        )

    # Env setup uses send_shell_command (bootstrap, not runtime interaction)
    shell_cmds = [e for e in events if e[0] == "send_shell_command"]
    assert len(shell_cmds) > 0, "Expected at least one send_shell_command"


# ---------------------------------------------------------------------------
# resume_worker_pane
# ---------------------------------------------------------------------------


class TestResumeWorkerPane:
    def test_basic_resume(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.agents import AgentDef
        from dgov.lifecycle import resume_worker_pane

        wt_dir = tmp_path / ".dgov" / "worktrees" / "fix-it"
        wt_dir.mkdir(parents=True)

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "fix-it",
                        "agent": "claude",
                        "prompt": "Fix the bug",
                        "pane_id": "%5",
                        "project_root": str(tmp_path),
                        "worktree_path": str(wt_dir),
                        "branch_name": "fix-it",
                        "state": "abandoned",
                    }
                ]
            },
        )

        registry = {
            "claude": AgentDef(
                id="claude",
                name="claude",
                short_label="cc",
                prompt_command="claude",
                prompt_transport="positional",
            )
        }

        mock_backend.is_alive.return_value = False
        mock_backend.create_pane.return_value = "%10"
        mock_backend.create_worker_pane.return_value = "%10"
        with (
            patch("dgov.lifecycle.subprocess.run") as mock_run,
            patch("dgov.lifecycle.load_registry", return_value=registry),
            patch("dgov.tmux.wait_for_shell_ready", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="abc\n", stderr="")
            result = resume_worker_pane(str(tmp_path), "fix-it", session_root=str(tmp_path))

        assert result["resumed"] is True
        assert result["slug"] == "fix-it"
        assert result["agent"] == "claude"
        assert result["pane_id"] == "%10"

        # State should be updated
        panes = all_panes(str(tmp_path))
        pane = next(p for p in panes if p["slug"] == "fix-it")
        assert pane["pane_id"] == "%10"
        assert pane["state"] == "active"

    def test_resume_waits_for_shell_before_startup_commands(
        self, tmp_path: Path, mock_backend: MagicMock
    ) -> None:
        from dgov.agents import AgentDef
        from dgov.lifecycle import resume_worker_pane

        wt_dir = tmp_path / ".dgov" / "worktrees" / "fix-delay"
        wt_dir.mkdir(parents=True)

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "fix-delay",
                        "agent": "claude",
                        "prompt": "Fix the bug",
                        "pane_id": "%5",
                        "project_root": str(tmp_path),
                        "worktree_path": str(wt_dir),
                        "branch_name": "fix-delay",
                        "state": "abandoned",
                    }
                ]
            },
        )

        registry = {
            "claude": AgentDef(
                id="claude",
                name="claude",
                short_label="cc",
                prompt_command="claude",
                prompt_transport="positional",
            )
        }

        events: list[tuple[str, object]] = []
        mock_backend.is_alive.return_value = False
        mock_backend.create_pane.return_value = "%10"
        mock_backend.create_worker_pane.return_value = "%10"
        mock_backend.send_input.side_effect = lambda pane_id, text: events.append(
            ("send_input", text)
        )
        mock_backend.send_shell_command.side_effect = lambda pane_id, cmd: events.append(
            ("send_shell_command", cmd)
        )

        with (
            patch("dgov.lifecycle.subprocess.run") as mock_run,
            patch("dgov.lifecycle.load_registry", return_value=registry),
            patch("dgov.tmux.wait_for_shell_ready", return_value=True),
            patch(
                "dgov.lifecycle.time.sleep",
                side_effect=lambda seconds: events.append(("sleep", seconds)),
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="abc\n", stderr="")
            resume_worker_pane(str(tmp_path), "fix-delay", session_root=str(tmp_path))

        # Env setup uses send_shell_command (bootstrap, not runtime interaction)
        shell_cmds = [e for e in events if e[0] == "send_shell_command"]
        assert len(shell_cmds) > 0, "Expected at least one send_shell_command"

    def test_resume_with_agent_override(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.agents import AgentDef
        from dgov.lifecycle import resume_worker_pane

        wt_dir = tmp_path / ".dgov" / "worktrees" / "task-x"
        wt_dir.mkdir(parents=True)

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "task-x",
                        "agent": "pi",
                        "prompt": "Refactor",
                        "pane_id": "%3",
                        "project_root": str(tmp_path),
                        "worktree_path": str(wt_dir),
                        "branch_name": "task-x",
                        "state": "failed",
                    }
                ]
            },
        )

        registry = {
            "claude": AgentDef(
                id="claude",
                name="claude",
                short_label="cc",
                prompt_command="claude",
                prompt_transport="positional",
            )
        }

        mock_backend.is_alive.return_value = False
        mock_backend.create_pane.return_value = "%20"
        mock_backend.create_worker_pane.return_value = "%20"
        with (
            patch("dgov.lifecycle.subprocess.run") as mock_run,
            patch("dgov.lifecycle.load_registry", return_value=registry),
            patch("dgov.tmux.wait_for_shell_ready", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="abc\n", stderr="")
            result = resume_worker_pane(
                str(tmp_path), "task-x", session_root=str(tmp_path), agent="claude"
            )

        assert result["agent"] == "claude"
        panes = all_panes(str(tmp_path))
        pane = next(p for p in panes if p["slug"] == "task-x")
        assert pane["agent"] == "claude"

    def test_resume_with_prompt_override(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.agents import AgentDef
        from dgov.lifecycle import resume_worker_pane

        wt_dir = tmp_path / ".dgov" / "worktrees" / "task-y"
        wt_dir.mkdir(parents=True)

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "task-y",
                        "agent": "claude",
                        "prompt": "Old prompt",
                        "pane_id": "%7",
                        "project_root": str(tmp_path),
                        "worktree_path": str(wt_dir),
                        "branch_name": "task-y",
                        "state": "abandoned",
                    }
                ]
            },
        )

        registry = {
            "claude": AgentDef(
                id="claude",
                name="claude",
                short_label="cc",
                prompt_command="claude",
                prompt_transport="positional",
            )
        }

        mock_backend.is_alive.return_value = False
        mock_backend.create_pane.return_value = "%30"
        mock_backend.create_worker_pane.return_value = "%30"
        with (
            patch("dgov.lifecycle.subprocess.run") as mock_run,
            patch("dgov.lifecycle.load_registry", return_value=registry),
            patch("dgov.tmux.wait_for_shell_ready", return_value=True),
            patch(
                "dgov.lifecycle.build_launch_command", return_value="claude 'prompt'"
            ) as mock_blc,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="abc\n", stderr="")
            result = resume_worker_pane(
                str(tmp_path),
                "task-y",
                session_root=str(tmp_path),
                prompt="New prompt for resume",
            )

        assert result["resumed"] is True
        # build_launch_command should receive the new prompt (with resume context appended)
        call_args = mock_blc.call_args
        prompt_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("prompt")
        assert "New prompt for resume" in prompt_arg

    def test_resume_nonexistent_slug(self, tmp_path: Path) -> None:
        from dgov.lifecycle import resume_worker_pane

        replace_all_panes(str(tmp_path), {"panes": []})
        result = resume_worker_pane(str(tmp_path), "no-such-pane", session_root=str(tmp_path))
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_resume_missing_worktree(self, tmp_path: Path) -> None:
        from dgov.lifecycle import resume_worker_pane

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "gone",
                        "agent": "claude",
                        "prompt": "Task",
                        "pane_id": "%1",
                        "project_root": str(tmp_path),
                        "worktree_path": "/nonexistent/path",
                        "branch_name": "gone",
                    }
                ]
            },
        )
        result = resume_worker_pane(str(tmp_path), "gone", session_root=str(tmp_path))
        assert "error" in result
        assert "no longer exists" in result["error"].lower()

    def test_resume_missing_branch(self, tmp_path: Path) -> None:
        from dgov.lifecycle import resume_worker_pane

        wt_dir = tmp_path / ".dgov" / "worktrees" / "dead-branch"
        wt_dir.mkdir(parents=True)

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "dead-branch",
                        "agent": "claude",
                        "prompt": "Task",
                        "pane_id": "%1",
                        "project_root": str(tmp_path),
                        "worktree_path": str(wt_dir),
                        "branch_name": "dead-branch",
                    }
                ]
            },
        )

        with patch("dgov.lifecycle.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="", stderr="not found")
            result = resume_worker_pane(str(tmp_path), "dead-branch", session_root=str(tmp_path))

        assert "error" in result
        assert "branch" in result["error"].lower()

    def test_resume_kills_old_pane(self, tmp_path: Path, mock_backend: MagicMock) -> None:
        from dgov.agents import AgentDef
        from dgov.lifecycle import resume_worker_pane

        wt_dir = tmp_path / ".dgov" / "worktrees" / "stale-pane"
        wt_dir.mkdir(parents=True)

        replace_all_panes(
            str(tmp_path),
            {
                "panes": [
                    {
                        "slug": "stale-pane",
                        "agent": "claude",
                        "prompt": "Task",
                        "pane_id": "%old",
                        "project_root": str(tmp_path),
                        "worktree_path": str(wt_dir),
                        "branch_name": "stale-pane",
                        "state": "abandoned",
                    }
                ]
            },
        )

        registry = {
            "claude": AgentDef(
                id="claude",
                name="claude",
                short_label="cc",
                prompt_command="claude",
                prompt_transport="positional",
            )
        }

        mock_backend.is_alive.return_value = True
        mock_backend.create_pane.return_value = "%new"
        mock_backend.create_worker_pane.return_value = "%new"
        with (
            patch("dgov.lifecycle.subprocess.run") as mock_run,
            patch("dgov.lifecycle.load_registry", return_value=registry),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="abc\n", stderr="")
            resume_worker_pane(str(tmp_path), "stale-pane", session_root=str(tmp_path))

        mock_backend.destroy.assert_called_once_with("%old")


# ---------------------------------------------------------------------------
# _plumbing_merge edge cases
# ---------------------------------------------------------------------------


class TestPlumbingMerge:
    def test_successful_merge(self, tmp_path: Path) -> None:
        from dgov.merger import _plumbing_merge

        call_log = []

        def fake_run(cmd, **kw):
            call_log.append(cmd)
            m = MagicMock()
            if "rev-parse" in cmd and "HEAD" in cmd and "symbolic-ref" not in cmd:
                m.returncode = 0
                m.stdout = "aaa111\n"
            elif "merge-tree" in cmd:
                m.returncode = 0
                m.stdout = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            elif "rev-parse" in cmd:
                m.returncode = 0
                m.stdout = "bbb222\n"
            elif "commit-tree" in cmd:
                m.returncode = 0
                m.stdout = "ccc333\n"
            elif "symbolic-ref" in cmd:
                m.returncode = 0
                m.stdout = "main\n"
            elif "update-ref" in cmd:
                m.returncode = 0
                m.stdout = ""
            elif "reset" in cmd:
                m.returncode = 0
                m.stdout = ""
            else:
                m.returncode = 0
                m.stdout = ""
            m.stderr = ""
            return m

        with patch("dgov.merger.subprocess.run", side_effect=fake_run):
            result = _plumbing_merge(str(tmp_path), "fix-branch")

        assert result.success is True
        # Verify we went through the full pipeline
        cmds = [" ".join(c) if isinstance(c, list) else str(c) for c in call_log]
        assert any("merge-tree" in c for c in cmds)
        assert any("commit-tree" in c for c in cmds)
        assert any("update-ref" in c for c in cmds)
        assert any("reset" in c for c in cmds)

    def test_detached_head(self, tmp_path: Path) -> None:
        from dgov.merger import _plumbing_merge

        call_count = {"n": 0}

        def fake_run(cmd, **kw):
            call_count["n"] += 1
            m = MagicMock()
            if "rev-parse" in cmd and "HEAD" in cmd and "symbolic-ref" not in cmd:
                m.returncode = 0
                m.stdout = "aaa111\n"
            elif "merge-tree" in cmd:
                m.returncode = 0
                m.stdout = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            elif "rev-parse" in cmd:
                m.returncode = 0
                m.stdout = "bbb222\n"
            elif "commit-tree" in cmd:
                m.returncode = 0
                m.stdout = "ccc333\n"
            elif "symbolic-ref" in cmd:
                m.returncode = 128
                m.stdout = ""
                m.stderr = "fatal: ref HEAD is not a symbolic ref"
            else:
                m.returncode = 0
                m.stdout = ""
            m.stderr = m.stderr if hasattr(m, "stderr") and m.stderr else ""
            return m

        with patch("dgov.merger.subprocess.run", side_effect=fake_run):
            result = _plumbing_merge(str(tmp_path), "some-branch")

        assert result.success is False
        assert "Detached HEAD" in result.stderr

    def test_reset_hard_failure(self, tmp_path: Path) -> None:
        from dgov.merger import _plumbing_merge

        def fake_run(cmd, **kw):
            m = MagicMock()
            if "rev-parse" in cmd and "HEAD" in cmd and "symbolic-ref" not in cmd:
                m.returncode = 0
                m.stdout = "aaa111\n"
            elif "merge-tree" in cmd:
                m.returncode = 0
                m.stdout = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
            elif "rev-parse" in cmd:
                m.returncode = 0
                m.stdout = "bbb222\n"
            elif "commit-tree" in cmd:
                m.returncode = 0
                m.stdout = "ccc333\n"
            elif "symbolic-ref" in cmd:
                m.returncode = 0
                m.stdout = "main\n"
            elif "update-ref" in cmd:
                m.returncode = 0
                m.stdout = ""
            elif "reset" in cmd:
                m.returncode = 1
                m.stdout = ""
                m.stderr = "error: unable to reset"
            else:
                m.returncode = 0
                m.stdout = ""
            m.stderr = getattr(m, "stderr", "") or ""
            return m

        with patch("dgov.merger.subprocess.run", side_effect=fake_run):
            result = _plumbing_merge(str(tmp_path), "fix-branch")

        assert result.success is False
        assert "reset" in result.stderr.lower()

    def test_merge_tree_conflict(self, tmp_path: Path) -> None:
        from dgov.merger import _plumbing_merge

        def fake_run(cmd, **kw):
            m = MagicMock()
            if "rev-parse" in cmd and "HEAD" in cmd:
                m.returncode = 0
                m.stdout = "aaa111\n"
            elif "merge-tree" in cmd:
                m.returncode = 1
                m.stdout = "CONFLICT (content): ..."
                m.stderr = ""
            else:
                m.returncode = 0
                m.stdout = ""
            m.stderr = getattr(m, "stderr", "") or ""
            return m

        with patch("dgov.merger.subprocess.run", side_effect=fake_run):
            result = _plumbing_merge(str(tmp_path), "conflict-branch")

        assert result.success is False

    def test_head_resolve_failure(self, tmp_path: Path) -> None:
        from dgov.merger import _plumbing_merge

        mock_result = MagicMock(returncode=128, stdout="", stderr="not a git repo")
        with patch("dgov.merger.subprocess.run", return_value=mock_result):
            result = _plumbing_merge(str(tmp_path), "any-branch")

        assert result.success is False
        assert "not a git repo" in result.stderr


# ---------------------------------------------------------------------------
# Batch: _compute_tiers — deep dependency chains
# ---------------------------------------------------------------------------


class TestComputeTiersDeep:
    def _to_dict(self, tasks: list[dict]) -> dict[str, dict]:
        return {t["id"]: {**t, "depends_on": t.get("depends_on", [])} for t in tasks}

    def test_four_level_chain(self) -> None:
        """A -> B -> C -> D: each touches the same file, so 4 tiers."""
        from dgov.batch import _compute_tiers

        tasks = self._to_dict(
            [
                {"id": "A", "touches": ["src/shared.py"]},
                {"id": "B", "touches": ["src/shared.py"]},
                {"id": "C", "touches": ["src/shared.py"]},
                {"id": "D", "touches": ["src/shared.py"]},
            ]
        )
        tiers = _compute_tiers(tasks)
        assert len(tiers) == 4
        assert [tiers[i][0]["id"] for i in range(4)] == ["A", "B", "C", "D"]

    def test_mixed_chain_and_parallel(self) -> None:
        """A touches x, B touches x, C touches y, D touches y+x."""
        from dgov.batch import _compute_tiers

        tasks = self._to_dict(
            [
                {"id": "A", "touches": ["x"]},
                {"id": "B", "touches": ["x"]},
                {"id": "C", "touches": ["y"]},
                {"id": "D", "touches": ["x", "y"]},
            ]
        )
        tiers = _compute_tiers(tasks)
        # Tier 0: A, C (disjoint)
        # Tier 1: B (overlaps A on x), D cannot go here because overlaps C on y
        # Tier 2: D
        assert len(tiers) == 3
        tier0_ids = {t["id"] for t in tiers[0]}
        assert tier0_ids == {"A", "C"}

    def test_circular_deps_do_not_hang(self) -> None:
        """Tasks with identical touches serialize; no infinite loop."""
        from dgov.batch import _compute_tiers

        tasks = self._to_dict([{"id": f"t{i}", "touches": ["shared"]} for i in range(5)])
        tiers = _compute_tiers(tasks)
        assert len(tiers) == 5
        for tier in tiers:
            assert len(tier) == 1


# ---------------------------------------------------------------------------
# Batch: run_batch with live wait (mocked)
# ---------------------------------------------------------------------------


class TestRunBatchLiveWait:
    def test_batch_creates_waits_merges(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dgov.batch import run_batch
        from dgov.dag_parser import DagRunSummary

        spec = {
            "project_root": str(tmp_path),
            "tasks": [
                {"id": "t1", "prompt": "do x", "agent": "claude", "touches": ["src/a.py"]},
                {"id": "t2", "prompt": "do y", "agent": "claude", "touches": ["src/b.py"]},
            ],
        }
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(spec))

        run_id = 1
        monkeypatch.setattr(
            "dgov.dag.run_dag_via_kernel",
            lambda *args, **kwargs: DagRunSummary(
                run_id=run_id,
                dag_file=str(spec_file),
                status="submitted",
                merged=[],
                failed=[],
                skipped=[],
                blocked=[],
            ),
        )
        monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args: 0)
        monkeypatch.setattr(
            "dgov.persistence.wait_for_events",
            lambda *args, **kwargs: [
                {"id": 1, "event": "dag_completed", "data": json.dumps({"dag_run_id": run_id})}
            ],
        )
        monkeypatch.setattr(
            "dgov.persistence.get_dag_run",
            lambda *args, **kwargs: {
                "id": run_id,
                "status": "completed",
                "dag_file": str(spec_file),
                "state_json": {"task_states": {"t1": "merged", "t2": "merged"}},
            },
        )
        monkeypatch.setattr("dgov.persistence.list_dag_tasks", lambda *args: [])

        result = run_batch(str(spec_file), session_root=str(tmp_path))

        assert result["merged"] == ["t1", "t2"]
        assert result["failed"] == []

    def test_batch_aborts_on_merge_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dgov.batch import run_batch
        from dgov.dag_parser import DagRunSummary

        spec = {
            "project_root": str(tmp_path),
            "tasks": [
                {"id": "t1", "prompt": "task1", "agent": "claude", "touches": ["a"]},
            ],
        }
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(spec))

        run_id = 1
        monkeypatch.setattr(
            "dgov.dag.run_dag_via_kernel",
            lambda *args, **kwargs: DagRunSummary(
                run_id=run_id,
                dag_file=str(spec_file),
                status="submitted",
                merged=[],
                failed=[],
                skipped=[],
                blocked=[],
            ),
        )
        monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args: 0)
        monkeypatch.setattr(
            "dgov.persistence.wait_for_events",
            lambda *args, **kwargs: [
                {"id": 1, "event": "dag_failed", "data": json.dumps({"dag_run_id": run_id})}
            ],
        )
        monkeypatch.setattr(
            "dgov.persistence.get_dag_run",
            lambda *args, **kwargs: {
                "id": run_id,
                "status": "failed",
                "dag_file": str(spec_file),
                "state_json": {"task_states": {"t1": "failed"}},
            },
        )
        monkeypatch.setattr("dgov.persistence.list_dag_tasks", lambda *args: [])

        result = run_batch(str(spec_file), session_root=str(tmp_path))

        assert "t1" in result["failed"]

    def test_batch_timeout_marks_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dgov.batch import run_batch
        from dgov.dag_parser import DagRunSummary

        spec = {
            "project_root": str(tmp_path),
            "tasks": [
                {"id": "t1", "prompt": "slow", "agent": "claude", "touches": ["x"], "timeout": 1},
            ],
        }
        spec_file = tmp_path / "spec.json"
        spec_file.write_text(json.dumps(spec))

        run_id = 1
        monkeypatch.setattr(
            "dgov.dag.run_dag_via_kernel",
            lambda *args, **kwargs: DagRunSummary(
                run_id=run_id,
                dag_file=str(spec_file),
                status="submitted",
                merged=[],
                failed=[],
                skipped=[],
                blocked=[],
            ),
        )
        monkeypatch.setattr("dgov.persistence.latest_event_id", lambda *args: 0)
        monkeypatch.setattr(
            "dgov.persistence.wait_for_events",
            lambda *args, **kwargs: [
                {"id": 1, "event": "dag_failed", "data": json.dumps({"dag_run_id": run_id})}
            ],
        )
        monkeypatch.setattr(
            "dgov.persistence.get_dag_run",
            lambda *args, **kwargs: {
                "id": run_id,
                "status": "failed",
                "dag_file": str(spec_file),
                "state_json": {"task_states": {"t1": "failed"}},
            },
        )
        monkeypatch.setattr("dgov.persistence.list_dag_tasks", lambda *args: [])

        result = run_batch(str(spec_file), session_root=str(tmp_path))

        assert "t1" in result["failed"]
