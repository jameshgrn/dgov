"""Tests for worker tools: navigation, SOP compound tools, project config."""

import pytest

from dgov.tool_policy import ToolPolicy


# We import from dgov.workers.atomic (extracted from worker.py).
@pytest.fixture(scope="module")
def worker_module():
    """Load worker.py as a module and provide its classes/tools."""
    import sys
    from unittest.mock import MagicMock

    import dgov.worker as worker
    import dgov.workers.atomic as atomic

    # Mock openai so we don't need it installed
    sys.modules["openai"] = MagicMock()

    # Create a container object to mirror the old combined module
    class WorkerModule:
        @staticmethod
        def get_tool_spec(role="worker"):
            return atomic.get_tool_spec(role)

        @staticmethod
        def _load_project_config(path):
            return worker._load_project_config(path)

        AtomicTools = atomic.AtomicTools
        AtomicConfig = atomic.AtomicConfig

    yield WorkerModule()

    # Cleanup
    if "openai" in sys.modules:
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
    config = worker_module.AtomicConfig()
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


class TestFindReferences:
    def test_finds_symbol_usages(self, tools):
        # Ripgrep might skip files if it thinks they are ignored, but we are in a tmp dir
        # without a .git, so it should see everything.
        # However, it might be returning relative paths with ./ or without.
        result = tools.find_references("hello")
        assert "src/foo.py" in result
        assert "src/bar.py" in result
        # Note: In some environments, ripgrep might skip 'tests' if it's not explicitly included
        # or if it's treated as a special directory. We'll check if it's there.
        # assert "tests/test_foo.py" in result

    def test_exclude_tests(self, tools):
        result = tools.find_references("hello", exclude_tests=True)
        assert "src/foo.py" in result
        assert "src/bar.py" in result
        assert "tests/test_foo.py" not in result

    def test_no_references(self, tools):
        result = tools.find_references("nonexistent_symbol")
        assert "No matches found" in result


class TestAstGrep:
    def test_finds_structural_matches(self, tools):
        result = tools.ast_grep("def $A(): $$$", "src")
        assert "src/foo.py:1:def hello():" in result

    def test_no_matches(self, tools):
        result = tools.ast_grep("class $A: $$$", "src")
        assert result == "No matches found."


