"""Tests for dgov.types module."""

import pytest

from dgov.types import (
    WorkerPhase,
    _strip_ansi,
    compute_phase,
    extract_summary_from_log,
    is_noise_line,
    match_signal,
)


class TestMatchSignal:
    """Tests for match_signal function."""

    def test_empty_string_returns_none(self):
        """Empty string should return None."""
        assert match_signal("") is None

    def test_whitespace_only_returns_none(self):
        """Whitespace-only string should return None."""
        assert match_signal("   \t\n  ") is None

    def test_no_match_returns_none(self):
        """String with no matching pattern should return None."""
        assert match_signal("Some random log line") is None

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("Reading file.txt", "Reading file.txt"),
            ("Read src/main.py", "Reading src/main.py"),
            ("Reading   multiple   spaces.txt", "Reading multiple   spaces.txt"),
        ],
    )
    def test_reading_patterns(self, line, expected):
        """Test reading patterns match correctly."""
        assert match_signal(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("Editing file.txt", "Editing file.txt"),
            ("Edit src/main.py", "Editing src/main.py"),
        ],
    )
    def test_editing_patterns(self, line, expected):
        """Test editing patterns match correctly."""
        assert match_signal(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("Writing file.txt", "Writing file.txt"),
            ("Write src/main.py", "Writing src/main.py"),
            ("Creating new_file.py", "Writing new_file.py"),
        ],
    )
    def test_writing_patterns(self, line, expected):
        """Test writing patterns match correctly."""
        assert match_signal(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("Running ruff check .", "Linting: ruff check ."),
            ("Ran ruff format", "Linting: ruff format"),
            ("Running pytest tests/", "Testing: pytest tests/"),
            ("Ran pytest -x", "Testing: pytest -x"),
            ("Running uv pip install", "Running: uv pip install"),
            ("Ran uv sync", "Running: uv sync"),
            ("Running git status", "Git: git status"),
            ("Ran git diff", "Git: git diff"),
        ],
    )
    def test_running_patterns(self, line, expected):
        """Test running/ran patterns match correctly."""
        assert match_signal(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("git add file.txt", "Staging: file.txt"),
            ("git add src/", "Staging: src/"),
        ],
    )
    def test_git_add_patterns(self, line, expected):
        """Test git add patterns match correctly."""
        assert match_signal(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("git commit -m 'message'", "Committing"),
            # bare "git commit" doesn't match — pattern requires whitespace after "commit"
            ("git commit", None),
        ],
    )
    def test_git_commit_patterns(self, line, expected):
        """Test git commit patterns match correctly."""
        assert match_signal(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("5 passed", "5 tests passed"),
            ("1 passed", "1 tests passed"),
            ("42 passed", "42 tests passed"),
        ],
    )
    def test_passed_patterns(self, line, expected):
        """Test passed test patterns match correctly."""
        assert match_signal(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("All checks passed", "Lint clean"),
            ("all checks passed", "Lint clean"),
            ("ALL CHECKS PASSED", "Lint clean"),
            ("no issues found", "Lint clean"),
            ("No Issues Found", "Lint clean"),
        ],
    )
    def test_lint_clean_patterns(self, line, expected):
        """Test lint clean patterns match correctly (case insensitive)."""
        assert match_signal(line) == expected

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("3 files changed", "3 files changed"),
            ("1 file changed", "1 files changed"),
        ],
    )
    def test_files_changed_patterns(self, line, expected):
        """Test files changed patterns match correctly."""
        assert match_signal(line) == expected

    def test_truncates_long_capture(self):
        """Long captures should be truncated to 60 chars for groups, 80 total."""
        long_path = "a" * 100
        result = match_signal(f"Reading {long_path}")
        assert result == f"Reading {'a' * 60}"
        assert len(result) <= 80

    def test_result_truncated_to_80_chars(self):
        """Result should be truncated to 80 chars total."""
        # Create a pattern that would exceed 80 chars when formatted
        long_path = "x" * 70
        result = match_signal(f"Running ruff check {long_path}")
        # "Linting: " is 9 chars + 60 chars max from group = 69, but let's verify
        assert len(result) <= 80

    def test_first_match_wins(self):
        """When line matches multiple patterns, first match should win."""
        # "Reading" pattern comes before "git add" pattern
        # Both could theoretically match but order matters
        line = "Reading file.txt"
        result = match_signal(line)
        assert result == "Reading file.txt"
        # Ensure it didn't match as something else
        assert "Staging" not in (result or "")

    def test_ansi_contaminated_input(self):
        """ANSI in input — regex search matches through ANSI, capture includes trailing."""
        # match_signal uses re.search, so ANSI prefix is skipped but trailing leaks into group
        result = match_signal("\x1b[32mReading file.txt\x1b[0m")
        assert result is not None
        assert "Reading" in result

    def test_partial_match_in_middle_of_line(self):
        """Pattern should match anywhere in the line."""
        assert match_signal("[INFO] Reading file.txt") == "Reading file.txt"
        # Capture group grabs everything after "Editing " to end of match
        result = match_signal("2024-01-01 Editing file.txt [done]")
        assert result is not None
        assert "Editing" in result


