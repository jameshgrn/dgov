"""Init subcommand — project bootstrap and auto-detection."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import click

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
plans/archive/
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
    # Go
    "go.sum",
    # Ruby
    "Gemfile.lock",
    # Elixir
    "mix.lock",
)


def _detect_scope_ignore_files(root: Path, language: str) -> list[str]:
    """Return known managed files plus Python's uv.lock default."""
    detected = [name for name in _SCOPE_IGNORE_CANDIDATES if (root / name).is_file()]
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
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            data = {}

        candidates: list[object] = []
        project = data.get("project")
        if isinstance(project, dict):
            candidates.append(project.get("dependencies", ()))
            optional = project.get("optional-dependencies", {})
            if isinstance(optional, dict):
                candidates.extend(optional.values())
        dependency_groups = data.get("dependency-groups", {})
        if isinstance(dependency_groups, dict):
            candidates.extend(dependency_groups.values())

        for group in candidates:
            if not isinstance(group, list):
                continue
            for dep in group:
                if isinstance(dep, str) and _dependency_name(dep) == "pytest":
                    return True

    for name in ("requirements.txt", "requirements-dev.txt", "dev-requirements.txt"):
        req = root / name
        if not req.is_file():
            continue
        try:
            for line in req.read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if _dependency_name(stripped) == "pytest":
                    return True
        except OSError:
            continue
    return False


