"""Tests for `dgov compile` CLI command."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_json_env():
    os.environ.pop("DGOV_JSON", None)
    yield
    os.environ.pop("DGOV_JSON", None)


@pytest.fixture
def runner():
    return CliRunner()


def _make_plan_tree(root: Path, *, sections: list[str] | None = None) -> Path:
    """Create a minimal plan tree with two sections and three units."""
    plan_dir = root / "myplan"
    plan_dir.mkdir()

    if sections is None:
        sections = ["core", "cli"]

    # _root.toml
    (plan_dir / "_root.toml").write_text(
        f'[plan]\nname = "test-plan"\nsummary = "Test"\nsections = {json.dumps(sections)}\n'
    )

    # core section with two tasks, second depends on first
    core_dir = plan_dir / "core"
    core_dir.mkdir()
    (core_dir / "setup.toml").write_text(
        "[tasks.init]\n"
        'summary = "Initialize"\n'
        'prompt = "Set up the project"\n'
        'commit_message = "Init project"\n'
        'files.create = ["src/main.py"]\n'
        "\n"
        "[tasks.config]\n"
        'summary = "Add config"\n'
        'prompt = "Add config file"\n'
        'commit_message = "Add config"\n'
        'depends_on = ["init"]\n'
        'files.create = ["src/config.py"]\n'
    )

    # cli section with one task depending on core
    cli_dir = plan_dir / "cli"
    cli_dir.mkdir()
    (cli_dir / "commands.toml").write_text(
        "[tasks.entry]\n"
        'summary = "Add CLI"\n'
        'prompt = "Wire CLI entry point"\n'
        'commit_message = "Add CLI"\n'
        'depends_on = ["core/setup.config"]\n'
        'files.create = ["src/cli.py"]\n'
    )

    return plan_dir


def _provider_project_toml(project_settings: str = "") -> str:
    return f"""
[project]
provider = "test-provider"
{project_settings}