class TestComputePhase:
    """Tests for compute_phase function."""

    def test_not_alive_not_done_is_stuck(self):
        """When not alive and not done, should return STUCK."""
        assert (
            compute_phase("active", alive=False, done=False, duration_s=0, summary="")
            == WorkerPhase.STUCK
        )

    def test_done_is_done(self):
        """When done=True, should return DONE regardless of other params."""
        assert (
            compute_phase("active", alive=True, done=True, duration_s=0, summary="")
            == WorkerPhase.DONE
        )
        assert (
            compute_phase("failed", alive=False, done=True, duration_s=0, summary="")
            == WorkerPhase.DONE
        )

    def test_summary_contains_test_is_testing(self):
        """Summary containing 'test' should return TESTING."""
        assert (
            compute_phase("active", alive=True, done=False, duration_s=0, summary="Running tests")
            == WorkerPhase.TESTING
        )

    def test_summary_contains_pytest_is_testing(self):
        """Summary containing 'pytest' should return TESTING."""
        assert (
            compute_phase("active", alive=True, done=False, duration_s=0, summary="pytest -x")
            == WorkerPhase.TESTING
        )

    def test_summary_contains_commit_is_committing(self):
        """Summary containing 'commit' should return COMMITTING."""
        assert (
            compute_phase("active", alive=True, done=False, duration_s=0, summary="git commit")
            == WorkerPhase.COMMITTING
        )

    def test_summary_contains_git_commit_is_committing(self):
        """Summary containing 'git commit' should return COMMITTING."""
        assert (
            compute_phase(
                "active",
                alive=True,
                done=False,
                duration_s=0,
                summary="Running git commit -m 'msg'",
            )
            == WorkerPhase.COMMITTING
        )

    def test_summary_contains_read_is_working(self):
        """Summary containing 'read' should return WORKING."""
        assert (
            compute_phase("active", alive=True, done=False, duration_s=0, summary="Reading file")
            == WorkerPhase.WORKING
        )

    def test_summary_contains_edit_is_working(self):
        """Summary containing 'edit' should return WORKING."""
        assert (
            compute_phase("active", alive=True, done=False, duration_s=0, summary="Editing code")
            == WorkerPhase.WORKING
        )

    def test_summary_contains_write_is_working(self):
        """Summary containing 'write' should return WORKING."""
        assert (
            compute_phase("active", alive=True, done=False, duration_s=0, summary="Writing output")
            == WorkerPhase.WORKING
        )

    def test_state_active_defaults_to_working(self):
        """When state is 'active' and no summary keywords match, should return WORKING."""
        assert (
            compute_phase(
                "active", alive=True, done=False, duration_s=0, summary="Some other activity"
            )
            == WorkerPhase.WORKING
        )

    def test_unknown_state_unknown_phase(self):
        """Unknown state with no matching summary should return UNKNOWN."""
        assert (
            compute_phase(
                "unknown_state", alive=True, done=False, duration_s=0, summary="unknown work"
            )
            == WorkerPhase.UNKNOWN
        )

    def test_case_insensitive_summary_check(self):
        """Summary check should be case insensitive."""
        assert (
            compute_phase("active", alive=True, done=False, duration_s=0, summary="TESTING mode")
            == WorkerPhase.TESTING
        )
        assert (
            compute_phase(
                "active", alive=True, done=False, duration_s=0, summary="COMMITTING changes"
            )
            == WorkerPhase.COMMITTING
        )

    def test_empty_summary_active_is_working(self):
        """Empty summary with active state should return WORKING."""
        assert (
            compute_phase("active", alive=True, done=False, duration_s=0, summary="")
            == WorkerPhase.WORKING
        )

    def test_duration_ignored(self):
        """Duration parameter is currently ignored in phase computation."""
        # Duration doesn't affect phase logic currently
        assert (
            compute_phase("active", alive=True, done=False, duration_s=999999, summary="Working")
            == WorkerPhase.WORKING
        )


