"""Tests for worker.py — AtomicTools and helpers.

Worker is a standalone subprocess, so we import it directly and test
the AtomicTools class against real temp directories. No network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# worker.py is a script with `openai` dependency; patch it before import
sys.modules.setdefault("openai", type(sys)("openai"))
sys.modules["openai"].OpenAI = object  # type: ignore[attr-defined]

from dgov.worker import AtomicTools, _load_project_config, _ProjectConfig, get_tool_spec


@pytest.fixture()
def tools(tmp_path: Path) -> AtomicTools:
    (tmp_path / "hello.py").write_text("x = 1\n")
    return AtomicTools(tmp_path, _ProjectConfig())


# -- _check_path --


def test_check_path_valid(tools: AtomicTools) -> None:
    result = tools._check_path("hello.py")
    assert isinstance(result, Path)
    assert result.name == "hello.py"


def test_check_path_traversal_blocked(tools: AtomicTools) -> None:
    result = tools._check_path("../../etc/passwd")
    assert isinstance(result, str)
    assert "traversal" in result.lower()


# -- read_file --


def test_read_file_full(tools: AtomicTools) -> None:
    result = tools.read_file("hello.py")
    assert result == "x = 1\n"


def test_read_file_line_range(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "multi.py").write_text("a\nb\nc\nd\n")
    result = tools.read_file("multi.py", start_line=2, end_line=3)
    assert "2: b" in result
    assert "3: c" in result
    assert "1: a" not in result


def test_read_file_missing(tools: AtomicTools) -> None:
    result = tools.read_file("nope.py")
    assert result.startswith("Error:")


# -- write_file --


def test_write_file(tools: AtomicTools, tmp_path: Path) -> None:
    result = tools.write_file("new.txt", "hello world")
    assert "Successfully" in result
    assert (tmp_path / "new.txt").read_text() == "hello world"


def test_write_file_creates_dirs(tools: AtomicTools, tmp_path: Path) -> None:
    result = tools.write_file("sub/dir/file.txt", "nested")
    assert "Successfully" in result
    assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"


# -- edit_file --


def test_edit_file_happy(tools: AtomicTools, tmp_path: Path) -> None:
    result = tools.edit_file("hello.py", "x = 1", "x = 2")
    assert "Successfully" in result
    assert (tmp_path / "hello.py").read_text() == "x = 2\n"


def test_edit_file_not_found_text(tools: AtomicTools) -> None:
    result = tools.edit_file("hello.py", "zzz", "aaa")
    assert "not found" in result


def test_edit_file_ambiguous(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "dup.py").write_text("aa\naa\n")
    result = tools.edit_file("dup.py", "aa", "bb")
    assert "matches 2" in result


def test_edit_file_missing(tools: AtomicTools) -> None:
    result = tools.edit_file("nope.py", "a", "b")
    assert result.startswith("Error:")


# -- apply_patch --


def test_apply_patch_simple(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "patch_me.py").write_text("line1\nline2\nline3\n")
    patch = "--- a/patch_me.py\n+++ b/patch_me.py\n@@ -2,1 +2,1 @@\n-line2\n+replaced\n"
    result = tools.apply_patch("patch_me.py", patch)
    assert "Successfully" in result
    assert "replaced" in (tmp_path / "patch_me.py").read_text()


def test_apply_patch_missing_file(tools: AtomicTools) -> None:
    result = tools.apply_patch("nope.py", "@@ -1,1 +1,1 @@\n-a\n+b\n")
    assert result.startswith("Error:")


# -- file_symbols --


def test_file_symbols(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "X = 1\n\ndef foo():\n    pass\n\nclass Bar:\n    def baz(self):\n        pass\n"
    )
    result = tools.file_symbols("mod.py")
    assert "def foo" in result
    assert "class Bar" in result
    assert "def Bar.baz" in result
    assert "X = ..." in result


def test_file_symbols_not_python(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("# hi")
    result = tools.file_symbols("readme.md")
    assert "Error:" in result


def test_file_symbols_syntax_error(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def (broken:\n")
    result = tools.file_symbols("bad.py")
    assert "SyntaxError" in result


# -- check_syntax --


def test_check_syntax_valid(tools: AtomicTools) -> None:
    result = tools.check_syntax("hello.py")
    assert "OK" in result


def test_check_syntax_invalid(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def (:\n")
    result = tools.check_syntax("bad.py")
    assert "SyntaxError" in result


# -- head / tail --


def test_head(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_text("\n".join(f"line{i}" for i in range(50)))
    result = tools.head("lines.txt", n=5)
    assert "1: line0" in result
    assert "5: line4" in result
    assert "line5" not in result


def test_tail(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_text("\n".join(f"line{i}" for i in range(50)))
    result = tools.tail("lines.txt", n=3)
    assert "line49" in result
    assert "line47" in result
    assert "line46" not in result


def test_head_missing(tools: AtomicTools) -> None:
    result = tools.head("nope.txt")
    assert result.startswith("Error:")


# -- _load_project_config --


def test_load_project_config_defaults(tmp_path: Path) -> None:
    config = _load_project_config(tmp_path)
    assert config.language == "python"
    assert config.test_dir == "tests/"


def test_load_project_config_from_toml(tmp_path: Path) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(
        '[project]\nlanguage = "rust"\nsrc_dir = "src/"\n'
        'test_dir = "tests/"\ntest_markers = ["unit"]\n'
    )
    config = _load_project_config(tmp_path)
    assert config.language == "rust"
    assert config.test_markers == ("unit",)


# -- get_tool_spec --


def test_get_tool_spec_returns_list() -> None:
    specs = get_tool_spec()
    assert isinstance(specs, list)
    assert len(specs) > 20
    names = {s["function"]["name"] for s in specs}
    assert "read_file" in names
    assert "done" in names
    assert "edit_file" in names