[providers.test-provider]
default_agent = "provider/model-name"
base_url = "https://provider.example.com/v1"
api_key_env = "TEST_PROVIDER_API_KEY"
"""


# -- Happy path --


def test_compile_happy_path(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    result = runner.invoke(cli, ["compile", str(plan_dir)])
    assert result.exit_code == 0, result.output
    assert "3 units" in result.output
    assert "_compiled.toml" in result.output

    compiled = plan_dir / "_compiled.toml"
    assert compiled.exists()

    # Verify it's valid TOML
    data = tomllib.loads(compiled.read_text())
    assert data["plan"]["name"] == "test-plan"
    assert "source_mtime_max" in data["plan"]
    assert "sop_set_hash" in data["plan"]
    assert len(data["tasks"]) == 3


def test_compile_reports_malformed_project_config(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        dgov_dir = root / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text("this is not valid toml {{{")
        plan_dir = _make_plan_tree(root)

        result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

    assert result.exit_code == 1
    assert "Invalid TOML" in result.output
    assert ".dgov/project.toml" in result.output


def test_compile_round_trips_through_parser(runner: CliRunner, tmp_path: Path) -> None:
    """_compiled.toml should parse via the existing parse_plan_file."""
    from dgov.plan import parse_plan_file

    plan_dir = _make_plan_tree(tmp_path)
    result = runner.invoke(cli, ["compile", str(plan_dir)])
    assert result.exit_code == 0, result.output

    compiled_path = plan_dir / "_compiled.toml"
    plan_spec = parse_plan_file(str(compiled_path))
    assert plan_spec.name == "test-plan"
    assert len(plan_spec.units) == 3

    # Check path-qualified IDs survived
    assert "core/setup.init" in plan_spec.units
    assert "core/setup.config" in plan_spec.units
    assert "cli/commands.entry" in plan_spec.units

    # Check depends_on resolved correctly
    entry = plan_spec.units["cli/commands.entry"]
    assert "core/setup.config" in entry.depends_on


def test_compile_preserves_root_default_agent(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    (plan_dir / "_root.toml").write_text(
        "[plan]\n"
        'name = "test-plan"\n'
        'summary = "Test"\n'
        'sections = ["core", "cli"]\n'
        'default_agent = "plan/default-agent"\n',
        encoding="utf-8",
    )

    result = runner.invoke(cli, ["compile", str(plan_dir)])

    assert result.exit_code == 0, result.output
    data = tomllib.loads((plan_dir / "_compiled.toml").read_text())
    assert data["plan"]["default_agent"] == "plan/default-agent"


def test_compile_reports_non_string_root_provider_field(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "test-plan"\nsummary = "Test"\nsections = ["core", "cli"]\n'
        "default_provider = 123\n",
        encoding="utf-8",
    )

    result = runner.invoke(cli, ["compile", str(plan_dir)])

    assert result.exit_code == 1
    assert "[plan].default_provider must be a string" in result.output


def test_compile_preserves_files(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

    data = tomllib.loads((plan_dir / "_compiled.toml").read_text())
    init_task = data["tasks"]["core/setup.init"]
    assert init_task["files"]["create"] == ["src/main.py"]


def test_compile_rejects_department_violation(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        dgov_dir = root / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[departments]\nCore = ["src/dgov/kernel.py"]\n',
            encoding="utf-8",
        )

        plan_dir = root / "constitution"
        section_dir = plan_dir / "core"
        section_dir.mkdir(parents=True)
        (plan_dir / "_root.toml").write_text(
            '[plan]\nname = "constitution"\nsummary = "Test"\nsections = ["core"]\n',
            encoding="utf-8",
        )
        (section_dir / "tasks.toml").write_text(
            "[tasks.kernel]\n"
            'summary = "Fix kernel"\n'
            'prompt = "Orient:\\nContext.\\n\\nEdit:\\n1. Change.\\n\\nVerify:\\n- Check."\n'
            'commit_message = "Fix kernel"\n'
            'files.edit = ["src/dgov/kernel.py"]\n',
            encoding="utf-8",
        )

        result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

        assert result.exit_code != 0
        assert "Constitutional violation" in result.output
        assert not (plan_dir / "_compiled.toml").exists()


# -- JSON output --


def test_compile_json_output(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    result = runner.invoke(cli, ["--json", "compile", str(plan_dir), "--dry-run"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["status"] == "compiled"
    assert data["units"] == 3
    assert data["edges"] == 2
    assert data["dry_run"] is True


# -- Structural errors --


def test_compile_cycle_fails(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = tmp_path / "cyclic"
    plan_dir.mkdir()
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "cycle"\nsummary = ""\nsections = ["s"]\n'
    )
    s_dir = plan_dir / "s"
    s_dir.mkdir()
    (s_dir / "tasks.toml").write_text(
        '[tasks.a]\nsummary = "a"\nprompt = "a"\ncommit_message = "a"\n'
        'depends_on = ["b"]\n\n'
        '[tasks.b]\nsummary = "b"\nprompt = "b"\ncommit_message = "b"\n'
        'depends_on = ["a"]\n'
    )
    result = runner.invoke(cli, ["compile", str(plan_dir)])
    assert result.exit_code != 0
    assert (
        "cycle" in result.output.lower()
        or "cycle" in (result.stderr_bytes or b"").decode().lower()
    )


def test_compile_unreachable_fails(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = tmp_path / "unreach"
    plan_dir.mkdir()
    (plan_dir / "_root.toml").write_text('[plan]\nname = "ur"\nsummary = ""\nsections = ["s"]\n')
    s_dir = plan_dir / "s"
    s_dir.mkdir()
    (s_dir / "tasks.toml").write_text(
        '[tasks.root]\nsummary = "r"\nprompt = "r"\ncommit_message = "r"\n\n'
        '[tasks.island]\nsummary = "i"\nprompt = "i"\ncommit_message = "i"\n'
        'depends_on = ["atoll"]\n\n'
        '[tasks.atoll]\nsummary = "a"\nprompt = "a"\ncommit_message = "a"\n'
        'depends_on = ["island"]\n'
    )
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
    assert result.exit_code != 0


def test_compile_unresolved_ref_fails(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = tmp_path / "badref"
    plan_dir.mkdir()
    (plan_dir / "_root.toml").write_text('[plan]\nname = "br"\nsummary = ""\nsections = ["s"]\n')
    s_dir = plan_dir / "s"
    s_dir.mkdir()
    (s_dir / "tasks.toml").write_text(
        '[tasks.a]\nsummary = "a"\nprompt = "a"\ncommit_message = "a"\n'
        'depends_on = ["nonexistent"]\n'
    )
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
    assert result.exit_code != 0
    assert "Unknown" in (result.output + (result.stderr_bytes or b"").decode())


def test_compile_missing_root_toml(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = tmp_path / "empty"
    plan_dir.mkdir()
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
    assert result.exit_code != 0


def test_compile_no_units(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = tmp_path / "nounits"
    plan_dir.mkdir()
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "empty"\nsummary = ""\nsections = ["s"]\n'
    )
    s_dir = plan_dir / "s"
    s_dir.mkdir()
    # TOML file with no [tasks] section
    (s_dir / "stuff.toml").write_text("[meta]\nversion = 1\n")
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
    assert result.exit_code != 0


# -- dry-run flag --


def test_compile_dry_run_label(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output


def test_compile_warns_when_setup_cmd_depends_on_plan_created_file(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(
        _provider_project_toml('setup_cmd = "xcodegen generate --spec project.yml"'),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    plan_dir = tmp_path / "swift-plan"
    section_dir = plan_dir / "tasks"
    section_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "swift-plan"\nsummary = ""\nsections = ["tasks"]\n'
    )
    (section_dir / "main.toml").write_text(
        "[tasks.project]\n"
        'summary = "Create XcodeGen project file"\n'
        'prompt = "Orient:\\nRead README.md.\\n\\nEdit:\\n'
        '1. Create project.yml.\\n\\nVerify:\\n- Check TOML."\n'
        'commit_message = "Create XcodeGen project file"\n'
        'files.create = ["project.yml"]\n'
    )

    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "setup_cmd references 'project.yml', which this plan creates" in result.output


def test_compile_warns_when_verify_tool_missing_from_worker_path(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(_provider_project_toml(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    plan_dir = tmp_path / "tool-plan"
    section_dir = plan_dir / "tasks"
    section_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "tool-plan"\nsummary = ""\nsections = ["tasks"]\n'
    )
    (section_dir / "main.toml").write_text(
        "[tasks.main]\n"
        'summary = "Update tool docs"\n'
        'prompt = "Orient:\\nRead README.md.\\n\\nEdit:\\n'
        "1. Update README.md.\\n\\nVerify:\\n"
        '- `definitely-missing-dgov-tool --check README.md`."\n'
        'commit_message = "Update tool docs"\n'
        'files.edit = ["README.md"]\n'
    )

    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Verify command references tool 'definitely-missing-dgov-tool'" in result.output


def test_compile_rejects_unknown_task_provider(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(_provider_project_toml(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    plan_dir = tmp_path / "provider-plan"
    section_dir = plan_dir / "tasks"
    section_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "provider-plan"\nsummary = ""\nsections = ["tasks"]\n'
    )
    (section_dir / "main.toml").write_text(
        "[tasks.main]\n"
        'summary = "Update docs"\n'
        'prompt = "Orient:\\nRead README.md.\\n\\nEdit:\\n'
        '1. Update README.md.\\n\\nVerify:\\n- Review diff."\n'
        'commit_message = "Update docs"\n'
        'provider = "missing"\n'
        'agent = "some/model"\n'
        'files.edit = ["README.md"]\n'
    )

    result = runner.invoke(cli, ["compile", str(plan_dir)])

    assert result.exit_code != 0
    assert "Unknown provider 'missing'" in result.output


def test_compile_rejects_missing_provider_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    plan_dir = tmp_path / "missing-provider-plan"
    section_dir = plan_dir / "tasks"
    section_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "missing-provider-plan"\nsummary = ""\nsections = ["tasks"]\n'
    )
    (section_dir / "main.toml").write_text(
        "[tasks.main]\n"
        'summary = "Update docs"\n'
        'prompt = "Orient:\\nRead README.md.\\n\\nEdit:\\n'
        '1. Update README.md.\\n\\nVerify:\\n- Review diff."\n'
        'commit_message = "Update docs"\n'
        'agent = "some/model"\n'
        'files.edit = ["README.md"]\n'
    )

    result = runner.invoke(cli, ["compile", str(plan_dir)])

    assert result.exit_code != 0
    assert "No provider for task" in result.output


def test_verify_prompt_command_scan_ignores_sop_prose_snippets() -> None:
    from dgov.cli.compile import _verify_prompt_commands

    prompt = """
