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
