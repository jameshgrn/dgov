"""Verification recipe loader and runner for project-local checks."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Literal


@dataclass(frozen=True)
class VerifyRecipe:
    name: str
    command: str
    description: str | None = None
    log_name: str | None = None
    parser: str | None = None


@dataclass(frozen=True)
class VerifyCommandResult:
    recipe_name: str
    command: str
    exit_code: int
    duration_s: float
    log_path: str | None
    warning_count: int
    summary: str


@dataclass(frozen=True)
class VerifyRunResult:
    status: Literal["pass", "fail"]
    results: tuple[VerifyCommandResult, ...]


_KNOWN_FIELDS = {"command", "description", "log_name", "parser"}


def _require_string(section: Mapping[str, Any], name: str, field: str) -> str:
    value = section.get(field)
    if not value or not isinstance(value, str):
        raise ValueError(
            f"verify recipe {name!r}: '{field}' is required and must be a non-empty string"
        )
    return value


def _optional_string(section: Mapping[str, Any], name: str, field: str) -> str | None:
    value = section.get(field)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"verify recipe {name!r}: '{field}' must be a string")
    return value


def _reject_unknown_fields(section: Mapping[str, Any], name: str) -> None:
    for key in section:
        if key not in _KNOWN_FIELDS:
            raise ValueError(f"verify recipe {name!r}: unknown field {key!r}")


def _recipe_from_section(name: str, section: Mapping[str, Any]) -> VerifyRecipe:
    command = _require_string(section, name, "command")
    _reject_unknown_fields(section, name)
    description = _optional_string(section, name, "description")
    log_name = _optional_string(section, name, "log_name")
    _validate_log_file_name(
        name,
        log_name or f"{name}.log",
        "log_name" if log_name else "log file name",
    )
    parser = _optional_string(section, name, "parser")
    return VerifyRecipe(
        name=name,
        command=command,
        description=description,
        log_name=log_name,
        parser=parser,
    )


def load_verify_recipes(raw: Mapping[str, Any]) -> dict[str, VerifyRecipe]:
    """Load verification recipes from raw project.toml data.

    Expects sections shaped like [verify.<name>] which tomllib exposes as
    {"verify": {"<name>": {"command": "...", ...}}}.
    """
    verify_section = raw.get("verify", {})
    if not isinstance(verify_section, dict):
        raise ValueError(".dgov/project.toml [verify] must be a table")

    recipes: dict[str, VerifyRecipe] = {}
    for name, section in verify_section.items():
        if not isinstance(section, dict):
            raise ValueError(f"verify recipe {name!r}: section must be a table")
        recipes[name] = _recipe_from_section(name, section)

    return recipes


def _count_warnings(text: str) -> int:
    """Conservatively count warning lines in captured output."""
    return sum(1 for line in text.splitlines() if "warning" in line.lower())


def _validate_log_file_name(recipe_name: str, value: str, field: str) -> None:
    windows_path = PureWindowsPath(value)
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
    ):
        raise ValueError(f"verify recipe {recipe_name!r}: {field} must be a file name, not a path")


def _log_file_for_recipe(log_dir: Path, recipe: VerifyRecipe) -> Path:
    file_name = recipe.log_name or f"{recipe.name}.log"
    _validate_log_file_name(
        recipe.name,
        file_name,
        "log_name" if recipe.log_name else "log file name",
    )
    log_file = log_dir / file_name
    resolved_dir = log_dir.resolve(strict=False)
    resolved_file = log_file.resolve(strict=False)
    try:
        resolved_file.relative_to(resolved_dir)
    except ValueError as exc:
        raise ValueError(
            f"verify recipe {recipe.name!r}: log file must stay under {log_dir}"
        ) from exc
    return log_file


def _execute_verify_command(
    root: Path,
    recipe: VerifyRecipe,
    timeout: float,
) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            recipe.command,
            shell=True,
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return -1, stdout + stderr + f"\n[verify] timed out after {timeout}s\n"
    except OSError as exc:
        return -1, f"\n[verify] failed to execute: {exc}\n"


def _run_single(
    root: Path,
    recipe: VerifyRecipe,
    timeout: float,
) -> VerifyCommandResult:
    log_dir = root / ".dgov" / "runtime" / "verify"
    log_file = _log_file_for_recipe(log_dir, recipe)
    log_dir.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    exit_code, output = _execute_verify_command(root, recipe, timeout)
    duration = time.monotonic() - start
    log_file.write_text(output, encoding="utf-8")
    warning_count = _count_warnings(output)

    summary = (
        f"exit={exit_code} in {duration:.2f}s, "
        f"{warning_count} warning{'s' if warning_count != 1 else ''}"
    )

    return VerifyCommandResult(
        recipe_name=recipe.name,
        command=recipe.command,
        exit_code=exit_code,
        duration_s=duration,
        log_path=str(log_file),
        warning_count=warning_count,
        summary=summary,
    )


def run_verify_recipe(
    root: str | Path,
    recipe: VerifyRecipe,
    timeout: float = 300.0,
) -> VerifyRunResult:
    """Execute a single verification recipe and return the result."""
    root_path = Path(root)
    result = _run_single(root_path, recipe, timeout)
    status = "pass" if result.exit_code == 0 else "fail"
    return VerifyRunResult(status=status, results=(result,))


def run_verify_recipes(
    root: str | Path,
    recipes: Mapping[str, VerifyRecipe],
    names: tuple[str, ...] | None = None,
    timeout: float = 300.0,
) -> VerifyRunResult:
    """Execute multiple verification recipes and return the aggregated result."""
    root_path = Path(root)
    selected = recipes if names is None else _select_recipes(recipes, names)

    results: list[VerifyCommandResult] = []
    for recipe in selected.values():
        results.append(_run_single(root_path, recipe, timeout))

    status = "pass" if all(r.exit_code == 0 for r in results) else "fail"
    return VerifyRunResult(status=status, results=tuple(results))


def _select_recipes(
    recipes: Mapping[str, VerifyRecipe],
    names: tuple[str, ...],
) -> dict[str, VerifyRecipe]:
    missing = [name for name in names if name not in recipes]
    if missing:
        raise ValueError(f"unknown verify recipe {missing[0]!r}")
    return {name: recipes[name] for name in names}
