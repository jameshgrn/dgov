"""Tests for preview engine: noise filtering, signal extraction, phase computation."""

from __future__ import annotations

import json
import time

import pytest

from dgov.status import (
    _compute_phase,
    _extract_summary_from_log,
    _is_noise_line,
    _match_signal,
    _read_progress_json,
)


@pytest.mark.unit
class TestIsNoiseLine:
    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "\t  \t",
            "Type your message...",
            "bypass permissions enabled",
            "shift+tab to add a newline",
            "ctrl+c to cancel",
            "YOLO mode active",
            "/model claude-opus",
            "Press ? for shortcuts",
            "MCP servers connected",
            "Update available: 1.2.3",
            "[Opus 4] thinking...",
            "[Sonnet] ready",
            "Sprouting worker...",
            "Cooking response...",
            "Cooked for 3.2s",
            "$",
            "$ ",
            "# ",
            "> ",
            ">>>",
            # Box-drawing only
            "\u2500\u2502\u250c\u2510",
            # Braille only
            "\u2800\u2801\u2802",
            # Progress bar
            "  45pct |========",
            " 50% |████████",
        ],
    )
    def test_noise_detected(self, line: str):
        assert _is_noise_line(line), f"Expected noise: {line!r}"

    @pytest.mark.parametrize(
        "line",
        [
            "Reading src/dgov/status.py",
            "Edit src/dgov/cli/pane.py",
            "Running pytest tests/test_preview.py -q",
            "git commit -m 'Add feature'",
            "5 passed in 1.2s",
            "def _compute_phase(state, alive):",
            "All checks passed",
        ],
    )
    def test_signal_not_noise(self, line: str):
        assert not _is_noise_line(line), f"Expected signal: {line!r}"


@pytest.mark.unit
class TestMatchSignal:
    def test_reading_file(self):
        assert _match_signal("Reading src/dgov/status.py") == "Reading src/dgov/status.py"

    def test_editing_file(self):
        assert _match_signal("Editing src/dgov/cli/pane.py") == "Editing src/dgov/cli/pane.py"

    def test_writing_file(self):
        assert _match_signal("Writing tests/test_preview.py") == "Writing tests/test_preview.py"

    def test_creating_file(self):
        assert _match_signal("Creating new_file.py") == "Writing new_file.py"

    def test_running_pytest(self):
        result = _match_signal("Running pytest tests/ -q")
        assert result is not None
        assert result.startswith("Testing:")

    def test_running_ruff(self):
        result = _match_signal("Running ruff check src/")
        assert result is not None
        assert result.startswith("Linting:")

    def test_running_uv(self):
        result = _match_signal("Running uv sync")
        assert result is not None
        assert result.startswith("Running:")

    def test_git_add(self):
        result = _match_signal("git add src/dgov/status.py")
        assert result is not None
        assert result.startswith("Staging:")

    def test_git_commit(self):
        result = _match_signal("git commit -m 'feat'")
        assert result == "Committing"

    def test_tests_passed(self):
        assert _match_signal("12 passed in 3.4s") == "12 tests passed"

    def test_lint_clean(self):
        assert _match_signal("All checks passed") == "Lint clean"

    def test_files_changed(self):
        assert _match_signal("3 files changed, 50 insertions") == "3 files changed"

    def test_no_match(self):
        assert _match_signal("just a random line of code") is None


