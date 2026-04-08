"""Tests for dgov config: project config loading and prompt rendering."""

from dgov.config import ProjectConfig, load_project_config


class TestProjectConfigDefaults:
    def test_defaults(self):
        pc = ProjectConfig()
        assert pc.language == "python"
        assert pc.src_dir == "src/"
        assert pc.test_dir == "tests/"
        assert "pytest" in pc.test_cmd
        assert "ruff check" in pc.lint_cmd

    def test_resolve_test_cmd(self):
        pc = ProjectConfig(test_cmd="pytest {test_dir} -q", test_dir="tests/")
        assert pc.resolve_test_cmd() == "pytest tests/ -q"

    def test_resolve_test_cmd_with_file(self):
        pc = ProjectConfig(test_cmd="pytest {test_dir} -q", test_dir="tests/")
        result = pc.resolve_test_cmd("tests/test_foo.py")
        assert "test_foo.py" in result

    def test_resolve_lint_cmd(self):
        pc = ProjectConfig(lint_cmd="ruff check {file}", src_dir="src/")
        assert pc.resolve_lint_cmd() == "ruff check src/"
        assert pc.resolve_lint_cmd("foo.py") == "ruff check foo.py"

    def test_resolve_format_cmd(self):
        pc = ProjectConfig(format_cmd="ruff format {file}")
        assert pc.resolve_format_cmd("foo.py") == "ruff format foo.py"


class TestPromptSection:
    def test_basic_rendering(self):
        pc = ProjectConfig()
        section = pc.to_prompt_section()
        assert "Language: python" in section
        assert "Source: src/" in section
        assert "Tests: tests/" in section

    def test_includes_markers(self):
        pc = ProjectConfig(test_markers=("unit", "integration"))
        section = pc.to_prompt_section()
        assert "unit" in section
        assert "integration" in section

    def test_includes_conventions(self):
        pc = ProjectConfig(conventions={"style": "google", "imports": "isort"})
        section = pc.to_prompt_section()
        assert "style: google" in section
        assert "imports: isort" in section


class TestLoadProjectConfig:
    def test_missing_file_returns_defaults(self, tmp_path):
        pc = load_project_config(tmp_path)
        assert pc.language == "python"

    def test_loads_toml(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[project]\nlanguage = "go"\nsrc_dir = "cmd/"\n'
            'test_dir = "cmd/"\ntest_cmd = "go test ./..."\n'
            'lint_cmd = "golangci-lint run {file}"\n'
            'format_cmd = "gofmt -w {file}"\n'
        )
        pc = load_project_config(tmp_path)
        assert pc.language == "go"
        assert pc.src_dir == "cmd/"
        assert pc.test_cmd == "go test ./..."

    def test_partial_toml_fills_defaults(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\nlanguage = "rust"\n')
        pc = load_project_config(tmp_path)
        assert pc.language == "rust"
        assert pc.test_dir == "tests/"  # default preserved

    def test_conventions_section(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[project]\n\n[conventions]\ntest_style = "pytest fixtures"\n'
            'imports = "absolute only"\n'
        )
        pc = load_project_config(tmp_path)
        assert pc.conventions == {"test_style": "pytest fixtures", "imports": "absolute only"}

    def test_markers_as_list(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\ntest_markers = ["unit", "slow"]\n')
        pc = load_project_config(tmp_path)
        assert pc.test_markers == ("unit", "slow")

    def test_type_check_cmd_loaded(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\ntype_check_cmd = "ty check"\n')
        pc = load_project_config(tmp_path)
        assert pc.type_check_cmd == "ty check"


class TestTypeCheckCommand:
    def test_type_check_cmd_default(self):
        assert ProjectConfig().type_check_cmd == ""

    def test_resolve_type_check_cmd(self):
        pc = ProjectConfig(type_check_cmd="ty check")
        assert pc.resolve_type_check_cmd() == "ty check"

    def test_prompt_section_includes_type_check(self):
        pc = ProjectConfig(type_check_cmd="ty check")
        section = pc.to_prompt_section()
        assert "Type check command: ty check" in section

    def test_prompt_section_omits_type_check_when_empty(self):
        pc = ProjectConfig()
        section = pc.to_prompt_section()
        assert "Type check" not in section
