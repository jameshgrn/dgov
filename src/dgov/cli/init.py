"""Init subcommand — project bootstrap and auto-detection."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import click

from dgov import __version__
from dgov.bootstrap_policy import GOVERNOR_CHARTER, SOP_FILES
from dgov.cli import cli, want_json
from dgov.project_root import resolve_project_root

_EXCLUDE_DIRS = frozenset({
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
})


_DGOV_GITIGNORE = """\
# Runtime state — never commit
state.db
state.db-wal
state.db-shm
runs.log
out/
runtime/
plans/deployed.jsonl
plans/*/_compiled.toml
"""

_SENTRUX_GITIGNORE = """\
# Runtime output — never commit
history.log
current.json
"""


def _ensure_gitignore(directory: Path, content: str) -> None:
    """Write a .gitignore if it doesn't already exist."""
    gitignore = directory / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(content)


def _sentrux_available() -> bool:
    """Return True when the sentrux binary is available."""
    return shutil.which("sentrux") is not None


def _sentrux_baseline_path(project_root: Path) -> Path:
    return project_root / ".sentrux" / "baseline.json"


def _save_sentrux_baseline(project_root: Path) -> tuple[bool, str]:
    """Save the sentrux baseline for the current repo."""
    try:
        result = subprocess.run(
            ["sentrux", "gate", "--save", str(project_root)],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        details = (getattr(exc, "stderr", "") or getattr(exc, "stdout", "") or str(exc)).strip()
        return False, details or "sentrux gate-save failed."
    return True, result.stdout.strip()


def _source_files(root: Path, ext: str) -> list[Path]:
    """Glob for files, skipping common non-source directories."""
    results: list[Path] = []
    for f in root.rglob(f"*{ext}"):
        if any(part in _EXCLUDE_DIRS for part in f.relative_to(root).parts):
            continue
        results.append(f)
    return results


# Known tooling-managed files that workers may incidentally touch via build /
# test / dep-install commands. Seeded into `[scope] ignore_files` by `dgov init`
# when detected, with Python's `uv.lock` included by default because `uv run`
# can create or refresh it mid-task even before it exists in git.
_SCOPE_IGNORE_CANDIDATES: tuple[str, ...] = (
    # Python
    ".venv",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    ".python-version",
    # JS/TS
    "node_modules",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    # Rust
    "Cargo.lock",
    # Swift / Xcode
    ".build",
    "DerivedData",
    "Package.resolved",
    # Go
    "go.sum",
    # Ruby
    "Gemfile.lock",
    # Elixir
    "mix.lock",
)
_PYTHON_REQUIREMENT_FILES: tuple[str, ...] = (
    "requirements.txt",
    "requirements-dev.txt",
    "dev-requirements.txt",
)
_JS_TOOLING_KEYS: tuple[str, ...] = (
    "test_cmd",
    "lint_cmd",
    "format_cmd",
    "lint_fix_cmd",
    "format_check_cmd",
    "type_check_cmd",
)
_ESLINT_CONFIG_FILES: tuple[str, ...] = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.yaml",
    ".eslintrc.yml",
    ".eslintrc.json",
    ".eslintrc",
)
_BIOME_CONFIG_FILES: tuple[str, ...] = ("biome.json", "biome.jsonc")
_PRETTIER_CONFIG_FILES: tuple[str, ...] = (
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.yaml",
    ".prettierrc.yml",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.mjs",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
)
_VITEST_CONFIG_FILES: tuple[str, ...] = (
    "vitest.config.js",
    "vitest.config.mjs",
    "vitest.config.ts",
    "vitest.config.mts",
)
_JEST_CONFIG_FILES: tuple[str, ...] = (
    "jest.config.js",
    "jest.config.ts",
    "jest.config.mjs",
    "jest.config.cjs",
)


def _detect_scope_ignore_files(root: Path, language: str) -> list[str]:
    """Return known managed files plus Python's uv.lock default."""
    detected = [name for name in _SCOPE_IGNORE_CANDIDATES if (root / name).exists()]
    if language == "python" and "uv.lock" not in detected:
        detected.append("uv.lock")
    return detected


def _dependency_name(dependency: str) -> str:
    """Return the normalized package name from a PEP 508-ish dependency string."""
    name = dependency.strip().split(";", 1)[0].strip()
    for token in ("[", "<", ">", "=", "!", "~", " "):
        name = name.split(token, 1)[0]
    return name.replace("_", "-").lower()


def _python_project_uses_pytest(root: Path) -> bool:
    """Detect pytest in common Python dependency manifests."""
    return _pyproject_uses_dependency(root, "pytest") or _requirements_use_dependency(
        root, "pytest"
    )


def _pyproject_uses_dependency(root: Path, dependency_name: str) -> bool:
    data = _read_pyproject(root)
    return _dependency_groups_include(
        _pyproject_dependency_candidates(data),
        dependency_name,
    )


def _read_pyproject(root: Path) -> dict[str, object]:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    try:
        return tomllib.loads(pyproject.read_text())
    except (tomllib.TOMLDecodeError, OSError):
        return {}


def _pyproject_dependency_candidates(data: dict[str, object]) -> list[object]:
    candidates: list[object] = []
    project = data.get("project")
    if isinstance(project, dict):
        project_data = cast("dict[str, object]", project)
        candidates.append(project_data.get("dependencies", ()))
        candidates.extend(_mapping_values(project_data.get("optional-dependencies")))
    candidates.extend(_mapping_values(data.get("dependency-groups")))
    return candidates


def _mapping_values(value: object) -> list[object]:
    return list(value.values()) if isinstance(value, dict) else []


def _dependency_groups_include(groups: list[object], dependency_name: str) -> bool:
    return any(_dependency_group_includes(group, dependency_name) for group in groups)


def _dependency_group_includes(group: object, dependency_name: str) -> bool:
    return isinstance(group, list) and any(
        isinstance(dependency, str) and _dependency_name(dependency) == dependency_name
        for dependency in group
    )


def _requirements_use_dependency(root: Path, dependency_name: str) -> bool:
    return any(
        _requirements_file_uses_dependency(root / filename, dependency_name)
        for filename in _PYTHON_REQUIREMENT_FILES
    )


def _requirements_file_uses_dependency(path: Path, dependency_name: str) -> bool:
    if not path.is_file():
        return False
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return False
    return any(_requirements_line_uses_dependency(line, dependency_name) for line in lines)


def _requirements_line_uses_dependency(line: str, dependency_name: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return False
    return _dependency_name(stripped) == dependency_name


def _empty_js_tooling() -> dict[str, str]:
    return dict.fromkeys(_JS_TOOLING_KEYS, "")


def _has_config_file(root: Path, filenames: tuple[str, ...]) -> bool:
    return any((root / filename).is_file() for filename in filenames)


def _read_package_json(root: Path) -> dict[str, object]:
    package_json = root / "package.json"
    if not package_json.is_file():
        return {}
    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return cast("dict[str, object]", payload) if isinstance(payload, dict) else {}


def _package_script_exists(root: Path, script_name: str) -> bool:
    scripts = _read_package_json(root).get("scripts", {})
    return isinstance(scripts, dict) and script_name in scripts


def _set_js_lint_commands(result: dict[str, str], *, has_eslint: bool, has_biome: bool) -> None:
    if has_eslint:
        result["lint_cmd"] = "npx eslint {file}"
        result["lint_fix_cmd"] = "npx eslint --fix {file}"
    elif has_biome:
        result["lint_cmd"] = "npx biome check {file}"
        result["lint_fix_cmd"] = "npx biome check --write {file}"


def _set_js_format_commands(
    result: dict[str, str],
    *,
    has_prettier: bool,
    has_biome: bool,
) -> None:
    if has_prettier:
        result["format_cmd"] = "npx prettier --write {file}"
        result["format_check_cmd"] = "npx prettier --check {file}"
    elif has_biome:
        result["format_cmd"] = "npx biome format --write {file}"
        result["format_check_cmd"] = "npx biome format {file}"


def _set_js_test_command(
    result: dict[str, str],
    *,
    has_vitest: bool,
    has_jest: bool,
    has_npm_test: bool,
) -> None:
    if has_vitest:
        result["test_cmd"] = "npx vitest run {test_dir}"
    elif has_jest:
        result["test_cmd"] = "npx jest"
    elif has_npm_test:
        result["test_cmd"] = "npm test"


def _set_js_type_check_command(result: dict[str, str], root: Path) -> None:
    if (root / "tsconfig.json").is_file():
        result["type_check_cmd"] = "npx tsc --noEmit"


def _detect_js_tooling(root: Path) -> dict[str, str]:
    """Detect JavaScript/TypeScript tooling from config files."""
    result = _empty_js_tooling()
    has_biome = _has_config_file(root, _BIOME_CONFIG_FILES)
    _set_js_lint_commands(
        result,
        has_eslint=_has_config_file(root, _ESLINT_CONFIG_FILES),
        has_biome=has_biome,
    )
    _set_js_format_commands(
        result,
        has_prettier=_has_config_file(root, _PRETTIER_CONFIG_FILES),
        has_biome=has_biome,
    )
    _set_js_test_command(
        result,
        has_vitest=_has_config_file(root, _VITEST_CONFIG_FILES),
        has_jest=_has_config_file(root, _JEST_CONFIG_FILES),
        has_npm_test=_package_script_exists(root, "test"),
    )
    _set_js_type_check_command(result, root)
    return result


@dataclass(frozen=True)
class ProjectDetection:
    language: str
    src_dir: str
    test_dir: str
    extensions: list[str]
    confidence: str
    reason: str


_PROJECT_TYPE_CHOICES = ("auto", "unknown", "python", "javascript", "rust", "go", "swift")


def _detect_language_counts(root: Path) -> dict[str, int]:
    """Count source files by language in the project root."""
    py_files = _source_files(root, ".py")
    js_files = _source_files(root, ".js") + _source_files(root, ".ts")
    rs_files = _source_files(root, ".rs")
    go_files = _source_files(root, ".go")
    swift_files = _source_files(root, ".swift")

    return {
        "python": len(py_files),
        "javascript": len(js_files),
        "rust": len(rs_files),
        "go": len(go_files),
        "swift": len(swift_files),
    }


def _detect_language(counts: dict[str, int]) -> str:
    """Determine primary language from file counts, defaulting to unknown."""
    language = max(counts, key=lambda k: counts.get(k, 0))
    if counts[language] == 0:
        language = "unknown"
    return language


def _detect_src_dir(root: Path, language: str = "") -> str:
    """Detect the source directory path."""
    if language == "swift" and (root / "Sources").is_dir():
        return "Sources/"
    if (root / "src").is_dir():
        return "src/"
    if (root / "lib").is_dir():
        return "lib/"
    if language == "swift":
        return "Sources/"
    return "."


def _detect_test_dir(root: Path, language: str = "") -> str:
    """Detect the test directory path."""
    if language == "swift" and (root / "Tests").is_dir():
        return "Tests/"
    if (root / "tests").is_dir():
        return "tests/"
    if (root / "test").is_dir():
        return "test/"
    if language == "swift":
        return "Tests/"
    return "tests/"


def _detect_extensions(language: str) -> list[str]:
    """Return file extensions for the detected language."""
    ext_map = {
        "python": [".py"],
        "javascript": [".js", ".ts", ".tsx"],
        "rust": [".rs"],
        "go": [".go"],
        "swift": [".swift"],
        "unknown": [],
    }
    return ext_map.get(language, [".py"])


def _detect_project(root: Path) -> tuple[str, str, str, list[str]]:
    """Auto-detect language, src dir, test dir, and extensions."""
    detection = _detect_project_details(root)
    return detection.language, detection.src_dir, detection.test_dir, detection.extensions


def _detect_project_details(root: Path, project_type: str = "auto") -> ProjectDetection:
    """Auto-detect project metadata, or apply an explicit project type."""
    if project_type not in _PROJECT_TYPE_CHOICES:
        raise ValueError(f"Unknown project type: {project_type}")

    counts = _detect_language_counts(root)
    language = project_type if project_type != "auto" else _detect_language(counts)
    src_dir = _detect_src_dir(root, language)
    test_dir = _detect_test_dir(root, language)
    extensions = _detect_extensions(language)
    confidence, reason = _detection_confidence(language, counts, project_type)
    return ProjectDetection(
        language=language,
        src_dir=src_dir,
        test_dir=test_dir,
        extensions=extensions,
        confidence=confidence,
        reason=reason,
    )


def _detection_confidence(
    language: str,
    counts: dict[str, int],
    project_type: str,
) -> tuple[str, str]:
    if project_type != "auto":
        return "explicit", f"selected by --project-type {project_type}"
    if language == "unknown":
        return "low", "no source files matched supported project types"
    return "high", f"detected {counts[language]} {language} source file(s)"


_LANG_TEMPLATES: dict[str, dict[str, str]] = {
    "python": {
        "test_cmd": "uv run pytest {test_dir} -q --tb=short",
        "lint_cmd": "uv run ruff check {file}",
        "format_cmd": "uv run ruff format {file}",
        "lint_fix_cmd": "uv run ruff check --fix --unsafe-fixes --show-fixes {file}",
        "format_check_cmd": "uv run ruff format --check {file}",
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
    "swift": {
        "test_cmd": "",
        "lint_cmd": "",
        "format_cmd": "",
        "lint_fix_cmd": "",
        "format_check_cmd": "",
    },
    "unknown": {
        "test_cmd": "",
        "lint_cmd": "",
        "format_cmd": "",
        "lint_fix_cmd": "",
        "format_check_cmd": "",
    },
}

_COMMAND_HINTS = {
    "test_cmd": "Configure a project-local test command or verify recipe",
    "lint_cmd": "Configure a project-local lint command or verify recipe",
    "format_cmd": "Configure a project-local format command or verify recipe",
    "lint_fix_cmd": "Configure a project-local lint-fix command or verify recipe",
    "format_check_cmd": "Configure a project-local format-check command or verify recipe",
    "type_check_cmd": "Configure a project-local type-check command or verify recipe",
}


def _render_project_toml(
    language: str,
    src_dir: str,
    test_dir: str,
    extensions: list[str],
    scope_ignore_files: list[str] | None = None,
    project_root: Path | None = None,
) -> str:
    """Render a project.toml string."""
    cmds = _project_commands(language, src_dir, project_root)
    ext_str = ", ".join(f'"{extension}"' for extension in extensions)
    ignore_files = scope_ignore_files or []
    lines = [
        *_project_header_lines(language, src_dir, test_dir, ext_str),
        *_project_command_lines(language, cmds),
        *_coverage_lines(cmds),
        *_setup_lines(language),
        *_runtime_config_lines(),
        *_provider_lines(),
        *_tool_policy_lines(language),
        *_scope_lines(ignore_files),
        *_convention_lines(),
    ]
    return "\n".join(lines) + "\n"


def _project_commands(
    language: str,
    src_dir: str,
    project_root: Path | None,
) -> dict[str, str]:
    cmds = _LANG_TEMPLATES.get(language, _LANG_TEMPLATES["python"])

    if language == "javascript" and project_root is not None:
        detected = _detect_js_tooling(project_root)
        return {**cmds, **detected}
    if (
        language == "python"
        and project_root is not None
        and _python_project_uses_pytest(project_root)
    ):
        coverage_source = src_dir.rstrip("/") or "."
        return {
            **cmds,
            "coverage_cmd": (
                f"uv run pytest --cov={coverage_source} --cov-report=json:{{output}} -q"
            ),
        }
    return dict(cmds)


def _project_header_lines(
    language: str,
    src_dir: str,
    test_dir: str,
    ext_str: str,
) -> list[str]:
    return [
        "[project]",
        f'language = "{language}"',
        f'src_dir = "{src_dir}"',
        f'test_dir = "{test_dir}"',
        f"source_extensions = [{ext_str}]",
        "",
        "# Sentrux baseline is explicit governor-owned state.",
        '# Run "dgov sentrux gate-save" after bootstrap and whenever you intentionally',
        "# refresh the architectural baseline for this repo.",
        "",
    ]


def _project_command_lines(language: str, cmds: dict[str, str]) -> list[str]:
    lines = [
        _render_cmd(cmds, "test_cmd"),
        _render_cmd(cmds, "lint_cmd"),
        _render_cmd(cmds, "format_cmd"),
        _render_cmd(cmds, "lint_fix_cmd"),
        _render_cmd(cmds, "format_check_cmd"),
    ]
    if language == "javascript":
        lines.append(_render_cmd(cmds, "type_check_cmd"))
    else:
        lines.append("# type_check_cmd = \"\"  # Optional: e.g. 'uv run ty check' for Python")
    return lines


def _render_cmd(cmds: dict[str, str], key: str) -> str:
    value = cmds.get(key, "")
    if value:
        return f'{key} = "{value}"'
    return f'{key} = ""  # {_COMMAND_HINTS[key]}'


def _coverage_lines(cmds: dict[str, str]) -> list[str]:
    coverage_cmd = cmds.get("coverage_cmd")
    if coverage_cmd:
        return [f'coverage_cmd = "{coverage_cmd}"', "coverage_threshold = 2.0"]
    return [
        (
            '# coverage_cmd = ""  # Optional: e.g. '
            "'uv run pytest --cov=src --cov-report=json:{output} -q'"
        ),
        "coverage_threshold = 2.0",
    ]


def _setup_lines(language: str) -> list[str]:
    if language == "javascript":
        return [
            'setup_cmd = "npm ci --ignore-scripts 2>/dev/null'
            ' || npm install --ignore-scripts 2>/dev/null"'
        ]
    return ['# setup_cmd = ""  # Runs in worktree before gates']


def _runtime_config_lines() -> list[str]:
    return [
        "",
        "# Worktree bootstrap timeout in seconds",
        "bootstrap_timeout = 300",
        "",
        "# Settlement timeout in seconds",
        "settlement_timeout = 120",
        "",
        "# Target line length for formatting and wrapping",
        "line_length = 99",
        "",
        "# Fast review hooks (git sanity checks). {file} is replaced with changed files.",
        "review_hooks = [",
        "  # \"grep -q 'TODO' {file} && exit 1 || exit 0\",  # Example: reject TODOs",
        '  # "detect-secrets-hook --baseline .secrets.baseline {file}",  # Example: secrets',
        "]",
        "",
    ]


def _provider_lines() -> list[str]:
    return [
        "# OpenAI-compatible worker provider.",
        "# Configure this before running executable worker plans.",
        '# provider = "your-provider"',
        "",
        "# [providers.your-provider]",
        '# default_agent = "provider/model-name"',
        '# base_url = "https://provider.example.com/v1"',
        '# api_key_env = "YOUR_PROVIDER_API_KEY"',
        "",
    ]


def _tool_policy_lines(language: str) -> list[str]:
    return [
        "[tool_policy]",
        "restrict_run_bash = true",
        'deny_shell_commands = ["pip", "python -m pip", "pip3", "python -m venv", "uv venv"]',
        "deny_shell_file_mutations = true",
        f"require_wrapped_verify_tools = {'true' if language == 'python' else 'false'}",
        f"require_uv_run = {'true' if language == 'python' else 'false'}",
        "",
    ]


def _scope_lines(ignore_files: list[str]) -> list[str]:
    return [
        "[scope]",
        "# Optional project-level merge surface. deny_files wins over allow_files and",
        "# cannot be bypassed by plan file claims. Leave allow_files empty for claim-only scope.",
        "allow_files = []",
        "deny_files = []",
        "",
        "# Files exempted from scope-violation checks in addition to dgov's built-in",
        "# Python defaults: .venv, uv.lock, __pycache__, and *.pyc.",
        "# Add repo-specific managed files here when workers may touch them incidentally.",
        "# Exact paths, directory names, and constrained globs like *.pyc are supported.",
        f"ignore_files = [{_quoted_string_list(ignore_files)}]",
        "",
    ]


def _quoted_string_list(values: list[str]) -> str:
    return ", ".join(f'"{value}"' for value in values)


def _convention_lines() -> list[str]:
    return [
        "[conventions]",
        "# Add project-specific rules here for the agent to follow",
        '# style = "Prefer functional over OOP"',
    ]


def _render_governor_md() -> str:
    """Render the repo-local governor charter."""
    return GOVERNOR_CHARTER


def _bootstrap_policy_targets(dgov_dir: Path) -> dict[Path, str]:
    """Return bootstrap-owned policy files under .dgov/."""
    sops_dir = dgov_dir / "sops"
    return {
        dgov_dir / "governor.md": _render_governor_md(),
        **{sops_dir / name: content for name, content in SOP_FILES.items()},
    }


def _write_bootstrap_files(
    bootstrap_files: dict[Path, str],
    *,
    force: bool,
) -> list[Path]:
    """Create missing bootstrap files, or refresh them when force=True."""
    created: list[Path] = []
    for path, content in bootstrap_files.items():
        if not force and path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        created.append(path)
    return created


def _prepare_runtime_dirs(project_root: Path, dgov_dir: Path) -> None:
    dgov_dir.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(dgov_dir, _DGOV_GITIGNORE)

    sentrux_dir = project_root / ".sentrux"
    sentrux_dir.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(sentrux_dir, _SENTRUX_GITIGNORE)


def _write_init_files(
    config_path: Path,
    toml_content: str,
    dgov_dir: Path,
    *,
    force: bool,
) -> list[Path]:
    created: list[Path] = []
    if force or not config_path.exists():
        config_path.write_text(toml_content)
        created.append(config_path)
    created.extend(_write_bootstrap_files(_bootstrap_policy_targets(dgov_dir), force=force))
    return created


def _exit_already_initialized(dgov_dir: Path) -> None:
    click.echo(f"Already initialized: {dgov_dir}")
    click.echo("Use --force to overwrite bootstrap files.")
    raise click.exceptions.Exit(code=1)


def _print_created_paths(created: list[Path]) -> None:
    for path in created:
        click.echo(f"Created {path}")


def _print_init_config_summary(
    detection: ProjectDetection,
    scope_ignore_files: list[str],
    project_root: Path,
) -> None:
    cmds = _project_commands(detection.language, detection.src_dir, project_root)
    click.echo(f"  language: {detection.language}")
    click.echo(f"  confidence: {detection.confidence} ({detection.reason})")
    click.echo(f"  src_dir:  {detection.src_dir}")
    click.echo(f"  test_dir: {detection.test_dir}")
    click.echo(f"  test_cmd: {cmds.get('test_cmd', '') or '(not configured)'}")
    click.echo(f"  lint_cmd: {cmds.get('lint_cmd', '') or '(not configured)'}")
    setup_lines = _setup_lines(detection.language)
    setup_cmd = next((line for line in setup_lines if line.startswith("setup_cmd = ")), "")
    if setup_cmd:
        click.echo(f"  {setup_cmd}")
    if scope_ignore_files:
        click.echo(f"  scope.ignore_files: {', '.join(scope_ignore_files)}")


def _is_git_repo(project_root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _probe_status(result: str) -> tuple[str, str]:
    lines = result.splitlines()
    exit_line = lines[-1] if lines else ""
    stdout = ""
    if "STDOUT:" in lines:
        start = lines.index("STDOUT:") + 1
        end = lines.index("STDERR:") if "STDERR:" in lines else len(lines)
        stdout = "\n".join(lines[start:end]).strip()
    if exit_line == "EXIT:0":
        return "ok", stdout.splitlines()[0] if stdout else ""
    return "missing", ""


def _worker_env_probe(
    project_root: Path, detection: ProjectDetection
) -> list[tuple[str, str, str]]:
    from dgov.workers.atomic import AtomicTools
    from dgov.workers.config import AtomicConfig

    cmds = _project_commands(detection.language, detection.src_dir, project_root)
    config = AtomicConfig(
        language=detection.language,
        src_dir=detection.src_dir,
        test_dir=detection.test_dir,
        test_cmd=cmds.get("test_cmd", ""),
        lint_cmd=cmds.get("lint_cmd", ""),
        format_cmd=cmds.get("format_cmd", ""),
        lint_fix_cmd=cmds.get("lint_fix_cmd", ""),
        type_check_cmd=cmds.get("type_check_cmd") or None,
    )
    tools = AtomicTools(project_root, config)
    probes = [
        ("whoami", "whoami"),
        ("USER", "printenv USER"),
    ]
    try:
        return [(name, *_probe_status(tools.run_bash(command))) for name, command in probes]
    finally:
        shutil.rmtree(tools._sandbox_home, ignore_errors=True)


def _print_init_preflight(project_root: Path, detection: ProjectDetection) -> None:
    click.echo("Preflight:")
    commands = ", ".join(sorted(cli.commands))
    click.echo(f"  dgov: {__version__}")
    click.echo(f"  commands: {commands}")
    if "pane" not in cli.commands:
        click.echo("  pane: unavailable")
    click.echo(f"  git repo: {'yes' if _is_git_repo(project_root) else 'no'}")
    click.echo(f"  ledger root: {project_root}")
    click.echo(f"  project: {detection.language} ({detection.confidence})")
    click.echo("  worker env:")
    for name, status, detail in _worker_env_probe(project_root, detection):
        suffix = f" ({detail})" if detail else ""
        click.echo(f"    {name}: {status}{suffix}")
    if detection.confidence == "low":
        click.echo("  warning: low-confidence detection; rerun with --project-type.")


def _confirm_low_confidence(detection: ProjectDetection, yes: bool) -> None:
    if detection.confidence != "low" or yes or want_json() or not sys.stdin.isatty():
        return
    if click.confirm(
        "Project type detection is low confidence. Continue with language = unknown?",
        default=False,
    ):
        return
    raise click.exceptions.Exit(code=1)


def _should_prompt_for_sentrux_baseline(yes: bool) -> bool:
    headless = not sys.stdin.isatty() or want_json()
    return not yes and not headless


def _maybe_create_sentrux_baseline(project_root: Path, *, yes: bool) -> tuple[Path, bool]:
    baseline_path = _sentrux_baseline_path(project_root)
    if baseline_path.exists() or not _sentrux_available():
        return baseline_path, False

    if _should_prompt_for_sentrux_baseline(yes) and not click.confirm(
        "Run `dgov sentrux gate-save` now to create the repo baseline?",
        default=True,
    ):
        return baseline_path, False

    ok, details = _save_sentrux_baseline(project_root)
    if ok:
        click.echo(f"Created {baseline_path}")
        return baseline_path, True
    click.echo(f"Could not create sentrux baseline: {details}", err=True)
    return baseline_path, False


def _print_init_next_steps(baseline_path: Path, baseline_created: bool) -> None:
    click.echo("Next:")
    click.echo("  1. Review .dgov/project.toml, .dgov/governor.md, and .dgov/sops/")
    if baseline_created or baseline_path.exists():
        click.echo(
            "  2. Refresh the architectural baseline with `dgov sentrux gate-save` "
            "when you intentionally reset it"
        )
    else:
        click.echo("  2. Run `dgov sentrux gate-save` to create the repo baseline")


@cli.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite bootstrap files")
@click.option("--yes", "-y", is_flag=True, help="Skip interactive prompts")
@click.option(
    "--project-type",
    type=click.Choice(_PROJECT_TYPE_CHOICES),
    default="auto",
    show_default=True,
    help="Override project type detection",
)
@click.option("--preflight", is_flag=True, help="Print bootstrap and worker-environment checks")
def init_cmd(force: bool, yes: bool, project_type: str, preflight: bool) -> None:
    """Bootstrap .dgov/project.toml and .dgov/governor.md.

    Auto-detects language, source directory, and test directory.
    """
    project_root = resolve_project_root()
    dgov_dir = project_root / ".dgov"
    config_path = dgov_dir / "project.toml"
    detection = _detect_project_details(project_root, project_type=project_type)
    _confirm_low_confidence(detection, yes)
    scope_ignore_files = _detect_scope_ignore_files(project_root, detection.language)
    toml_content = _render_project_toml(
        detection.language,
        detection.src_dir,
        detection.test_dir,
        detection.extensions,
        scope_ignore_files,
        project_root,
    )

    _prepare_runtime_dirs(project_root, dgov_dir)
    created = _write_init_files(config_path, toml_content, dgov_dir, force=force)

    if not created:
        _exit_already_initialized(dgov_dir)

    _print_created_paths(created)

    if config_path in created:
        _print_init_config_summary(detection, scope_ignore_files, project_root)
        baseline_path, baseline_created = _maybe_create_sentrux_baseline(project_root, yes=yes)
        if preflight:
            _print_init_preflight(project_root, detection)
        _print_init_next_steps(baseline_path, baseline_created)
