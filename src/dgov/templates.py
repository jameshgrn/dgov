"""Prompt template system for worker panes.

Templates provide structured, reusable prompts with variable substitution.
Built-in templates encode dgov conventions (uv run, ruff, pytest -q, git commit).
User templates live in .dgov/templates/*.toml and override built-ins by name.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from dgov.router import PaneRole


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
        default_agent="worker",
        description="Fix a bug in a single file with targeted tests",
    ),
    "feature": PromptTemplate(
        name="feature",
        template=(
            "Implement the following feature in {file}: {description}. "
            "Follow existing code patterns. Add tests in {test_file}. "
            "Run: uv run ruff check {file} && uv run pytest {test_file} -q. "
            'git add {file} {test_file} && git commit -m "Add: {description}"'
        ),
        required_vars=["file", "description", "test_file"],
        default_agent="worker",
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
        default_agent="worker",
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
        default_agent="worker",
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
        default_agent="supervisor",
        description="Review code and output structured JSON findings",
    ),
    PaneRole.LT_GOV: PromptTemplate(
        name=PaneRole.LT_GOV,
        template=(
            "You are a lieutenant governor (LT-GOV) managing a tier of workers.\n\n"
            "## Identity\n"
            "- Slug: {ltgov_slug}\n"
            "- Project root: $DGOV_PROJECT_ROOT\n"
            "- Role: sub-governor. You orchestrate workers via plans.\n\n"
            "## Important\n"
            "Multi-step work uses the plan-driven workflow:\n"
            "  1. Write a plan TOML under .dgov/plans/\n"
            "  2. Run it with: uv run dgov plan run .dgov/plans/<name>.toml\n"
            "  3. Monitor drives dispatch → review → merge → eval evidence → notify\n"
            "\n"
            "Ad-hoc `dgov pane create` is for "
            "single-file micro-tasks and emergency recovery only.\n"
            "ALL commands must include `-r $DGOV_PROJECT_ROOT` "
            "so your panes appear in the governor's dashboard.\n\n"
            "## Workflow\n"
            "1. Create a plan TOML with exact file claims and evals:\n"
            "   echo '[[evals]]\n"
            '   statement = "..."  # falsifiable\n'
            '   files = ["src/...", "tests/..."]\n'
            "   [[units]]\n"
            '   satisfies = ["eval-id"]\n'
            '   files = ["..."]\n'
            '   prompt = "..."  # numbered steps, read first, explicit commit\n'
            "   Save as .dgov/plans/{ltgov_slug}.toml\n"
            "2. Execute via: uv run dgov plan run .dgov/plans/{ltgov_slug}.toml\n"
            "3. Monitor drives the full lifecycle; do not ad-hoc dispatch.\n"
            "4. If a task fails twice at one tier or retries are exhausted, "
            "escalate to the supervisor (claude).\n"
            "5. On structural issues, write to .dgov/progress/{ltgov_slug}.json:\n"
            '   {{"status": "escalation", "reason": "..."}}\n'
            "   Then exit.\n\n"
            "## Rules\n"
            "- NEVER push to remote.\n"
            "- NEVER edit tracked files directly; run workers instead.\n"
            "- Enforce deterministic review, policy-core checks, and bounded retry before merge.\n"
            "- Signal completion by writing .dgov/progress/{ltgov_slug}.json.\n\n"
            "## When done\n"
            "Write to .dgov/progress/{ltgov_slug}.json:\n"
            '{{"status": "done", "merged": ["slug1"], "failed": ["slug2"], "summary": "..."}}\n'
            "Then exit."
        ),
        required_vars=["ltgov_slug", "task_list", "default_agent"],
        default_agent="manager",
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
