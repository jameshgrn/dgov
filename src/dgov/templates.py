"""Prompt template system for worker panes.

Templates provide structured, reusable prompts with variable substitution.
Built-in templates encode dgov conventions (uv run, ruff, pytest -q, git commit).
User templates live in .dgov/templates/*.toml and override built-ins by name.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PromptTemplate:
    name: str
    template: str
    required_vars: list[str]
    default_agent: str | None = None
    description: str = ""


BUILT_IN_TEMPLATES: dict[str, PromptTemplate] = {
    "bugfix": PromptTemplate(
        name="bugfix",
        template=(
            "Read {file}. Find the bug described: {description}. Fix it. "
            "Run tests with: uv run pytest {test_file} -q. "
            'git add {file} && git commit -m "Fix: {description}"'
        ),
        required_vars=["file", "description", "test_file"],
        default_agent="pi",
        description="Fix a bug in a single file with targeted tests",
    ),
    "feature": PromptTemplate(
        name="feature",
        template=(
            "Implement the following feature in {file}: {description}. "
            "Follow existing code patterns. Add tests in {test_file}. "
            "Run: uv run ruff check {file} && uv run pytest {test_file} -q. "
            'git add -A && git commit -m "Add: {description}"'
        ),
        required_vars=["file", "description", "test_file"],
        default_agent="claude",
        description="Add a new feature with tests",
    ),
    "refactor": PromptTemplate(
        name="refactor",
        template=(
            "Refactor {file}: {description}. Preserve all existing behavior. "
            "Run full test suite for the module: uv run pytest {test_file} -q. "
            'git add {file} && git commit -m "Refactor: {description}"'
        ),
        required_vars=["file", "description", "test_file"],
        default_agent="pi",
        description="Refactor code while preserving behavior",
    ),
    "test": PromptTemplate(
        name="test",
        template=(
            "Read {file}. Write comprehensive tests in {test_file} covering: "
            "happy path, edge cases, error handling. Follow existing test patterns "
            "in the repo. Run: uv run pytest {test_file} -q. "
            'git add {test_file} && git commit -m "Add tests for {file}"'
        ),
        required_vars=["file", "test_file"],
        default_agent="pi",
        description="Write tests for an existing file",
    ),
    "review": PromptTemplate(
        name="review",
        template=(
            "Review all Python files in {directory}. Output findings as a JSON "
            "array with fields: file, line, severity (critical/medium/low), "
            "category, description, suggested_fix. Be thorough but avoid "
            "style-only nitpicks."
        ),
        required_vars=["directory"],
        default_agent="claude",
        description="Review code and output structured JSON findings",
    ),
    "lt-gov": PromptTemplate(
        name="lt-gov",
        template=(
            "You are a lieutenant governor (LT-GOV) managing a tier of workers.\n\n"
            "## Identity\n"
            "- Slug: {ltgov_slug}\n"
            "- Project root: {project_root}\n"
            "- Role: sub-governor. You orchestrate workers. You do NOT edit code directly.\n\n"
            "## Workers to dispatch\n"
            "{task_list}\n\n"
            "## Workflow\n"
            "For each worker:\n"
            '1. dgov pane create -a {default_agent} -p "<task prompt>" '
            "-r {project_root} --parent {ltgov_slug}\n"
            "2. dgov pane wait <slug> -t 300\n"
            "3. dgov pane review <slug>\n"
            "4. If passes: dgov pane merge-request <slug>\n"
            "5. If fails: dgov pane close <slug>, retry with better prompt or escalate to claude\n"
            "6. dgov pane close <slug>\n\n"
            "## Rules\n"
            "- NEVER push to remote\n"
            "- NEVER edit files directly\n"
            "- NEVER run dgov pane merge directly — use dgov pane merge-request\n"
            "- If a worker fails twice on the same task, escalate to claude\n"
            "- If you hit a structural problem, write to .dgov/progress/{ltgov_slug}.json:\n"
            '  {"status": "escalation", "reason": "..."}\n'
            "  Then exit.\n\n"
            "## When done\n"
            "Write to .dgov/progress/{ltgov_slug}.json:\n"
            '{"status": "done", "merged": ["slug1"], "failed": ["slug2"], "summary": "..."}\n'
            "Then exit."
        ),
        required_vars=["ltgov_slug", "project_root", "task_list", "default_agent"],
        default_agent="claude",
        description="Meta-prompt for a lieutenant governor managing a worker tier",
    ),
}


def _load_template_file(path: Path) -> PromptTemplate:
    """Load a single .toml template file."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    name = data.get("name", path.stem)
    template = data["template"]
    required_vars = list(data.get("required_vars", []))
    default_agent = data.get("default_agent")
    description = data.get("description", "")
    return PromptTemplate(
        name=name,
        template=template,
        required_vars=required_vars,
        default_agent=default_agent,
        description=description,
    )


def load_templates(session_root: str) -> dict[str, PromptTemplate]:
    """Load templates: built-ins merged with user templates from .dgov/templates/.

    User templates override built-ins by name.
    """
    templates = dict(BUILT_IN_TEMPLATES)
    templates_dir = Path(session_root) / ".dgov" / "templates"
    if templates_dir.is_dir():
        for toml_file in sorted(templates_dir.glob("*.toml")):
            tpl = _load_template_file(toml_file)
            templates[tpl.name] = tpl
    return templates


def render_template(template: PromptTemplate, vars: dict[str, str]) -> str:
    """Substitute {var} placeholders in a template.

    Raises ValueError if any required var is missing.
    Extra vars are ignored.
    """
    missing = [v for v in template.required_vars if v not in vars]
    if missing:
        raise ValueError(
            f"Template '{template.name}' missing required variables: {', '.join(missing)}"
        )
    return template.template.format_map(vars)


def list_templates(session_root: str) -> list[dict]:
    """Return template metadata for display."""
    templates = load_templates(session_root)
    return [
        {
            "name": tpl.name,
            "description": tpl.description,
            "required_vars": tpl.required_vars,
            "default_agent": tpl.default_agent,
        }
        for tpl in templates.values()
    ]