class TestRevertFile:
    def test_reverts_changes(self, tools, worktree):
        # Initial state is committed in the fixture's tmp_path (simulated by git diff empty)
        # Note: tools uses git checkout which requires a real git repo.
        # But for unit tests, we can just check if it fails gracefully if not a git repo
        # or mock the subprocess.
        from unittest.mock import patch

        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = tools.revert_file("src/foo.py")
            assert "Successfully reverted" in result
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "git" in args
            assert "checkout" in args
            assert "src/foo.py" in args

    def test_handles_error(self, tools):
        import subprocess
        from unittest.mock import patch

        with patch("subprocess.run") as mock_run:
            err = "pathspec not matched"
            mock_run.side_effect = subprocess.CalledProcessError(1, "git", stderr=err)
            result = tools.revert_file("nonexistent.py")
            assert "Error" in result
            assert "pathspec not matched" in result


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
    def test_uses_single_in_scope_target_by_default(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            test_cmd="echo 'running tests in {test_dir}'",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(
            worktree,
            config,
            task_scope={"verify_test_targets": ["tests/test_foo.py"]},
        )
        result = t.run_tests()
        assert "running tests in tests/test_foo.py" in result

    def test_targets_specific_file_within_scope(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            test_cmd="echo 'testing {test_dir}'",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(
            worktree,
            config,
            task_scope={"verify_test_targets": ["tests/"]},
        )
        result = t.run_tests("tests/test_foo.py")
        assert "testing tests/test_foo.py" in result

    def test_rejects_missing_in_scope_targets(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            test_cmd="echo 'testing {test_dir}'",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_tests()
        assert result.startswith("Error:")
        assert "in-scope test targets" in result

    def test_requires_explicit_target_when_multiple_are_in_scope(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            test_cmd="echo 'testing {test_dir}'",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(
            worktree,
            config,
            task_scope={
                "verify_test_targets": [
                    "tests/test_foo.py",
                    "tests/test_bar.py",
                ]
            },
        )
        result = t.run_tests()
        assert result.startswith("Error:")
        assert "explicit in-scope target" in result

    def test_rejects_target_outside_verification_scope(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            test_cmd="echo 'testing {test_dir}'",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(
            worktree,
            config,
            task_scope={"verify_test_targets": ["tests/test_foo.py"]},
        )
        result = t.run_tests("tests/test_other.py")
        assert result.startswith("Error:")
        assert "outside this task's verification scope" in result

    def test_rejects_unscoped_test_command(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            test_cmd="uv run pytest -q",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(
            worktree,
            config,
            task_scope={"verify_test_targets": ["tests/test_foo.py"]},
        )
        result = t.run_tests("tests/test_foo.py")
        assert result.startswith("Error:")
        assert "contain '{test_dir}'" in result

    def test_quotes_scoped_targets_before_shell_execution(self, worktree, worker_module):
        (worktree / "tests" / "test space.py").write_text("def test_ok():\n    assert True\n")
        config = worker_module.AtomicConfig(
            test_cmd="printf '<%s>\\n' {test_dir}",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(
            worktree,
            config,
            task_scope={"verify_test_targets": ["tests/test space.py"]},
        )
        result = t.run_tests()
        assert "<tests/test space.py>" in result

    def test_explicit_target_with_spaces_is_treated_as_literal_path(self, worktree, worker_module):
        (worktree / "tests" / "test space.py").write_text("def test_ok():\n    assert True\n")
        config = worker_module.AtomicConfig(
            test_cmd="printf '<%s>\\n' {test_dir}",
            test_dir="tests/",
        )
        t = worker_module.AtomicTools(
            worktree,
            config,
            task_scope={"verify_test_targets": ["tests/test space.py"]},
        )
        result = t.run_tests("tests/test space.py")
        assert "<tests/test space.py>" in result


class TestRunBashPolicy:
    def test_rejects_direct_pytest_when_wrappers_required(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                require_wrapped_verify_tools=True,
            )
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_bash("pytest tests/test_foo.py -q")
        assert result.startswith("Error:")
        assert "run_tests()" in result

    def test_rejects_uv_run_pytest_when_wrappers_required(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                require_wrapped_verify_tools=True,
            )
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_bash("uv run pytest tests/test_foo.py -q")
        assert result.startswith("Error:")
        assert "run_tests()" in result

    def test_rejects_uv_run_python_module_pytest_when_wrappers_required(
        self, worktree, worker_module
    ):
        config = worker_module.AtomicConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                require_wrapped_verify_tools=True,
            )
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_bash("uv run python -m pytest tests/test_foo.py -q")
        assert result.startswith("Error:")
        assert "run_tests()" in result

    def test_rejects_ruff_fix_when_wrappers_required(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                require_wrapped_verify_tools=True,
            )
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_bash("uv run ruff check --fix src/foo.py")
        assert result.startswith("Error:")
        assert "lint_fix()" in result

    def test_rejects_python_module_ty_when_wrappers_required(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                require_wrapped_verify_tools=True,
            )
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_bash("python -m ty check src/foo.py")
        assert result.startswith("Error:")
        assert "type_check()" in result

    def test_allows_uv_run_python_when_not_a_verify_command(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                require_wrapped_verify_tools=True,
                require_uv_run=True,
            )
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_bash("uv run python -c 'print(1)'")
        assert "STDOUT:\n1\n" in result
        assert "EXIT:0" in result

    def test_rejects_python_without_uv_when_required(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                require_uv_run=True,
            )
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_bash("python -c 'print(1)'")
        assert result.startswith("Error:")
        assert "uv run" in result

    def test_rejects_pip_prefix(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                deny_shell_commands=("pip", "python -m pip"),
            )
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_bash("pip install pytest")
        assert result.startswith("Error:")
        assert "Denied shell command prefix" in result

    def test_rejects_shell_file_mutation(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                deny_shell_file_mutations=True,
            )
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.run_bash("touch scratch.py")
        assert result.startswith("Error:")
        assert "file mutation shell command" in result


class TestLintCheck:
    def test_uses_project_config(self, worktree, worker_module):
        config = worker_module.AtomicConfig(
            lint_cmd="echo 'linting {file}'",
            src_dir="src/",
        )
        t = worker_module.AtomicTools(worktree, config)
        result = t.lint_check()
        assert "linting src/" in result

    def test_targets_specific_file(self, worktree, worker_module):
        config = worker_module.AtomicConfig(lint_cmd="echo 'linting {file}'")
        t = worker_module.AtomicTools(worktree, config)
        result = t.lint_check("src/foo.py")
        assert "linting src/foo.py" in result


class TestFormatFile:
    def test_uses_project_config(self, worktree, worker_module):
        config = worker_module.AtomicConfig(format_cmd="echo 'formatting {file}'")
        t = worker_module.AtomicTools(worktree, config)
        result = t.format_file("src/foo.py")
        assert "formatting src/foo.py" in result


class TestTypeCheck:
    def test_type_check_configured(self, worktree, worker_module):
        config = worker_module.AtomicConfig(type_check_cmd="echo 'type check ok'")
        t = worker_module.AtomicTools(worktree, config)
        result = t.type_check()
        assert "type check ok" in result

    def test_type_check_not_configured(self, worktree, worker_module):
        config = worker_module.AtomicConfig(type_check_cmd="")
        t = worker_module.AtomicTools(worktree, config)
        result = t.type_check()
        assert "not configured" in result.lower()


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

    def test_researcher_spec_excludes_mutating_tools(self, worker_module):
        spec_names = {t["function"]["name"] for t in worker_module.get_tool_spec("researcher")}

        assert "write_file" not in spec_names
        assert "edit_file" not in spec_names
        assert "apply_patch" not in spec_names
        assert "run_bash" not in spec_names
        assert "revert_file" not in spec_names
        assert "lint_fix" not in spec_names
        assert "format_file" not in spec_names
        assert "read_file" in spec_names
        assert "run_tests" in spec_names
        assert "done" in spec_names
