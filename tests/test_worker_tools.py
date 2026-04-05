"""Tests for worker tools: navigation, SOP compound tools, project config."""

from pathlib import Path

import pytest


# We can't import from dgov.worker (it's a standalone script with no dgov imports).
# Instead we exec the module to get its classes. This mirrors how the subprocess works.
@pytest.fixture(scope="module")
def worker_module():
    """Load worker.py as a module without triggering openai import."""
    import importlib.util
    import sys
    from unittest.mock import MagicMock

    # Mock openai so we don't need it installed
    sys.modules["openai"] = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "dgov_worker",
        Path(__file__).resolve().parent.parent / "src" / "dgov" / "worker.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod

    # Cleanup
    del sys.modules["openai"]


@pytest.fixture
def worktree(tmp_path):
    """Create a minimal worktree with some files."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("def hello():\n    return 'world'\n")
    (tmp_path / "src" / "bar.py").write_text("import foo\nresult = foo.hello()\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("def test_hello():\n    assert True\n")
    (tmp_path / "README.md").write_text("# Test Project\n")
    return tmp_path


@pytest.fixture
def tools(worktree, worker_module):
    """Create AtomicTools with default config."""
    config = worker_module._ProjectConfig()
    return worker_module.AtomicTools(worktree, config)


# -- Navigation tools --


class TestGrep:
    def test_finds_matches(self, tools):
        result = tools.grep("def hello", "src/foo.py")
        assert "src/foo.py:1:" in result
        assert "def hello" in result

    def test_searches_directory(self, tools):
        result = tools.grep("import foo")
        assert "src/bar.py:1:" in result

    def test_no_matches(self, tools):
        result = tools.grep("nonexistent_pattern_xyz")
        assert result == "No matches found."

    def test_invalid_regex(self, tools):
        result = tools.grep("[invalid")
        assert "Error" in result

    def test_path_traversal_blocked(self, tools):
        result = tools.grep("pattern", "../../etc/passwd")
        assert "Error" in result

    def test_truncates_at_limit(self, tools, worktree):
        # Write a file with many matching lines
        (worktree / "big.py").write_text("\n".join(f"line_{i} = True" for i in range(200)))
        result = tools.grep("line_", "big.py")
        assert "truncated" in result


class TestGlob:
    def test_finds_python_files(self, tools):
        result = tools.glob("*.py")
        assert "src/foo.py" in result
        assert "src/bar.py" in result
        assert "tests/test_foo.py" in result

    def test_finds_by_pattern(self, tools):
        result = tools.glob("test_*.py")
        assert "tests/test_foo.py" in result
        assert "src/foo.py" not in result

    def test_no_matches(self, tools):
        result = tools.glob("*.rs")
        assert result == "No files matched."

    def test_skips_dotfiles(self, tools, worktree):
        (worktree / ".hidden").mkdir()
        (worktree / ".hidden" / "secret.py").write_text("x = 1")
        result = tools.glob("*.py")
        assert ".hidden" not in result


class TestListDir:
    def test_lists_root(self, tools):
        result = tools.list_dir(".")
        assert "src/" in result
        assert "tests/" in result
        assert "README.md" in result

    def test_lists_subdirectory(self, tools):
        result = tools.list_dir("src")
        assert "foo.py" in result
        assert "bar.py" in result

    def test_nonexistent_dir(self, tools):
        result = tools.list_dir("nonexistent")
        assert "Error" in result

    def test_file_not_dir(self, tools):
        result = tools.list_dir("README.md")
        assert "Error" in result

    def test_path_traversal_blocked(self, tools):
        result = tools.list_dir("../../etc")
        assert "Error" in result


# -- SOP compound tools --


class TestRunTests:
    def test_uses_project_config(self, worktree, worker_module):
        config = worker_module._ProjectConfig(
            test_cmd="echo 'running tests in {test_dir}'",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_tests()
        assert "running tests in tests/" in result

    def test_targets_specific_file(self, worktree, worker_module):
        config = worker_module._ProjectConfig(
            test_cmd="echo 'testing {test_dir}'",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_tests("tests/test_foo.py")
        assert "testing tests/test_foo.py" in result


class TestLintCheck:
    def test_uses_project_config(self, worktree, worker_module):
        config = worker_module._ProjectConfig(
            lint_cmd="echo 'linting {file}'",
            src_dir="src/",
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.lint_check()
        assert "linting src/" in result

    def test_targets_specific_file(self, worktree, worker_module):
        config = worker_module._ProjectConfig(lint_cmd="echo 'linting {file}'")
        t = worker_module.AtomicTools(worktree, config)
        result = t.lint_check("src/foo.py")
        assert "linting src/foo.py" in result


class TestFormatFile:
    def test_uses_project_config(self, worktree, worker_module):
        config = worker_module._ProjectConfig(format_cmd="echo 'formatting {file}'")
        t = worker_module.AtomicTools(worktree, config)
        result = t.format_file("src/foo.py")
        assert "formatting src/foo.py" in result


# -- Project config loading --


class TestProjectConfig:
    def test_defaults_when_no_file(self, worker_module, tmp_path):
        config = worker_module._load_project_config(tmp_path)
        assert config.language == "python"
        assert config.test_dir == "tests/"
        assert config.src_dir == "src/"

    def test_loads_from_toml(self, worker_module, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[project]\nlanguage = "rust"\nsrc_dir = "crate/src/"\n'
            'test_dir = "crate/tests/"\ntest_cmd = "cargo test"\n'
            'lint_cmd = "cargo clippy"\nformat_cmd = "cargo fmt"\n'
            'test_markers = ["unit", "integration"]\n'
            '\n[conventions]\nstyle = "rustfmt"\n'
        )
        config = worker_module._load_project_config(tmp_path)
        assert config.language == "rust"
        assert config.src_dir == "crate/src/"
        assert config.test_cmd == "cargo test"
        assert config.test_markers == ("unit", "integration")
        assert config.conventions == {"style": "rustfmt"}

    def test_corrupt_toml_returns_defaults(self, worker_module, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text("this is not valid toml {{{")
        config = worker_module._load_project_config(tmp_path)
        assert config.language == "python"


# -- Tool spec completeness --


class TestToolSpec:
    def test_all_tools_in_spec(self, worker_module):
        """Every method on AtomicTools has a matching entry in get_tool_spec."""
        spec_names = {t["function"]["name"] for t in worker_module.get_tool_spec()}
        # done is not an AtomicTools method
        spec_names.discard("done")

        tool_methods = {
            name
            for name in dir(worker_module.AtomicTools)
            if not name.startswith("_") and callable(getattr(worker_module.AtomicTools, name))
        }

        missing = tool_methods - spec_names
        assert not missing, f"AtomicTools methods missing from tool spec: {missing}"

    def test_no_orphan_specs(self, worker_module):
        """Every entry in get_tool_spec (except 'done') maps to an AtomicTools method."""
        spec_names = {t["function"]["name"] for t in worker_module.get_tool_spec()}
        spec_names.discard("done")

        tool_methods = {
            name
            for name in dir(worker_module.AtomicTools)
            if not name.startswith("_") and callable(getattr(worker_module.AtomicTools, name))
        }

        orphans = spec_names - tool_methods
        assert not orphans, f"Tool specs without AtomicTools methods: {orphans}"
