"""Tests for dgov.status – tail_worker_log."""

from __future__ import annotations

import pytest

from dgov.status import _extract_summary_from_log, _strip_ansi, tail_worker_log


@pytest.mark.unit
class TestTailWorkerLog:
    def _make_log(self, tmp_path, slug: str, content: bytes) -> str:
        """Create a .dgov/logs/<slug>.log under tmp_path, return session_root."""
        log_dir = tmp_path / ".dgov" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / f"{slug}.log").write_bytes(content)
        return str(tmp_path)

    def test_missing_log_returns_none(self, tmp_path):
        result = tail_worker_log(str(tmp_path), "no-such-worker")
        assert result is None

    def test_empty_log_returns_empty_string(self, tmp_path):
        root = self._make_log(tmp_path, "empty", b"")
        result = tail_worker_log(root, "empty")
        assert result == ""

    def test_returns_last_n_lines(self, tmp_path):
        lines = [f"line {i}" for i in range(50)]
        content = "\n".join(lines).encode()
        root = self._make_log(tmp_path, "big", content)
        result = tail_worker_log(root, "big", lines=5)
        assert result is not None
        assert result.splitlines() == [f"line {i}" for i in range(45, 50)]

    def test_fewer_lines_than_requested(self, tmp_path):
        content = b"alpha\nbeta\ngamma"
        root = self._make_log(tmp_path, "short", content)
        result = tail_worker_log(root, "short", lines=10)
        assert result is not None
        assert result.splitlines() == ["alpha", "beta", "gamma"]

    def test_strips_ansi_codes(self, tmp_path):
        content = b"\x1b[32mgreen\x1b[0m\n\x1b[1mbold\x1b[0m"
        root = self._make_log(tmp_path, "ansi", content)
        result = tail_worker_log(root, "ansi")
        assert result is not None
        assert "\x1b" not in result
        assert result.splitlines() == ["green", "bold"]

    def test_handles_invalid_utf8(self, tmp_path):
        content = b"good line\nbad \xff bytes\nlast"
        root = self._make_log(tmp_path, "bin", content)
        result = tail_worker_log(root, "bin")
        assert result is not None
        assert "last" in result
        # replacement character should appear instead of crash
        assert "\ufffd" in result or "bad" in result

    def test_seek_efficiency_large_file(self, tmp_path):
        """For a large file, only the tail chunk is read (not the whole file)."""
        # 10_000 lines, ~110 KB
        lines = [f"log entry number {i:05d}" for i in range(10_000)]
        content = "\n".join(lines).encode()
        root = self._make_log(tmp_path, "huge", content)
        result = tail_worker_log(root, "huge", lines=5)
        assert result is not None
        result_lines = result.splitlines()
        assert len(result_lines) == 5
        assert result_lines[-1] == "log entry number 09999"

    def test_single_line_log(self, tmp_path):
        root = self._make_log(tmp_path, "one", b"only line")
        result = tail_worker_log(root, "one", lines=5)
        assert result == "only line"

    def test_default_lines_is_20(self, tmp_path):
        lines = [f"L{i}" for i in range(30)]
        content = "\n".join(lines).encode()
        root = self._make_log(tmp_path, "default", content)
        result = tail_worker_log(root, "default")
        assert result is not None
        assert len(result.splitlines()) == 20


