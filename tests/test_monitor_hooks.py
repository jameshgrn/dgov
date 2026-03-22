"""Unit tests for monitor_hooks module.

Tests loading and matching of configurable monitor hooks from TOML files.
"""

import pytest

from dgov.monitor_hooks import MonitorHook, load_monitor_hooks, match_monitor_hook

pytestmark = pytest.mark.unit


class TestMonitorHookDataclass:
    """Tests for MonitorHook dataclass."""

    def test_monitor_hook_basic(self):
        """Test basic MonitorHook creation."""
        hook = MonitorHook(pattern=".*", kind="working")
        assert hook.pattern == ".*"
        assert hook.kind == "working"
        assert hook.message is None
        assert hook.keystroke is None

    def test_monitor_hook_with_all_fields(self):
        """Test MonitorHook with all fields populated."""
        hook = MonitorHook(
            pattern="test.*",
            kind="done",
            message="Task completed",
            keystroke="\x03",
        )
        assert hook.pattern == "test.*"
        assert hook.kind == "done"
        assert hook.message == "Task completed"
        assert hook.keystroke == "\x03"

    def test_monitor_hook_is_frozen(self):
        """Test that MonitorHook is immutable (frozen)."""
        hook = MonitorHook(pattern=".*", kind="working")
        with pytest.raises(Exception):  # FrozenInstanceError for dataclasses
            hook.pattern = "new_pattern"


class TestLoadMonitorHooks:
    """Tests for load_monitor_hooks function."""

    def test_load_empty_session_root(self, tmp_path, monkeypatch):
        """Test loading from empty/non-existent config files."""
        # Mock home to return tmp_path so no real config files are loaded
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        hooks = load_monitor_hooks(str(tmp_path))
        assert hooks == []

    def test_load_valid_toml_single_hook(self, tmp_path, monkeypatch):
        """Test loading a single valid hook from TOML."""
        # Mock home to return tmp_path so no real config files are loaded
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        config_content = """
[[hooks.hook]]
pattern = ".*complete.*"
kind = "done"
message = "Done!"
"""
        config_file = tmp_path / ".dgov" / "monitor-hooks.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(config_content)

        hooks = load_monitor_hooks(str(tmp_path))
        assert len(hooks) == 1
        assert hooks[0].pattern == ".*complete.*"
        assert hooks[0].kind == "done"
        assert hooks[0].message == "Done!"

    def test_load_valid_toml_multiple_hooks(self, tmp_path, monkeypatch):
        """Test loading multiple hooks from TOML."""
        # Mock home to return tmp_path so no real config files are loaded
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        config_content = """
[[hooks.hook]]
pattern = ".*complete.*"
kind = "done"

[[hooks.hook]]
pattern = ".*error.*"
kind = "fail"
message = "Error detected"
"""
        config_file = tmp_path / ".dgov" / "monitor-hooks.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(config_content)

        hooks = load_monitor_hooks(str(tmp_path))
        assert len(hooks) == 2

    def test_load_valid_toml_nested_hooks(self, tmp_path, monkeypatch):
        """Test loading hooks from nested [hooks.my-rule] tables."""
        # Mock home to return tmp_path so no real config files are loaded
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        config_content = """
[hooks.my-done-hook]
pattern = "finished"
kind = "done"
message = "Finished!"
"""
        config_file = tmp_path / ".dgov" / "monitor-hooks.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(config_content)

        hooks = load_monitor_hooks(str(tmp_path))
        assert len(hooks) == 1
        assert hooks[0].pattern == "finished"
        assert hooks[0].kind == "done"
        assert hooks[0].message == "Finished!"

    def test_load_valid_toml_default_kind(self, tmp_path, monkeypatch):
        """Test that missing kind defaults to 'working'."""
        # Mock home to return tmp_path so no real config files are loaded
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        config_content = """
[[hooks.hook]]
pattern = ".*processing.*"
message = "Working..."
"""
        config_file = tmp_path / ".dgov" / "monitor-hooks.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(config_content)

        hooks = load_monitor_hooks(str(tmp_path))
        assert len(hooks) == 1
        assert hooks[0].kind == "working"

    def test_load_project_overrides_home(self, tmp_path, monkeypatch):
        """Test that project-level hooks override home-level hooks."""
        # Setup home directory mock
        home_dgov = tmp_path / "home_user" / ".dgov"
        home_dgov.mkdir(parents=True)
        home_config = home_dgov / "monitor-hooks.toml"
        home_config.write_text("""
[[hooks.hook]]
pattern = ".*default.*"
kind = "working"
""")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home_user")

        # Setup project directory
        project_dgov = tmp_path / "project" / ".dgov"
        project_dgov.mkdir(parents=True)
        project_config = project_dgov / "monitor-hooks.toml"
        project_config.write_text("""
[[hooks.hook]]
pattern = ".*default.*"
kind = "done"
""")

        hooks = load_monitor_hooks(str(tmp_path / "project"))
        assert len(hooks) == 1
        assert hooks[0].kind == "done"  # Project overrides home

    def test_load_malformed_toml_ignored(self, tmp_path, monkeypatch, caplog):
        """Test that malformed TOML files are skipped with warning."""
        # Mock home to return tmp_path so no real config files are loaded
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        config_content = """
[[hooks.hook]]
pattern = "invalid toml {{{
"""
        config_file = tmp_path / ".dgov" / "monitor-hooks.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(config_content)

        hooks = load_monitor_hooks(str(tmp_path))
        assert hooks == []

    def test_load_toml_missing_pattern_skipped(self, tmp_path, monkeypatch):
        """Test that entries without pattern are skipped."""
        # Mock home to return tmp_path so no real config files are loaded
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        config_content = """
[[hooks.hook]]
kind = "done"
message = "No pattern here"
"""
        config_file = tmp_path / ".dgov" / "monitor-hooks.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(config_content)

        hooks = load_monitor_hooks(str(tmp_path))
        assert hooks == []

    def test_load_keystroke_field(self, tmp_path, monkeypatch):
        """Test loading keystroke field."""
        # Mock home to return tmp_path so no real config files are loaded
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

        config_content = """
[[hooks.hook]]
pattern = ".*waiting.*"
kind = "nudge"
keystroke = "\\u0003"
"""
        config_file = tmp_path / ".dgov" / "monitor-hooks.toml"
        config_file.parent.mkdir(parents=True)
        config_file.write_text(config_content)

        hooks = load_monitor_hooks(str(tmp_path))
        assert len(hooks) == 1
        assert hooks[0].keystroke == "\u0003"


