"""Unit tests for dgov.templates and template CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.templates import (
    BUILT_IN_TEMPLATES,
    PromptTemplate,
    list_templates,
    load_templates,
    render_template,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def skip_governor_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DGOV_SKIP_GOVERNOR_CHECK", "1")


def _pane(slug: str = "task", agent: str = "qwen-35b") -> SimpleNamespace:
    return SimpleNamespace(
        slug=slug,
        pane_id="%7",
        agent=agent,
        worktree_path=f"/tmp/{slug}",
        branch_name=slug,
    )


class TestRenderTemplate:
    def test_all_vars_present(self) -> None:
        tpl = PromptTemplate(
            name="test",
            template="Fix {file}: {description}",
            required_vars=["file", "description"],
        )
        result = render_template(tpl, {"file": "app.py", "description": "null check"})
        assert result == "Fix app.py: null check"

    def test_missing_required_var_raises(self) -> None:
        tpl = PromptTemplate(
            name="test",
            template="Fix {file}: {description}",
            required_vars=["file", "description"],
        )
        with pytest.raises(ValueError, match="missing required variables.*description"):
            render_template(tpl, {"file": "app.py"})

    def test_extra_vars_ignored(self) -> None:
        tpl = PromptTemplate(
            name="test",
            template="Fix {file}",
            required_vars=["file"],
        )
        result = render_template(tpl, {"file": "app.py", "extra": "ignored"})
        assert result == "Fix app.py"


class TestLoadTemplates:
    def test_built_ins_loaded_by_default(self, tmp_path: Path) -> None:
        templates = load_templates(str(tmp_path))
        assert "bugfix" in templates
        assert "feature" in templates
        assert "refactor" in templates
        assert "test" in templates
        assert "review" in templates

    def test_user_template_merged(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / ".dgov" / "templates"
        templates_dir.mkdir(parents=True)
        (templates_dir / "deploy.toml").write_text(
            'name = "deploy"\n'
            'description = "Deploy to staging"\n'
            'template = "Deploy {service} to {env}"\n'
            'required_vars = ["service", "env"]\n'
            'default_agent = "pi"\n'
        )
        templates = load_templates(str(tmp_path))
        assert "deploy" in templates
        assert templates["deploy"].description == "Deploy to staging"
        assert templates["deploy"].default_agent == "pi"

    def test_user_template_overrides_builtin(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / ".dgov" / "templates"
        templates_dir.mkdir(parents=True)
        (templates_dir / "bugfix.toml").write_text(
            'name = "bugfix"\n'
            'description = "Custom bugfix"\n'
            'template = "Custom: fix {file} because {description}"\n'
            'required_vars = ["file", "description"]\n'
            'default_agent = "claude"\n'
        )
        templates = load_templates(str(tmp_path))
        assert templates["bugfix"].description == "Custom bugfix"
        assert templates["bugfix"].template.startswith("Custom:")
        assert templates["bugfix"].default_agent == "claude"


class TestBuiltInTemplatesRender:
    @pytest.mark.parametrize("name", list(BUILT_IN_TEMPLATES.keys()))
    def test_builtin_renders_with_dummy_vars(self, name: str) -> None:
        tpl = BUILT_IN_TEMPLATES[name]
        dummy_vars = {v: f"dummy_{v}" for v in tpl.required_vars}
        # Add optional vars that appear in templates but aren't required
        dummy_vars.setdefault("test_file", "tests/test_dummy.py")
        result = render_template(tpl, dummy_vars)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_lt_gov_template_uses_plan_run(self) -> None:
        tpl = BUILT_IN_TEMPLATES["lt-gov"]

        assert "plan run --wait" not in tpl.template
        assert "pane create --land" not in tpl.template
        assert "plan run" in tpl.template


class TestListTemplates:
    def test_returns_expected_format(self, tmp_path: Path) -> None:
        result = list_templates(str(tmp_path))
        assert isinstance(result, list)
        assert len(result) >= 5
        for entry in result:
            assert "name" in entry
            assert "description" in entry
            assert "required_vars" in entry
            assert "default_agent" in entry


class TestTemplateCliList:
    def test_template_list_output(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            cli, ["template", "list", "--project-root", str(tmp_path), "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        names = {t["name"] for t in data}
        assert "bugfix" in names
        assert "feature" in names

    def test_template_list_human_readable(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(cli, ["template", "list", "--project-root", str(tmp_path)])
        assert result.exit_code == 0
        assert "Name" in result.output
        assert "Description" in result.output
        assert "Agent" in result.output
        assert "bugfix" in result.output

    def test_template_show(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            cli, ["template", "show", "bugfix", "--project-root", str(tmp_path)]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "bugfix"
        assert "template" in data
        assert "required_vars" in data

    def test_template_show_unknown(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            cli, ["template", "show", "nonexistent", "--project-root", str(tmp_path)]
        )
        assert result.exit_code == 1
        assert "Unknown template" in result.output

    def test_template_create_prints_toml(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["template", "create", "my-tpl"])
        assert result.exit_code == 0
        assert "my-tpl" in result.output
        assert ".dgov/templates/" in result.output


class TestPaneCreateWithTemplate:
    def test_template_renders_and_creates_pane(self, runner: CliRunner) -> None:
        with patch(
            "dgov.lifecycle.create_worker_pane",
            return_value=_pane("fix-null", "qwen-35b"),
        ) as mock_create:
            result = runner.invoke(
                cli,
                [
                    "pane",
                    "create",
                    "-T",
                    "bugfix",
                    "--var",
                    "file=src/app.py",
                    "--var",
                    "description=null pointer",
                    "--var",
                    "test_file=tests/test_app.py",
                    "--no-preflight",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = mock_create.call_args.kwargs
        assert "null pointer" in call_kwargs["prompt"]
        assert "src/app.py" in call_kwargs["prompt"]
        assert call_kwargs["agent"] == "qwen-35b"
        assert call_kwargs["skip_auto_structure"] is True

    def test_template_uses_default_agent(self, runner: CliRunner) -> None:
        with patch(
            "dgov.lifecycle.create_worker_pane",
            return_value=_pane("add-feat", "qwen-35b"),
        ) as mock_create:
            result = runner.invoke(
                cli,
                [
                    "pane",
                    "create",
                    "-T",
                    "feature",
                    "--var",
                    "file=src/feat.py",
                    "--var",
                    "description=add caching",
                    "--var",
                    "test_file=tests/test_feat.py",
                    "--no-preflight",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock_create.call_args.kwargs["agent"] == "qwen-35b"

    def test_template_agent_override(self, runner: CliRunner) -> None:
        with patch(
            "dgov.lifecycle.create_worker_pane",
            return_value=_pane("fix-bug", "claude"),
        ) as mock_create:
            result = runner.invoke(
                cli,
                [
                    "pane",
                    "create",
                    "-T",
                    "bugfix",
                    "--agent",
                    "claude",
                    "--var",
                    "file=src/app.py",
                    "--var",
                    "description=fix it",
                    "--var",
                    "test_file=tests/test_app.py",
                    "--no-preflight",
                ],
            )

        assert result.exit_code == 0, result.output
        assert mock_create.call_args.kwargs["agent"] == "claude"

    def test_unknown_template_exits(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli,
            ["pane", "create", "-T", "nonexistent", "--no-preflight"],
        )
        assert result.exit_code == 1
        assert "Unknown template" in result.output

    def test_missing_template_var_exits(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli,
            ["pane", "create", "-T", "bugfix", "--var", "file=app.py", "--no-preflight"],
        )
        assert result.exit_code == 1
        assert "missing required variables" in result.output

    def test_neither_prompt_nor_template_exits(self, runner: CliRunner) -> None:
        result = runner.invoke(
            cli,
            ["pane", "create", "--no-preflight"],
        )
        assert result.exit_code == 1
        assert "Prompt required" in result.output


class TestTomlLoading:
    def test_toml_file_loaded(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / ".dgov" / "templates"
        templates_dir.mkdir(parents=True)
        (templates_dir / "lint-all.toml").write_text(
            'name = "lint-all"\n'
            'description = "Lint everything"\n'
            'template = "Run ruff on {directory}"\n'
            'required_vars = ["directory"]\n'
        )
        templates = load_templates(str(tmp_path))
        assert "lint-all" in templates
        tpl = templates["lint-all"]
        assert tpl.required_vars == ["directory"]
        assert tpl.default_agent is None
        result = render_template(tpl, {"directory": "src/"})
        assert result == "Run ruff on src/"

    def test_toml_name_defaults_to_stem(self, tmp_path: Path) -> None:
        templates_dir = tmp_path / ".dgov" / "templates"
        templates_dir.mkdir(parents=True)
        (templates_dir / "my-task.toml").write_text(
            'template = "Do {thing}"\nrequired_vars = ["thing"]\n'
        )
        templates = load_templates(str(tmp_path))
        assert "my-task" in templates