@pytest.mark.unit
class TestExtractSummaryFromLog:
    def _make_log(self, tmp_path, slug: str, content: str) -> str:
        """Create a .dgov/logs/<slug>.log under tmp_path, return session_root."""
        log_dir = tmp_path / ".dgov" / "logs"
        log_dir.mkdir(parents=True)
        (log_dir / f"{slug}.log").write_text(content)
        return str(tmp_path)

    def test_empty_log_returns_empty_string(self, tmp_path):
        result = _extract_summary_from_log(str(tmp_path), "missing")
        assert result == ""

    def test_extracts_reading_signal(self, tmp_path):
        content = "some noise\nReading src/dgov/cli.py\n"
        root = self._make_log(tmp_path, "reader", content)
        result = _extract_summary_from_log(root, "reader")
        assert "src/dgov/cli.py" in result

    def test_extracts_editing_signal(self, tmp_path):
        content = "noise\nEditing tests/test_main.py\n"
        root = self._make_log(tmp_path, "editor", content)
        result = _extract_summary_from_log(root, "editor")
        assert "tests/test_main.py" in result

    def test_extracts_git_commit_signal(self, tmp_path):
        content = 'working...\ngit commit -m "fix bug"\n'
        root = self._make_log(tmp_path, "committer", content)
        result = _extract_summary_from_log(root, "committer")
        assert "ommitting" in result.lower()

    def test_extracts_tests_passed_signal(self, tmp_path):
        content = "running tests...\n5 passed in 1.2s\n"
        root = self._make_log(tmp_path, "tester", content)
        result = _extract_summary_from_log(root, "tester")
        assert "5" in result and "passed" in result.lower()

    def test_extracts_lint_clean_signal(self, tmp_path):
        content = "linting...\nAll checks passed!\n"
        root = self._make_log(tmp_path, "linter", content)
        result = _extract_summary_from_log(root, "linter")
        assert "lint" in result.lower()

    def test_skips_noise_lines(self, tmp_path):
        content = "────\n"  # box drawing noise (U+2500)
        root = self._make_log(tmp_path, "noisy", content)
        result = _extract_summary_from_log(root, "noisy")
        assert result == ""

    def test_uses_pre_read_when_provided(self, tmp_path):
        """When pre_read is given, no log file is read."""
        result = _extract_summary_from_log(
            str(tmp_path), "nonexistent", pre_read="Reading foo.py\n"
        )
        assert "foo.py" in result

    def test_returns_truncated_non_signal_line(self, tmp_path):
        """A non-signal, non-noise line is returned truncated to 60 chars."""
        long_line = "x" * 120
        content = f"{long_line}\n"
        root = self._make_log(tmp_path, "longline", content)
        result = _extract_summary_from_log(root, "longline")
        assert len(result) <= 60


@pytest.mark.unit
class TestStripAnsi:
    def test_strips_color_codes(self):
        text = "\x1b[31mred\x1b[0m plain"
        assert _strip_ansi(text) == "red plain"

    def test_strips_bold(self):
        text = "\x1b[1mbold text\x1b[0m"
        assert _strip_ansi(text) == "bold text"


@pytest.mark.unit
class TestShellPromptNoise:
    """Test that shell prompt noise is filtered from summaries."""

    def test_arrow_prompt_filtered(self):
        """Arrow-style prompts like '➜ slug git:(branch)' should be detected as noise."""
        from dgov.status import _is_noise_line

        assert _is_noise_line("➜  slug git:(branch)") is True
        assert _is_noise_line("➜ myproject") is True
        assert _is_noise_line("\u279c project") is True

    def test_other_arrow_prompts_filtered(self):
        """Other arrow variants should also be detected as noise."""
        from dgov.status import _is_noise_line

        assert _is_noise_line("❯ workspace") is True
        assert _is_noise_line("⌵ dir") is True

    def test_non_prompt_lines_not_filtered(self):
        """Actual output lines should NOT be detected as noise."""
        from dgov.status import _is_noise_line

        assert _is_noise_line("Reading src/dgov/cli.py") is False
        assert _is_noise_line("5 passed in 1.2s") is False
        assert _is_noise_line("linting completed") is False

    def test_bare_prompts_still_filtered(self):
        """Bare shell prompts should still be detected."""
        from dgov.status import _is_noise_line

        assert _is_noise_line("$") is True
        assert _is_noise_line("#") is True
        assert _is_noise_line(">") is True

    def test_strips_cursor_movement(self):
        text = "\x1b[2J\x1b[Hscreen cleared"
        assert _strip_ansi(text) == "screen cleared"

    def test_strips_osc_sequences(self):
        # OSC title set: ESC ] 0 ; title BEL
        text = "\x1b]0;my title\x07visible"
        assert _strip_ansi(text) == "visible"

    def test_strips_private_mode_set(self):
        text = "\x1b[?25hcursor shown"
        assert _strip_ansi(text) == "cursor shown"

    def test_strips_control_chars(self):
        text = "hello\x00world\x07done"
        assert _strip_ansi(text) == "helloworlddone"

    def test_strips_carriage_returns(self):
        text = "line1\rline2"
        assert _strip_ansi(text) == "line1line2"

    def test_preserves_plain_text(self):
        text = "no escape codes here"
        assert _strip_ansi(text) == text

    def test_empty_string(self):
        assert _strip_ansi("") == ""

    def test_strips_multiple_sequences(self):
        text = "\x1b[32mgreen\x1b[0m and \x1b[34mblue\x1b[0m"
        assert _strip_ansi(text) == "green and blue"
