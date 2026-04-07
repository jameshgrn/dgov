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


# -- Happy path --


def test_compile_happy_path(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
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


def test_compile_round_trips_through_parser(runner: CliRunner, tmp_path: Path) -> None:
    """_compiled.toml should parse via the existing parse_plan_file."""
    from dgov.plan import parse_plan_file

    plan_dir = _make_plan_tree(tmp_path)
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
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


def test_compile_preserves_files(runner: CliRunner, tmp_path: Path) -> None:
    plan_dir = _make_plan_tree(tmp_path)
    runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])

    data = tomllib.loads((plan_dir / "_compiled.toml").read_text())
    init_task = data["tasks"]["core/setup.init"]
    assert init_task["files"]["create"] == ["src/main.py"]


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
    result = runner.invoke(cli, ["compile", str(plan_dir), "--dry-run"])
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


def test_compile_fails_no_dry_run_no_api_key(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    with runner.isolated_filesystem() as root:
        plan_dir = _make_plan_tree(Path(root))
        # Create a SOP to trigger bundling
        sops_dir = Path(root) / ".dgov" / "sops"
        sops_dir.mkdir(parents=True)
        (sops_dir / "style.md").write_text("---\nname: style\ntitle: Style\n---\nUse ruff.\n")

        monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
        result = runner.invoke(cli, ["compile", "myplan"])
        assert result.exit_code != 0
        assert "FIREWORKS_API_KEY missing" in result.output


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