class TestExtractSummaryFromLog:
    """Tests for extract_summary_from_log function."""

    def test_empty_string_returns_starting(self):
        """Empty log text should return 'starting...'."""
        assert extract_summary_from_log("") == "starting..."

    def test_whitespace_only_returns_starting(self):
        """Whitespace-only log text should return 'starting...'."""
        assert extract_summary_from_log("   \n\t  \n") == "starting..."

    def test_extracts_last_matching_signal(self):
        """Should extract the last matching signal from the log."""
        log = """Reading file1.txt
Reading file2.txt
Editing main.py"""
        assert extract_summary_from_log(log) == "Editing main.py"

    def test_falls_back_to_last_line_no_match(self):
        """When no signals match, should return last line (stripped)."""
        log = """Some random line
Another random line
Final line"""
        assert extract_summary_from_log(log) == "Final line"

    def test_strips_ansi_from_output(self):
        """Should strip ANSI sequences from the output."""
        log = "\x1b[32mReading file.txt\x1b[0m"
        result = extract_summary_from_log(log)
        assert "\x1b" not in result
        assert result == "Reading file.txt"

    def test_strips_ansi_from_fallback_line(self):
        """Should strip ANSI from fallback line when no signal matches."""
        log = "\x1b[31m\x1b[1mError message\x1b[0m"
        result = extract_summary_from_log(log)
        assert "\x1b" not in result
        assert result == "Error message"

    def test_truncates_fallback_to_80_chars(self):
        """Fallback line should be truncated to 80 chars."""
        long_line = "x" * 100
        log = f"Line 1\n{long_line}"
        result = extract_summary_from_log(log)
        assert len(result) <= 80
        assert result == "x" * 80

    def test_handles_complex_ansi_sequences(self):
        """Should handle complex ANSI sequences including OSC."""
        log = "\x1b]0;title\x07\x1b[32mReading file.txt\x1b[0m"
        result = extract_summary_from_log(log)
        assert "\x1b" not in result
        assert result == "Reading file.txt"

    def test_empty_lines_filtered(self):
        """Empty lines should be filtered out."""
        log = """Line 1


Last line with signal"""
        assert extract_summary_from_log(log) == "Last line with signal"

    def test_prefers_signal_over_last_line(self):
        """Should prefer signal match over simple last line."""
        log = """Some activity
Reading file.txt
Just a random last line"""
        # Even though "Just a random last line" is last,
        # it should find "Reading file.txt" as signal
        result = extract_summary_from_log(log)
        # Actually it scans from end, so it checks last line first
        # If last line doesn't match, it continues backwards
        assert result == "Reading file.txt"

    def test_signal_in_last_line_wins(self):
        """Signal in last line should be extracted."""
        log = """Reading file1.txt
Reading file2.txt
Running pytest tests/"""
        assert extract_summary_from_log(log) == "Testing: pytest tests/"