def _detect_js_tooling(root: Path) -> dict[str, str]:
    """Detect JavaScript/TypeScript tooling from config files.

    Returns a dict with keys: test_cmd, lint_cmd, format_cmd, lint_fix_cmd,
    format_check_cmd, type_check_cmd. Empty string means not detected.
    """
    result: dict[str, str] = {
        "test_cmd": "",
        "lint_cmd": "",
        "format_cmd": "",
        "lint_fix_cmd": "",
        "format_check_cmd": "",
        "type_check_cmd": "",
    }

    # Check for eslint config files
    eslint_configs = [
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
    ]
    has_eslint = any((root / cfg).is_file() for cfg in eslint_configs)

    # Check for biome config
    has_biome = any((root / cfg).is_file() for cfg in ["biome.json", "biome.jsonc"])

    # Check for prettier config
    prettier_configs = [
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
    ]
    has_prettier = any((root / cfg).is_file() for cfg in prettier_configs)

    # Check for test runners
    has_vitest = any(
        (root / cfg).is_file()
        for cfg in [
            "vitest.config.js",
            "vitest.config.mjs",
            "vitest.config.ts",
            "vitest.config.mts",
        ]
    )
    has_jest = any(
        (root / cfg).is_file()
        for cfg in ["jest.config.js", "jest.config.ts", "jest.config.mjs", "jest.config.cjs"]
    )

    # Check package.json for test script
    has_npm_test = False
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            import json

            with package_json.open(encoding="utf-8") as f:
                pkg = json.load(f)
            scripts = pkg.get("scripts", {})
            has_npm_test = "test" in scripts
        except Exception:
            pass

    # Check for tsconfig.json
    has_tsconfig = (root / "tsconfig.json").is_file()

    # Set lint commands
    if has_eslint:
        result["lint_cmd"] = "npx eslint {file}"
        result["lint_fix_cmd"] = "npx eslint --fix {file}"
    elif has_biome:
        result["lint_cmd"] = "npx biome check {file}"
        result["lint_fix_cmd"] = "npx biome check --write {file}"

    # Set format commands
    if has_prettier:
        result["format_cmd"] = "npx prettier --write {file}"
        result["format_check_cmd"] = "npx prettier --check {file}"
    elif has_biome:
        result["format_cmd"] = "npx biome format --write {file}"
        result["format_check_cmd"] = "npx biome format {file}"

    # Set test command
    if has_vitest:
        result["test_cmd"] = "npx vitest run {test_dir}"
    elif has_jest:
        result["test_cmd"] = "npx jest"
    elif has_npm_test:
        result["test_cmd"] = "npm test"

    # Set type check command
    if has_tsconfig:
        result["type_check_cmd"] = "npx tsc --noEmit"

    return result


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
    language = max(counts, key=lambda k: counts.get(k, 0))
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
    cmds = _LANG_TEMPLATES.get(language, _LANG_TEMPLATES["python"])

    # For JavaScript, detect actual tooling and override defaults
    if language == "javascript" and project_root is not None:
        detected = _detect_js_tooling(project_root)
        cmds = {**cmds, **detected}
    elif (
        language == "python"
        and project_root is not None
        and _python_project_uses_pytest(project_root)
    ):
        coverage_source = src_dir.rstrip("/") or "."
        cmds = {
            **cmds,
            "coverage_cmd": (
                f"uv run pytest --cov={coverage_source} --cov-report=json:{{output}} -q"
            ),
        }

    ext_str = ", ".join(f'"{e}"' for e in extensions)
    ignore_files = scope_ignore_files or []

    def render_cmd(key: str, hint: str) -> str:
        """Render a command line, showing empty strings as commented hints."""
        value = cmds.get(key, "")
        if value:
            return f'{key} = "{value}"'
        return f'{key} = ""  # {hint}'

    # Hints shown as comments when a tool isn't detected
    test_hint = "Not detected. Try: 'npx vitest run {test_dir}'"
    lint_hint = "Not detected. Try: 'npx eslint {file}'"
    format_hint = "Not detected. Try: 'npx prettier --write {file}'"
    lint_fix_hint = "Not detected. Try: 'npx eslint --fix {file}'"
    format_check_hint = "Not detected. Try: 'npx prettier --check {file}'"
    type_check_hint = "Not detected. Try: 'npx tsc --noEmit'"

    lines = [
        "[project]",
        f'language = "{language}"',
        f'src_dir = "{src_dir}"',
        f'test_dir = "{test_dir}"',
        f"source_extensions = [{ext_str}]",
        "",
        "# OpenAI-compatible worker endpoint",
        'default_agent = "accounts/fireworks/routers/kimi-k2p5-turbo"',
        'llm_base_url = "https://api.fireworks.ai/inference/v1"',
        'llm_api_key_env = "FIREWORKS_API_KEY"',
        "",
        "# Sentrux baseline is explicit governor-owned state.",
        '# Run "dgov sentrux gate-save" after bootstrap and whenever you intentionally',
        "# refresh the architectural baseline for this repo.",
        "",
        render_cmd("test_cmd", test_hint),
        render_cmd("lint_cmd", lint_hint),
        render_cmd("format_cmd", format_hint),
        render_cmd("lint_fix_cmd", lint_fix_hint),
        render_cmd("format_check_cmd", format_check_hint),
    ]

    # Add type_check_cmd for JavaScript or as commented example for others
    if language == "javascript":
        lines.append(render_cmd("type_check_cmd", type_check_hint))
    else:
        lines.append("# type_check_cmd = \"\"  # Optional: e.g. 'uv run ty check' for Python")

    coverage_cmd = cmds.get("coverage_cmd")
    if coverage_cmd:
        lines.append(f'coverage_cmd = "{coverage_cmd}"')
    else:
        lines.append(
            '# coverage_cmd = ""  # Optional: e.g. '
            "'uv run pytest --cov=src --cov-report=json:{output} -q'"
        )
    lines.append("coverage_threshold = 2.0")

    # setup_cmd: runs in worktree before lint/test/format gates
    if language == "javascript":
        lines.append(
            'setup_cmd = "npm ci --ignore-scripts 2>/dev/null'
            ' || npm install --ignore-scripts 2>/dev/null"'
        )
    else:
        lines.append('# setup_cmd = ""  # Runs in worktree before gates')

    lines.extend([
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
        "[tool_policy]",
        "restrict_run_bash = true",
        'deny_shell_commands = ["pip", "python -m pip", "pip3", "python -m venv", "uv venv"]',
        "deny_shell_file_mutations = true",
        f"require_wrapped_verify_tools = {'true' if language == 'python' else 'false'}",
        f"require_uv_run = {'true' if language == 'python' else 'false'}",
        "",
        "[scope]",
        "# Files exempted from scope-violation checks in addition to dgov's built-in",
        "# Python defaults: .venv, uv.lock, __pycache__, and *.pyc.",
        "# Add repo-specific managed files here when workers may touch them incidentally.",
        "# Exact paths, directory names, and constrained globs like *.pyc are supported.",
        f"ignore_files = [{', '.join(f'"{name}"' for name in ignore_files)}]",
        "",
        "[conventions]",
        "# Add project-specific rules here for the agent to follow",
        '# style = "Prefer functional over OOP"',
    ])
    return "\n".join(lines) + "\n"


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