[SOP: Project-Local Extensions]
## Verify
- Use `.get()`, `project.toml`, `description = "Run targeted tests"`, and `-m unit` in prose.
- Keep `plan tree has no units`, `missing sections`, and `Run targeted tests` as prose.
- Example Swift code: `let result = try await client.fetch()`.
- Compare file references like `src/dgov/cli/compile.py` and `README.md`.
## Escalate
- Mention `definitely-missing-dgov-tool --not-a-command` outside Verify.

Orient:
- Read README.md.
Edit:
- Update README.md.
Verify:
- `definitely-missing-dgov-tool --check README.md`
"""

    assert _verify_prompt_commands(prompt) == ["definitely-missing-dgov-tool --check README.md"]


def test_verify_prompt_command_scan_keeps_common_wrapped_commands() -> None:
    from dgov.cli.compile import _verify_prompt_commands

    prompt = """
Orient:
- Read README.md.
Edit:
- Update README.md.
Verify:
- `uv run pytest -q tests/test_cli_compile.py`
- `swift test --filter ParserTests`
- `./scripts/check-plan README.md`
"""

    assert _verify_prompt_commands(prompt) == [
        "uv run pytest -q tests/test_cli_compile.py",
        "swift test --filter ParserTests",
        "./scripts/check-plan README.md",
    ]


def test_compile_warns_when_plan_archive_is_ignored(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text("[project]\n", encoding="utf-8")
    (dgov_dir / ".gitignore").write_text("plans/archive/\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    plan_dir = tmp_path / "archive-plan"
    section_dir = plan_dir / "tasks"
    section_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "archive-plan"\nsummary = ""\nsections = ["tasks"]\n'
    )
    (section_dir / "main.toml").write_text(
        "[tasks.main]\n"
        'summary = "Update docs"\n'
        'prompt = "Orient:\\nRead README.md.\\n\\nEdit:\\n'
        '1. Update README.md.\\n\\nVerify:\\n- Review diff."\n'
        'commit_message = "Update docs"\n'
        'files.edit = ["README.md"]\n'
    )

    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "ignores .dgov/plans/archive" in result.output


def test_compile_does_not_warn_zero_sop_for_docs_only_task(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(_provider_project_toml(), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    plan_dir = tmp_path / "docs-plan"
    section_dir = plan_dir / "tasks"
    section_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "docs-plan"\nsummary = ""\nsections = ["tasks"]\n'
    )
    (section_dir / "main.toml").write_text(
        "[tasks.main]\n"
        'summary = "Update docs"\n'
        'prompt = "Orient:\\nRead README.md.\\n\\nEdit:\\n'
        '1. Update README.md.\\n\\nVerify:\\n- Review diff."\n'
        'commit_message = "Update docs"\n'
        'files.edit = ["README.md"]\n'
    )

    result = runner.invoke(cli, ["compile", str(plan_dir)])

    assert result.exit_code == 0, result.output
    assert "matched zero SOPs" not in result.output


def test_compile_empty_plan_reports_undeclared_section_files(
    runner: CliRunner, tmp_path: Path
) -> None:
    plan_dir = tmp_path / "missing-section-plan"
    section_dir = plan_dir / "tasks"
    section_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "missing-section-plan"\nsummary = ""\nsections = []\n'
    )
    (section_dir / "main.toml").write_text(
        "[tasks.main]\n"
        'summary = "Work"\n'
        'prompt = "Orient:\\nRead README.md.\\n\\nEdit:\\n1. Update README.md.\\n\\n'
        'Verify:\\n- Review diff."\n'
        'commit_message = "Update docs"\n'
        'files.edit = ["README.md"]\n'
    )

    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

    assert result.exit_code == 1
    assert "undeclared section directories: tasks" in result.output
    assert "[plan].sections" in result.output


def test_compile_empty_plan_reports_visible_files_without_tasks(
    runner: CliRunner, tmp_path: Path
) -> None:
    plan_dir = tmp_path / "empty-task-plan"
    section_dir = plan_dir / "tasks"
    section_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "empty-task-plan"\nsummary = ""\nsections = ["tasks"]\n'
    )
    (section_dir / "main.toml").write_text("# missing [tasks.<slug>] table\n")

    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

    assert result.exit_code == 1
    assert "no [tasks.<slug>] tables" in result.output
    assert "tasks/main.toml" in result.output


def test_compile_empty_plan_reports_ignored_task_files(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = tmp_path / "ignored-task-plan"
    section_dir = plan_dir / "tasks"
    section_dir.mkdir(parents=True)
    (plan_dir / "_root.toml").write_text(
        '[plan]\nname = "ignored-task-plan"\nsummary = ""\nsections = ["tasks"]\n'
    )
    (section_dir / "_example.toml").write_text('[tasks.main]\nsummary = "Example"\n')

    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

    assert result.exit_code == 1
    assert "ignored TOML files" in result.output
    assert "tasks/_example.toml" in result.output


# -- serializer edge cases --


def test_compile_multiline_prompt(runner: CliRunner, tmp_path: Path) -> None:
    """Prompts with newlines should serialize as multi-line TOML strings."""
    plan_dir = tmp_path / "ml"
    plan_dir.mkdir()
    (plan_dir / "_root.toml").write_text('[plan]\nname = "ml"\nsummary = ""\nsections = ["s"]\n')
    s_dir = plan_dir / "s"
    s_dir.mkdir()
    (s_dir / "work.toml").write_text(
        "[tasks.multi]\n"
        'summary = "multi"\n'
        'prompt = """\nLine 1\nLine 2\nLine 3"""\n'
        'commit_message = "multi"\n'
    )
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
    assert result.exit_code == 0, result.output

    # Round-trip: re-parse and check prompt survived
    from dgov.plan import parse_plan_file

    spec = parse_plan_file(str(plan_dir / "_compiled.toml"))
    prompt = spec.units["s/work.multi"].prompt
    assert "Line 1" in prompt
    assert "Line 2" in prompt