class TestIsNoiseLine:
    """Tests for is_noise_line function."""

    def test_empty_line_is_noise(self):
        """Empty line should be considered noise."""
        assert is_noise_line("") is True

    def test_whitespace_only_is_noise(self):
        """Whitespace-only line should be considered noise."""
        assert is_noise_line("   ") is True
        assert is_noise_line("\t\t") is True
        assert is_noise_line("  \t  ") is True

    def test_box_drawing_chars_are_noise(self):
        """Lines with only box drawing chars should be noise."""
        # Box drawing range: U+2500 to U+257F
        assert is_noise_line("━" * 10) is True
        assert is_noise_line("─" * 10) is True
        assert is_noise_line("│" * 10) is True
        assert is_noise_line("┌─┐") is True

    def test_block_chars_are_noise(self):
        """Block characters should be considered noise."""
        # Block elements: U+2580 to U+259F
        assert is_noise_line("█" * 10) is True
        assert is_noise_line("▀▄") is True

    def test_braille_pattern_chars_are_noise(self):
        """Braille pattern chars should be noise."""
        # Braille: U+2800 to U+28FF
        assert is_noise_line("⠿" * 10) is True
        assert is_noise_line("⠁⠃⠉") is True

    def test_mixed_whitespace_and_box_drawing_is_noise(self):
        """Mix of whitespace and box drawing should be noise."""
        assert is_noise_line("  ──  ━━  ") is True

    def test_tui_chrome_keywords_are_noise(self):
        """Lines with TUI chrome keywords should be noise."""
        assert is_noise_line("type your message here") is True
        assert is_noise_line("Type Your Message") is True
        assert is_noise_line("bypass permissions") is True
        assert is_noise_line("MCP servers") is True

    def test_case_insensitive_tui_keywords(self):
        """TUI keywords check should be case insensitive."""
        assert is_noise_line("TYPE YOUR MESSAGE") is True
        assert is_noise_line("Bypass Permissions") is True
        assert is_noise_line("mcp servers") is True

    def test_content_line_is_not_noise(self):
        """Line with actual content should not be noise."""
        assert is_noise_line("Reading file.txt") is False
        assert is_noise_line("Error: something failed") is False
        assert is_noise_line("Hello world") is False

    def test_mixed_content_not_noise(self):
        """Line with content plus box drawing should not be noise."""
        # If there's actual text content, it's not just TUI chrome
        assert is_noise_line("─── Reading file.txt ───") is False

    def test_partial_match_in_content(self):
        """TUI keyword within other content is still noise if matched."""
        # The pattern matches anywhere in the line
        assert is_noise_line("Please type your message here") is True


class TestStripAnsi:
    """Tests for _strip_ansi helper function."""

    def test_empty_string(self):
        """Empty string should return empty."""
        assert _strip_ansi("") == ""

    def test_no_ansi_returns_unchanged(self):
        """String without ANSI should return unchanged."""
        text = "Hello world"
        assert _strip_ansi(text) == text

    def test_strips_csi_sequences(self):
        """Should strip CSI sequences (ESC[...)."""
        assert _strip_ansi("\x1b[32mGreen\x1b[0m") == "Green"
        assert _strip_ansi("\x1b[1;32mBold Green\x1b[0m") == "Bold Green"
        assert _strip_ansi("\x1b[?25l") == ""  # Hide cursor

    def test_strips_osc_sequences(self):
        """Should strip OSC sequences (ESC]...BEL or ESC]...ST)."""
        assert _strip_ansi("\x1b]0;title\x07") == ""  # BEL terminated
        assert _strip_ansi("\x1b]2;title\x1b\\") == ""  # ST terminated

    def test_strips_tmux_title_sequences(self):
        """Should strip tmux title-setting sequences."""
        assert _strip_ansi("\x1bktitle\x1b\\") == ""

    def test_preserves_non_ansi_content(self):
        """Should preserve content between ANSI sequences."""
        assert _strip_ansi("\x1b[32mHello\x1b[0m \x1b[31mWorld\x1b[0m") == "Hello World"

    def test_handles_multiple_sequences(self):
        """Should handle multiple ANSI sequences in one string."""
        text = "\x1b[1m\x1b[32m\x1b[40mText\x1b[0m"
        assert _strip_ansi(text) == "Text"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