@pytest.mark.unit
class TestExtractSummary:
    def _make_log(self, tmp_path, slug: str, content: str) -> str:
        log_dir = tmp_path / ".dgov" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / f"{slug}.log").write_text(content)
        return str(tmp_path)

    def test_empty_log(self, tmp_path):
        root = self._make_log(tmp_path, "empty", "")
        assert _extract_summary_from_log(root, "empty") == ""

    def test_signal_extracted(self, tmp_path):
        content = "some noise\nReading src/dgov/status.py\n"
        root = self._make_log(tmp_path, "sig", content)
        result = _extract_summary_from_log(root, "sig")
        assert result == "Reading src/dgov/status.py"

    def test_noise_skipped(self, tmp_path):
        content = "Editing foo.py\nType your message...\n   \n"
        root = self._make_log(tmp_path, "skip", content)
        result = _extract_summary_from_log(root, "skip")
        assert result == "Editing foo.py"

    def test_truncated_fallback(self, tmp_path):
        long_line = "x" * 200
        content = f"{long_line}\n"
        root = self._make_log(tmp_path, "long", content)
        result = _extract_summary_from_log(root, "long")
        assert len(result) <= 60

    def test_missing_log(self, tmp_path):
        assert _extract_summary_from_log(str(tmp_path), "missing") == ""

    def test_all_noise(self, tmp_path):
        content = "   \nType your message\n$ \n"
        root = self._make_log(tmp_path, "allnoise", content)
        assert _extract_summary_from_log(root, "allnoise") == ""

    def test_prefers_bottom_signal(self, tmp_path):
        content = "Reading old_file.py\nsome random stuff\n12 passed in 2s\n"
        root = self._make_log(tmp_path, "bottom", content)
        result = _extract_summary_from_log(root, "bottom")
        assert result == "12 tests passed"


@pytest.mark.unit
class TestComputePhase:
    def test_terminal_failed(self):
        assert _compute_phase("failed", False, True, 100, "") == "failed"

    def test_terminal_merged(self):
        assert _compute_phase("merged", False, True, 100, "") == "merged"

    def test_terminal_closed(self):
        assert _compute_phase("closed", False, True, 100, "") == "closed"

    def test_terminal_superseded(self):
        assert _compute_phase("superseded", False, True, 100, "") == "closed"

    def test_terminal_escalated(self):
        assert _compute_phase("escalated", False, True, 100, "") == "failed"

    def test_terminal_timed_out(self):
        assert _compute_phase("timed_out", False, True, 100, "") == "failed"

    def test_done(self):
        assert _compute_phase("active", True, True, 100, "") == "done"

    def test_abandoned(self):
        assert _compute_phase("active", False, False, 100, "") == "abandoned"

    def test_starting(self):
        assert _compute_phase("active", True, False, 10, "") == "starting"

    def test_testing(self):
        assert _compute_phase("active", True, False, 60, "Testing: pytest tests/") == "testing"

    def test_committing(self):
        assert _compute_phase("active", True, False, 60, "Staging: foo.py") == "committing"

    def test_working(self):
        assert _compute_phase("active", True, False, 60, "Reading foo.py") == "working"

    def test_idle(self):
        assert _compute_phase("active", True, False, 60, "") == "idle"


@pytest.mark.unit
class TestReadProgressJson:
    def _make_progress(self, tmp_path, slug: str, data: dict, age_s: float = 0) -> str:
        progress_dir = tmp_path / ".dgov" / "progress"
        progress_dir.mkdir(parents=True)
        path = progress_dir / f"{slug}.json"
        path.write_text(json.dumps(data))
        if age_s > 0:
            import os

            old_time = time.time() - age_s
            os.utime(path, (old_time, old_time))
        return str(tmp_path)

    def test_v1_schema(self, tmp_path):
        root = self._make_progress(tmp_path, "v1", {"v": 1, "phase": "working", "message": "hi"})
        result = _read_progress_json(root, "v1")
        assert result == {"phase": "working", "message": "hi"}

    def test_legacy_schema(self, tmp_path):
        root = self._make_progress(
            tmp_path, "leg", {"status": "testing", "message": "running", "turn": 5}
        )
        result = _read_progress_json(root, "leg")
        assert result == {"phase": "testing", "message": "running", "turn": 5}

    def test_stale_progress_ignored(self, tmp_path):
        root = self._make_progress(
            tmp_path, "old", {"v": 1, "phase": "working", "message": "hi"}, age_s=120
        )
        assert _read_progress_json(root, "old") is None

    def test_missing_file(self, tmp_path):
        assert _read_progress_json(str(tmp_path), "nope") is None

    def test_invalid_json(self, tmp_path):
        progress_dir = tmp_path / ".dgov" / "progress"
        progress_dir.mkdir(parents=True)
        (progress_dir / "bad.json").write_text("not json{{{")
        assert _read_progress_json(str(tmp_path), "bad") is None