class TestMatchMonitorHook:
    """Tests for match_monitor_hook function."""

    def test_match_no_output(self):
        """Test matching against empty output returns None."""
        result = match_monitor_hook("", [])
        assert result is None

    def test_match_no_rules(self):
        """Test matching with no rules returns None."""
        result = match_monitor_hook("some output", [])
        assert result is None

    def test_match_exact_pattern(self):
        """Test exact pattern matching."""
        hook = MonitorHook(pattern=".*complete.*", kind="done")
        result = match_monitor_hook("Task complete", [hook])
        assert result == hook

    def test_match_partial_pattern(self):
        """Test partial pattern matching within last 10 lines."""
        hook = MonitorHook(pattern=".*error.*", kind="fail")
        output = "\n".join([f"line {i}" for i in range(15)] + ["error occurred"])
        result = match_monitor_hook(output, [hook])
        assert result == hook

    def test_match_case_insensitive(self):
        """Test case-insensitive matching."""
        hook = MonitorHook(pattern=".*COMPLETE.*", kind="done")
        result = match_monitor_hook("task Complete now", [hook])
        assert result == hook

    def test_match_regex_special_chars(self, tmp_path):
        """Test regex special characters in patterns."""
        hook = MonitorHook(pattern=r"\d+\. \d+%", kind="working")
        result = match_monitor_hook("Progress: 50. 75%", [hook])
        assert result == hook

    def test_match_last_10_lines_only(self):
        """Test that only last 10 lines are checked."""
        hook = MonitorHook(pattern=".*early.*", kind="working")
        # Pattern appears before line 10 cutoff
        output = "\n".join(["early line"] + [f"line {i}" for i in range(20)])
        result = match_monitor_hook(output, [hook])
        assert result is None

    def test_match_invalid_regex_skipped(self):
        """Test that invalid regex patterns are skipped gracefully."""
        hook = MonitorHook(pattern="[invalid(regex", kind="working")
        result = match_monitor_hook("test output", [hook])
        assert result is None

    def test_match_first_match_wins(self):
        """Test that first matching hook is returned."""
        hook1 = MonitorHook(pattern=".*match.*", kind="done")
        hook2 = MonitorHook(pattern=".*match.*", kind="fail")
        result = match_monitor_hook("this matches both", [hook1, hook2])
        assert result == hook1

    def test_match_multiline_tail(self):
        """Test matching across multiline tail."""
        # The function joins lines with \n, so pattern needs to account for that
        hook = MonitorHook(pattern=r".*start.*\n.*middle.*\n.*end.*", kind="working")
        output = "start\nmiddle\nend"
        result = match_monitor_hook(output, [hook])
        assert result == hook

    def test_match_empty_lines_in_output(self):
        """Test matching with empty lines in output."""
        hook = MonitorHook(pattern=".*found.*", kind="done")
        output = "\n\n\nfound it\n\n"
        result = match_monitor_hook(output, [hook])
        assert result == hook
