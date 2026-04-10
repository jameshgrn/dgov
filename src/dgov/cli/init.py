"""Init subcommand — project bootstrap and auto-detection."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import click

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
        f'test_cmd = "{cmds["test_cmd"]}"',
        f'lint_cmd = "{cmds["lint_cmd"]}"',
        f'format_cmd = "{cmds["format_cmd"]}"',
        f'lint_fix_cmd = "{cmds["lint_fix_cmd"]}"',
        f'format_check_cmd = "{cmds["format_check_cmd"]}"',
        "# type_check_cmd = \"\"  # Optional: e.g. 'uv run ty check' for Python",
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
        "[conventions]",
        "# Add project-specific rules here for the agent to follow",
        '# style = "Prefer functional over OOP"',
    ]
    return "\n".join(lines) + "\n"


def _render_governor_md() -> str:
    """Render the repo-local governor charter."""
    return """# Governor Charter

This file is the repo-local contract for the governor. Read it before authoring
plans, retrying failed work, or changing task boundaries.

## Purpose

The governor is responsible for making AI coding work deterministic at the
system level. Workers may be probabilistic. Governance should not be.

## Core Principles

- Plan first. Do not dispatch work that has not been thought through.
- Keep tasks atomic. One task should produce one logical change.
- Respect file claims. A task must only edit files it explicitly claims.
- Prefer explicit contracts over clever prompts.
- Fail closed. If structure or scope is unclear, stop and fix the plan.

## Planning Rules

- Split work into units with clear summaries, prompts, and commit messages.
- Use dependencies only for real ordering constraints.
- Avoid broad exploratory tasks. Break them into concrete units.
- Put repo-wide implementation guidance in `.dgov/sops/`, not in ad hoc task text.
- Keep provider config and project conventions in `.dgov/project.toml`.

## Task Authoring Rules

- Every task must declare file claims.
- Prompts should follow: orient, edit, verify.
- Commit messages must be imperative and reflect one logical change.
- If a task needs different model behavior, override `agent`; do not restate
  general governance rules in the task prompt.

## Retry And Failure Rules

- Retry only when the task is still well-scoped and the failure is fixable.
- If the worker exposed a planning flaw, change the plan before retrying.
- If settlement rejects for scope, do not brute-force retry.
- If a failure points to repo-wide guidance drift, update the relevant SOP or
  this charter.

## Scope Rules

- Governance rules live here.
- Worker execution guidance lives in `.dgov/sops/*.md`.
- `.sentrux/baseline.json` is governor-owned state. Refresh it explicitly with
  `dgov sentrux gate-save`; workers must not edit it.
- Hard invariants live in code and settlement gates.
- Do not use this file as a dump for project-specific style trivia. Keep it
  focused on planning, dispatch, retry, and done criteria.

## State Modeling

- Treat state-model cleanup as architecture work, not incidental polish.
- If a task reveals state bloat, contradictory flags, or grab-bag models,
  either make the refactor explicit in the task or split it into a follow-up.
- Prefer designs where invalid states are impossible, not just discouraged.
- Prefer derivation from durable evidence like events over storing redundant
  booleans or cached conclusions.
- Do not smuggle broad state-model rewrites into unrelated tasks just because
  the worker noticed a smell.

## Done Criteria

- The plan is structurally valid.
- Tasks are scoped tightly enough to review and retry safely.
- Guidance is obvious enough that the worker should not need to infer policy.
- Settlement can verify the result with declared commands and gates.
"""


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
    governor_path = dgov_dir / "governor.md"

    language, src_dir, test_dir, extensions = _detect_project(project_root)
    toml_content = _render_project_toml(language, src_dir, test_dir, extensions)
    governor_content = _render_governor_md()

    dgov_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []

    if force or not config_path.exists():
        config_path.write_text(toml_content)
        created.append(config_path)

    if force or not governor_path.exists():
        governor_path.write_text(governor_content)
        created.append(governor_path)

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
        click.echo("  1. Review .dgov/project.toml and .dgov/governor.md")
        if baseline_created or baseline_path.exists():
            click.echo(
                "  2. Refresh the architectural baseline with `dgov sentrux gate-save` "
                "when you intentionally reset it"
            )
        else:
            click.echo("  2. Run `dgov sentrux gate-save` to create the repo baseline")
