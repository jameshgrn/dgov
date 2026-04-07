"""Init subcommand — project bootstrap and auto-detection."""

from __future__ import annotations

from pathlib import Path

import click

from dgov.cli import cli

_EXCLUDE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".tox",
        ".venv",
        "venv",
        ".eggs",
        "dist",
        "build",
        ".dgov-worktrees",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
    }
)


def _source_files(root: Path, ext: str) -> list[Path]:
    """Glob for files, skipping common non-source directories."""
    results: list[Path] = []
    for f in root.rglob(f"*{ext}"):
        if any(part in _EXCLUDE_DIRS for part in f.relative_to(root).parts):
            continue
        results.append(f)
    return results


def _detect_project(root: Path) -> tuple[str, str, str, list[str]]:
    """Auto-detect language, src dir, test dir, and extensions."""
    language = "python"
    src_dir = "src/"
    test_dir = "tests/"
    extensions = [".py"]

    # Language detection by file prevalence
    py_files = _source_files(root, ".py")
    js_files = _source_files(root, ".js") + _source_files(root, ".ts")
    rs_files = _source_files(root, ".rs")
    go_files = _source_files(root, ".go")

    counts = {
        "python": len(py_files),
        "javascript": len(js_files),
        "rust": len(rs_files),
        "go": len(go_files),
    }
    language = max(counts, key=counts.get)  # type: ignore[arg-type]
    if counts[language] == 0:
        language = "python"

    # Source dir detection
    if (root / "src").is_dir():
        src_dir = "src/"
    elif (root / "lib").is_dir():
        src_dir = "lib/"
    else:
        src_dir = "."

    # Test dir detection
    if (root / "tests").is_dir():
        test_dir = "tests/"
    elif (root / "test").is_dir():
        test_dir = "test/"
    else:
        test_dir = "tests/"

    # Extensions by language
    ext_map = {
        "python": [".py"],
        "javascript": [".js", ".ts", ".tsx"],
        "rust": [".rs"],
        "go": [".go"],
    }
    extensions = ext_map.get(language, [".py"])

    return language, src_dir, test_dir, extensions


_LANG_TEMPLATES: dict[str, dict[str, str]] = {
    "python": {
        "test_cmd": "python -m pytest {test_dir} -q --tb=short",
        "lint_cmd": "python -m ruff check {file}",
        "format_cmd": "python -m ruff format {file}",
        "lint_fix_cmd": "python -m ruff check --fix {file}",
        "format_check_cmd": "python -m ruff format --check {file}",
    },
    "javascript": {
        "test_cmd": "npx vitest run {test_dir}",
        "lint_cmd": "npx eslint {file}",
        "format_cmd": "npx prettier --write {file}",
        "lint_fix_cmd": "npx eslint --fix {file}",
        "format_check_cmd": "npx prettier --check {file}",
    },
    "rust": {
        "test_cmd": "cargo test",
        "lint_cmd": "cargo clippy -- -D warnings",
        "format_cmd": "cargo fmt",
        "lint_fix_cmd": "cargo clippy --fix --allow-dirty",
        "format_check_cmd": "cargo fmt --check",
    },
    "go": {
        "test_cmd": "go test ./...",
        "lint_cmd": "golangci-lint run {file}",
        "format_cmd": "gofmt -w {file}",
        "lint_fix_cmd": "golangci-lint run --fix {file}",
        "format_check_cmd": "gofmt -l {file}",
    },
}


def _render_project_toml(language: str, src_dir: str, test_dir: str, extensions: list[str]) -> str:
    """Render a project.toml string."""
    cmds = _LANG_TEMPLATES.get(language, _LANG_TEMPLATES["python"])
    ext_str = ", ".join(f'"{e}"' for e in extensions)

    lines = [
        "[project]",
        f'language = "{language}"',
        f'src_dir = "{src_dir}"',
        f'test_dir = "{test_dir}"',
        f"source_extensions = [{ext_str}]",
        f'test_cmd = "{cmds["test_cmd"]}"',
        f'lint_cmd = "{cmds["lint_cmd"]}"',
        f'format_cmd = "{cmds["format_cmd"]}"',
        f'lint_fix_cmd = "{cmds["lint_fix_cmd"]}"',
        f'format_check_cmd = "{cmds["format_check_cmd"]}"',
        "",
        "[conventions]",
    ]
    return "\n".join(lines) + "\n"


@cli.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite existing project.toml")
def init_cmd(force: bool) -> None:
    """Bootstrap .dgov/project.toml for the current repository.

    Auto-detects language, source directory, and test directory.
    """
    project_root = Path.cwd()
    dgov_dir = project_root / ".dgov"
    config_path = dgov_dir / "project.toml"

    if config_path.exists() and not force:
        click.echo(f"Already exists: {config_path}")
        click.echo("Use --force to overwrite.")
        raise click.exceptions.Exit(code=1)

    language, src_dir, test_dir, extensions = _detect_project(project_root)

    toml_content = _render_project_toml(language, src_dir, test_dir, extensions)

    dgov_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(toml_content)
    click.echo(f"Created {config_path}")
    click.echo(f"  language: {language}")
    click.echo(f"  src_dir:  {src_dir}")
    click.echo(f"  test_dir: {test_dir}")