@cli.command(name="init")
@click.option("--force", is_flag=True, help="Overwrite bootstrap files")
@click.option("--yes", "-y", is_flag=True, help="Skip interactive prompts")
def init_cmd(force: bool, yes: bool) -> None:
    """Bootstrap .dgov/project.toml and .dgov/governor.md.

    Auto-detects language, source directory, and test directory.
    """
    project_root = resolve_project_root()
    dgov_dir = project_root / ".dgov"
    config_path = dgov_dir / "project.toml"
    language, src_dir, test_dir, extensions = _detect_project(project_root)
    scope_ignore_files = _detect_scope_ignore_files(project_root, language)
    toml_content = _render_project_toml(
        language, src_dir, test_dir, extensions, scope_ignore_files, project_root
    )

    dgov_dir.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(dgov_dir, _DGOV_GITIGNORE)

    sentrux_dir = project_root / ".sentrux"
    sentrux_dir.mkdir(parents=True, exist_ok=True)
    _ensure_gitignore(sentrux_dir, _SENTRUX_GITIGNORE)

    created: list[Path] = []

    if force or not config_path.exists():
        config_path.write_text(toml_content)
        created.append(config_path)

    created.extend(
        _write_bootstrap_files(
            _bootstrap_policy_targets(dgov_dir),
            force=force,
        )
    )

    if not created:
        click.echo(f"Already initialized: {dgov_dir}")
        click.echo("Use --force to overwrite bootstrap files.")
        raise click.exceptions.Exit(code=1)

    for path in created:
        click.echo(f"Created {path}")

    if config_path in created:
        click.echo(f"  language: {language}")
        click.echo(f"  src_dir:  {src_dir}")
        click.echo(f"  test_dir: {test_dir}")
        if scope_ignore_files:
            click.echo(f"  scope.ignore_files: {', '.join(scope_ignore_files)}")
        baseline_path = _sentrux_baseline_path(project_root)
        baseline_created = False

        headless = not sys.stdin.isatty() or want_json()
        should_prompt = not yes and not headless

        if not baseline_path.exists() and _sentrux_available():
            if should_prompt:
                create_baseline = click.confirm(
                    "Run `dgov sentrux gate-save` now to create the repo baseline?",
                    default=True,
                )
                if create_baseline:
                    ok, details = _save_sentrux_baseline(project_root)
                    if ok:
                        click.echo(f"Created {baseline_path}")
                        baseline_created = True
                    else:
                        click.echo(f"Could not create sentrux baseline: {details}", err=True)
            else:
                # Automate if --yes or headless
                ok, details = _save_sentrux_baseline(project_root)
                if ok:
                    click.echo(f"Created {baseline_path}")
                    baseline_created = True
                else:
                    click.echo(f"Could not create sentrux baseline: {details}", err=True)

        click.echo("Next:")
        click.echo("  1. Review .dgov/project.toml, .dgov/governor.md, and .dgov/sops/")
        if baseline_created or baseline_path.exists():
            click.echo(
                "  2. Refresh the architectural baseline with `dgov sentrux gate-save` "
                "when you intentionally reset it"
            )
        else:
            click.echo("  2. Run `dgov sentrux gate-save` to create the repo baseline")
