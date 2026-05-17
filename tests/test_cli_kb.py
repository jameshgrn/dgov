"""Tests for `dgov kb` CLI commands."""

from __future__ import annotations

import json
import os
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
def runner() -> CliRunner:
    return CliRunner()


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_valid_kb(root: Path) -> None:
    _write(root / ".dgov" / "governor.md", "governor\n")
    _write(
        root / "docs" / "knowledge" / "concepts" / "sentrux.md",
        """---
id: sentrux
title: Sentrux
kind: concept
status: living
sources:
  - .dgov/governor.md
related: []
---

# Sentrux

Source-backed article body.
""",
    )


def _write_kb_with_relations(root: Path) -> None:
    _write(root / ".dgov" / "governor.md", "governor\n")
    _write(
        root / "docs" / "knowledge" / "concepts" / "sentrux.md",
        """---
id: sentrux
title: Sentrux
kind: concept
status: living
sources:
  - .dgov/governor.md
related:
  - runner
---

# Sentrux

Body.
""",
    )
    _write(
        root / "docs" / "knowledge" / "concepts" / "runner.md",
        """---
id: runner
title: Runner
kind: concept
status: living
sources:
  - .dgov/governor.md
related:
  - sentrux
  - planner
---

# Runner

Body.
""",
    )
    _write(
        root / "docs" / "knowledge" / "concepts" / "planner.md",
        """---
id: planner
title: Planner
kind: concept
status: living
sources:
  - .dgov/governor.md
related: []
---

# Planner

Body.
""",
    )


def test_kb_validate_passes(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_valid_kb(root)
        result = runner.invoke(cli, ["kb", "validate"])

    assert result.exit_code == 0, result.output
    assert "Knowledge base valid: 1 article(s)." in result.output


def test_kb_validate_fails(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write(
            root / "docs" / "knowledge" / "concepts" / "bad.md",
            """---
id: bad
title: Bad
kind: concept
status: living
sources:
  - missing.md
related: []
---

# Bad

Bad article.
""",
        )
        result = runner.invoke(cli, ["kb", "validate"])

    assert result.exit_code == 1, result.output
    assert "source does not exist: missing.md" in result.output


def test_kb_list_json(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_valid_kb(root)
        result = runner.invoke(cli, ["--json", "kb", "list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["articles"][0]["id"] == "sentrux"
    assert payload["articles"][0]["absolute_path"].endswith("docs/knowledge/concepts/sentrux.md")


def test_kb_show_article(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_valid_kb(root)
        result = runner.invoke(cli, ["kb", "show", "sentrux"])

    assert result.exit_code == 0, result.output
    assert "path: docs/knowledge/concepts/sentrux.md" in result.output
    assert "# Sentrux" in result.output


def test_kb_show_unknown_article(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_valid_kb(root)
        result = runner.invoke(cli, ["kb", "show", "missing"])

    assert result.exit_code == 1, result.output
    assert "unknown article id: missing" in result.output


def test_kb_graph_json(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_kb_with_relations(root)
        result = runner.invoke(cli, ["--json", "kb", "graph"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["articles"] == ["planner", "runner", "sentrux"]
    related_edges = [e for e in payload["edges"] if e["relation"] == "related"]
    assert len(related_edges) == 3


def test_kb_graph_text(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_kb_with_relations(root)
        result = runner.invoke(cli, ["kb", "graph"])
    assert result.exit_code == 0, result.output
    assert "Articles (3):" in result.output
    assert "Edges:" in result.output


def test_kb_related_json(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_kb_with_relations(root)
        result = runner.invoke(cli, ["--json", "kb", "related", "runner", "--depth", "1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["start"] == "runner"
    assert payload["depth"] == 1
    assert sorted(payload["related"]) == ["planner", "sentrux"]


def test_kb_related_text(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_kb_with_relations(root)
        result = runner.invoke(cli, ["kb", "related", "runner", "--depth", "1"])
    assert result.exit_code == 0, result.output
    assert "planner" in result.output
    assert "sentrux" in result.output


def test_kb_related_unknown(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_kb_with_relations(root)
        result = runner.invoke(cli, ["kb", "related", "missing", "--depth", "1"])
    assert result.exit_code == 1, result.output
    assert "unknown article id: missing" in result.output


def test_kb_path_json(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_kb_with_relations(root)
        result = runner.invoke(cli, ["--json", "kb", "path", "sentrux", "planner"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["path"] == ["sentrux", "runner", "planner"]


def test_kb_path_text(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_kb_with_relations(root)
        result = runner.invoke(cli, ["kb", "path", "sentrux", "planner"])
    assert result.exit_code == 0, result.output
    assert "sentrux -> runner -> planner" in result.output


def test_kb_path_unknown(runner: CliRunner, tmp_path: Path) -> None:
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_kb_with_relations(root)
        result = runner.invoke(cli, ["kb", "path", "missing", "planner"])
    assert result.exit_code == 1, result.output
    assert "unknown article id: missing" in result.output


def test_kb_open_fallback(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("dgov.cli.kb.shutil.which", lambda _name: None)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_valid_kb(root)
        result = runner.invoke(cli, ["kb", "open", "sentrux"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("obsidian://open?path=")
    assert "sentrux.md" in result.output


def test_kb_open_with_obsidian(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)

    monkeypatch.setattr("dgov.cli.kb.shutil.which", lambda _name: "/usr/bin/obsidian")
    monkeypatch.setattr("dgov.cli.kb.subprocess.run", fake_run)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_valid_kb(root)
        result = runner.invoke(cli, ["kb", "open", "sentrux"])
    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0] == ["obsidian", "open", "docs/knowledge/concepts/sentrux.md"]


def test_kb_open_unknown(runner: CliRunner, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("dgov.cli.kb.shutil.which", lambda _name: None)
    with runner.isolated_filesystem(temp_dir=tmp_path) as td:
        root = Path(td)
        _write_valid_kb(root)
        result = runner.invoke(cli, ["kb", "open", "missing"])
    assert result.exit_code == 1, result.output
    assert "unknown article id: missing" in result.output
