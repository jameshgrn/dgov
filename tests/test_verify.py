"""Tests for dgov verification recipes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dgov.verify import (
    VerifyCommandResult,
    VerifyRecipe,
    VerifyRunResult,
    load_verify_recipes,
    run_verify_recipe,
    run_verify_recipes,
)

pytestmark = pytest.mark.unit


class TestVerifyRecipe:
    def test_defaults(self):
        recipe = VerifyRecipe(name="lint", command="ruff check src/")
        assert recipe.name == "lint"
        assert recipe.command == "ruff check src/"
        assert recipe.description is None
        assert recipe.log_name is None
        assert recipe.parser is None

    def test_frozen(self):
        recipe = VerifyRecipe(name="lint", command="ruff check src/")
        with pytest.raises(AttributeError):
            setattr(recipe, "name", "other")  # noqa: B010


class TestVerifyCommandResult:
    def test_frozen(self):
        result = VerifyCommandResult(
            recipe_name="lint",
            command="ruff check src/",
            exit_code=0,
            duration_s=0.5,
            log_path=None,
            warning_count=0,
            summary="ok",
        )
        with pytest.raises(AttributeError):
            setattr(result, "exit_code", 1)  # noqa: B010


class TestVerifyRunResult:
    def test_frozen(self):
        result = VerifyRunResult(status="pass", results=())
        with pytest.raises(AttributeError):
            setattr(result, "status", "fail")  # noqa: B010


class TestLoadVerifyRecipes:
    def test_loads_valid_recipes(self):
        raw = {
            "verify": {
                "lint": {"command": "ruff check src/", "description": "Run ruff"},
                "test": {"command": "pytest -q"},
            }
        }
        recipes = load_verify_recipes(raw)
        assert len(recipes) == 2
        assert recipes["lint"].command == "ruff check src/"
        assert recipes["lint"].description == "Run ruff"
        assert recipes["test"].command == "pytest -q"

    def test_empty_when_no_verify_section(self):
        assert load_verify_recipes({}) == {}
        assert load_verify_recipes({"project": {}}) == {}

    def test_rejects_missing_command(self):
        raw = {"verify": {"lint": {}}}
        with pytest.raises(ValueError, match=r"lint.*command"):
            load_verify_recipes(raw)

    def test_rejects_empty_command(self):
        raw = {"verify": {"lint": {"command": ""}}}
        with pytest.raises(ValueError, match=r"lint.*command"):
            load_verify_recipes(raw)

    def test_rejects_non_string_command(self):
        raw = {"verify": {"lint": {"command": 42}}}
        with pytest.raises(ValueError, match=r"lint.*command"):
            load_verify_recipes(raw)

    def test_rejects_unknown_field(self):
        raw = {"verify": {"lint": {"command": "ruff check", "unknown": "field"}}}
        with pytest.raises(ValueError, match=r"lint.*unknown"):
            load_verify_recipes(raw)

    def test_rejects_invalid_description_type(self):
        raw = {"verify": {"lint": {"command": "ruff check", "description": 123}}}
        with pytest.raises(ValueError, match=r"lint.*description"):
            load_verify_recipes(raw)

    def test_rejects_invalid_log_name_type(self):
        raw = {"verify": {"lint": {"command": "ruff check", "log_name": 123}}}
        with pytest.raises(ValueError, match=r"lint.*log_name"):
            load_verify_recipes(raw)

    def test_rejects_invalid_parser_type(self):
        raw = {"verify": {"lint": {"command": "ruff check", "parser": 123}}}
        with pytest.raises(ValueError, match=r"lint.*parser"):
            load_verify_recipes(raw)

    def test_preserves_all_optional_fields(self):
        raw = {
            "verify": {
                "lint": {
                    "command": "ruff check src/",
                    "description": "Run linter",
                    "log_name": "lint.log",
                    "parser": "ruff",
                }
            }
        }
        recipes = load_verify_recipes(raw)
        recipe = recipes["lint"]
        assert recipe.description == "Run linter"
        assert recipe.log_name == "lint.log"
        assert recipe.parser == "ruff"


class TestRunVerifyRecipe:
    def _write_script(self, tmp_path: Path, code: str) -> Path:
        script = tmp_path / "cmd.py"
        script.write_text(code)
        return script

    def test_captures_stdout_stderr(self, tmp_path):
        script = self._write_script(
            tmp_path, "import sys\nprint('hello')\nsys.stderr.write('world\\n')"
        )
        recipe = VerifyRecipe(name="hello", command=f"{sys.executable} {script}")
        result = run_verify_recipe(tmp_path, recipe)
        assert result.status == "pass"
        assert len(result.results) == 1
        r = result.results[0]
        assert r.exit_code == 0
        assert r.recipe_name == "hello"
        assert r.log_path is not None
        log = Path(r.log_path)
        assert log.exists()
        content = log.read_text()
        assert "hello" in content
        assert "world" in content

    def test_counts_warnings(self, tmp_path):
        script = self._write_script(
            tmp_path,
            "print('warning: something')\nprint('Warning: another')\nprint('normal line')",
        )
        recipe = VerifyRecipe(name="warn", command=f"{sys.executable} {script}")
        result = run_verify_recipe(tmp_path, recipe)
        r = result.results[0]
        assert r.warning_count == 2
        assert "2 warnings" in r.summary

    def test_non_zero_status(self, tmp_path):
        script = self._write_script(tmp_path, "import sys\nsys.exit(1)")
        recipe = VerifyRecipe(name="fail", command=f"{sys.executable} {script}")
        result = run_verify_recipe(tmp_path, recipe)
        assert result.status == "fail"
        r = result.results[0]
        assert r.exit_code == 1
        assert "exit=1" in r.summary

    def test_log_name_override(self, tmp_path):
        script = self._write_script(tmp_path, "print('hi')")
        recipe = VerifyRecipe(
            name="hello", command=f"{sys.executable} {script}", log_name="custom.log"
        )
        result = run_verify_recipe(tmp_path, recipe)
        r = result.results[0]
        assert r.log_path is not None
        assert r.log_path.endswith("custom.log")

    def test_timeout(self, tmp_path):
        script = self._write_script(tmp_path, "import time\ntime.sleep(10)")
        recipe = VerifyRecipe(name="slow", command=f"{sys.executable} {script}")
        result = run_verify_recipe(tmp_path, recipe, timeout=0.1)
        r = result.results[0]
        assert r.exit_code == -1
        assert r.log_path is not None
        assert "timed out" in r.summary or "timed out" in Path(r.log_path).read_text()

    def test_summary_singular_warning(self, tmp_path):
        script = self._write_script(tmp_path, "print('warning: only one')")
        recipe = VerifyRecipe(name="single", command=f"{sys.executable} {script}")
        result = run_verify_recipe(tmp_path, recipe)
        r = result.results[0]
        assert r.warning_count == 1
        assert "1 warning" in r.summary
        assert "1 warnings" not in r.summary


class TestRunVerifyRecipes:
    def _write_script(self, tmp_path: Path, code: str) -> Path:
        script = tmp_path / "cmd.py"
        script.write_text(code)
        return script

    def test_runs_multiple(self, tmp_path):
        ok = self._write_script(tmp_path, "print('a')")
        bad = self._write_script(tmp_path, "import sys; sys.exit(1)")

        recipes = {
            "ok": VerifyRecipe(name="ok", command=f"{sys.executable} {ok}"),
            "bad": VerifyRecipe(name="bad", command=f"{sys.executable} {bad}"),
        }
        result = run_verify_recipes(tmp_path, recipes)
        assert result.status == "fail"
        assert len(result.results) == 2

    def test_selects_subset(self, tmp_path):
        ok = self._write_script(tmp_path, "print('a')")
        recipes = {
            "ok": VerifyRecipe(name="ok", command=f"{sys.executable} {ok}"),
            "skip": VerifyRecipe(
                name="skip",
                command=f"{sys.executable} -c 'import sys; sys.exit(1)'",
            ),
        }
        result = run_verify_recipes(tmp_path, recipes, names=("ok",))
        assert result.status == "pass"
        assert len(result.results) == 1
        assert result.results[0].recipe_name == "ok"


class TestProjectConfigVerifyRecipes:
    def test_loads_from_toml(self, tmp_path):
        from dgov.config import load_project_config

        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            "[project]\n\n"
            "[verify.lint]\n"
            'command = "ruff check src/"\n'
            'description = "Run ruff"\n\n'
            "[verify.test]\n"
            'command = "pytest -q"\n'
            'log_name = "test.log"\n'
        )
        pc = load_project_config(tmp_path)
        assert "lint" in pc.verify_recipes
        assert pc.verify_recipes["lint"].command == "ruff check src/"
        assert pc.verify_recipes["test"].log_name == "test.log"

    def test_defaults_empty(self, tmp_path):
        from dgov.config import load_project_config

        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\nlanguage = "python"\n')
        pc = load_project_config(tmp_path)
        assert pc.verify_recipes == {}

    def test_invalid_recipe_in_toml_raises(self, tmp_path):
        from dgov.config import load_project_config

        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\n\n[verify.lint]\ncommand = ""\n')
        with pytest.raises(ValueError, match=r"lint.*command"):
            load_project_config(tmp_path)
