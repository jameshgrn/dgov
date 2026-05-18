"""Compile subcommand — plan tree compilation pipeline."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import click

from dgov.cli import cli, load_project_config_or_exit, print_dag_graph, want_json
from dgov.project_root import resolve_project_root

if TYPE_CHECKING:
    from dgov.config import ProjectConfig
    from dgov.plan import PlanIssue
    from dgov.plan_tree import FlatPlan, PlanTree
    from dgov.sop_bundler import BundleResult, SopBundler


@dataclass(frozen=True)
class _SopCache:
    mapping: dict[str, tuple[str, ...]] | None = None
    sop_set_hash: str | None = None


@dataclass(frozen=True)
class _CompileSummary:
    output_path: Path
    unit_count: int
    edge_count: int
    sop_count: int
    sop_set_hash: str
    dry_run: bool
    warnings: tuple[PlanIssue, ...]


@cli.command(name="compile")
@click.argument("plan_root", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.option("--dry-run", is_flag=True, help="Use identity SOP bundler (no LLM call)")
@click.option(
    "--recompile-sops", is_flag=True, help="Force SOP re-assignment even if hash matches"
)
@click.option("--graph", is_flag=True, help="Print DAG shape after compile")
def compile_cmd(plan_root: Path, dry_run: bool, recompile_sops: bool, graph: bool) -> None:
    """Compile a plan tree into _compiled.toml.

    Walks the plan tree, merges units, resolves refs, validates the DAG,
    bundles SOPs, and writes a dispatch-ready _compiled.toml.

    \b
    Example: dgov compile .dgov/plans/my-plan/
    """
    compile_plan_dir(plan_root, dry_run=dry_run, recompile_sops=recompile_sops, graph=graph)


def compile_plan_dir(plan_root: Path, *, dry_run: bool, recompile_sops: bool, graph: bool) -> None:
    """Compile pipeline: walk → merge → resolve → validate → bundle → write."""
    resolved = _load_resolved_plan(plan_root)
    project_root = _resolve_compile_project_root(plan_root)
    project_config = _load_project_config(project_root)
    bundle_result = _bundle_sops(
        resolved,
        plan_root,
        project_root=project_root,
        dry_run=dry_run,
        recompile_sops=recompile_sops,
    )
    out_path = _write_compiled_plan(plan_root, bundle_result, resolved)
    warnings = _validate_compiled_plan(
        out_path,
        project_config,
        project_root,
        require_provider=not dry_run,
    )
    summary = _build_summary(out_path, resolved, bundle_result, dry_run, warnings)

    _print_summary(summary)
    _print_graph_if_requested(graph, resolved)


def _resolve_compile_project_root(plan_root: Path) -> Path:
    """Resolve config from a project-scoped plan, preserving standalone plan behavior."""
    project_root = resolve_project_root(plan_root)
    if _is_project_root(project_root):
        return project_root
    return resolve_project_root()


def _is_project_root(path: Path) -> bool:
    """True when ``path`` carries a project marker.

    ``resolve_project_root`` returns the literal input when no marker is found
    upstream, so this distinguishes a real hit from the no-marker fallback.
    """
    return (path / ".dgov").is_dir() or (path / ".git").exists()


def _load_resolved_plan(plan_root: Path) -> FlatPlan:
    tree = _walk_plan_tree(plan_root)
    flat = _merge_plan_tree(tree)
    _ensure_plan_has_units(flat)
    resolved = _resolve_plan_refs(flat)
    _validate_dag(resolved)
    return resolved


def _walk_plan_tree(plan_root: Path) -> PlanTree:
    from dgov.plan_tree import walk_tree

    try:
        return walk_tree(plan_root)
    except (FileNotFoundError, ValueError) as exc:
        _exit_with_error(f"Error: {exc}")


def _merge_plan_tree(tree: PlanTree) -> FlatPlan:
    from dgov.plan_tree import merge_tree

    try:
        return merge_tree(tree)
    except ValueError as exc:
        _exit_with_error(f"Error: {exc}")


def _ensure_plan_has_units(plan: FlatPlan) -> None:
    if not plan.units:
        diagnostic = _empty_plan_tree_diagnostic(plan)
        _exit_with_error(f"Error: plan tree has no units. {diagnostic}")


def _empty_plan_tree_diagnostic(plan: FlatPlan) -> str:
    declared_sections = set(plan.root_meta.sections)
    undeclared_sections = _undeclared_task_sections(plan.plan_root, declared_sections)
    if undeclared_sections:
        return (
            "Found task TOML files in undeclared section directories: "
            f"{', '.join(undeclared_sections)}. Add the section name to _root.toml "
            "[plan].sections or move the task file into a declared section."
        )
    if not declared_sections:
        return (
            "_root.toml [plan].sections is empty. Declare at least one section and "
            "add a depth-1 TOML file containing [tasks.<slug>]."
        )

    visible_files = _visible_task_toml_files(plan.plan_root, plan.root_meta.sections)
    if visible_files:
        return (
            "Task TOML files were found but no [tasks.<slug>] tables were parsed: "
            f"{', '.join(visible_files)}."
        )

    ignored_files = _ignored_task_toml_files(plan.plan_root, plan.root_meta.sections)
    if ignored_files:
        return (
            "Declared sections contain only ignored TOML files: "
            f"{', '.join(ignored_files)}. Rename a task file without a leading '_' "
            "or '.' prefix."
        )

    nested_files = _nested_task_toml_files(plan.plan_root, plan.root_meta.sections)
    if nested_files:
        return (
            "Task TOML files are nested below section directories, but plan trees "
            f"read only depth-1 files: {', '.join(nested_files)}."
        )

    return (
        "Declared sections contain no task TOML files. Add a depth-1 TOML file "
        "containing [tasks.<slug>] to one of: "
        f"{', '.join(plan.root_meta.sections)}."
    )


def _visible_task_toml_files(plan_root: Path, sections: tuple[str, ...]) -> list[str]:
    files: list[str] = []
    for section in sections:
        section_dir = plan_root / section
        if not section_dir.is_dir():
            continue
        files.extend(
            str(path.relative_to(plan_root))
            for path in sorted(section_dir.iterdir())
            if path.is_file() and path.suffix == ".toml" and not path.name.startswith((".", "_"))
        )
    return files


def _ignored_task_toml_files(plan_root: Path, sections: tuple[str, ...]) -> list[str]:
    files: list[str] = []
    for section in sections:
        section_dir = plan_root / section
        if not section_dir.is_dir():
            continue
        files.extend(
            str(path.relative_to(plan_root))
            for path in sorted(section_dir.iterdir())
            if path.is_file() and path.suffix == ".toml" and path.name.startswith((".", "_"))
        )
    return files


def _nested_task_toml_files(plan_root: Path, sections: tuple[str, ...]) -> list[str]:
    files: list[str] = []
    for section in sections:
        section_dir = plan_root / section
        if not section_dir.is_dir():
            continue
        files.extend(
            str(path.relative_to(plan_root))
            for path in sorted(section_dir.rglob("*.toml"))
            if path.is_file() and path.parent != section_dir
        )
    return files


def _undeclared_task_sections(plan_root: Path, declared_sections: set[str]) -> list[str]:
    sections: list[str] = []
    for path in sorted(plan_root.iterdir()):
        if (
            path.is_dir()
            and path.name not in declared_sections
            and path.name != "archive"
            and _visible_task_toml_files(plan_root, (path.name,))
        ):
            sections.append(path.name)
    return sections


def _resolve_plan_refs(plan: FlatPlan) -> FlatPlan:
    from dgov.plan_tree import resolve_refs

    try:
        return resolve_refs(plan)
    except ValueError as exc:
        _exit_with_error(f"Error: {exc}")


def _validate_dag(plan: FlatPlan) -> None:
    from dgov.plan_tree import validate

    report = validate(plan)
    if not (report.cycles or report.unreachable):
        return

    for cycle in report.cycles:
        click.echo(f"  ERROR: cycle: {' → '.join(cycle)}", err=True)
    if report.unreachable:
        click.echo(f"  ERROR: unreachable: {', '.join(report.unreachable)}", err=True)
    raise click.exceptions.Exit(code=1) from None


def _load_project_config(project_root: Path) -> ProjectConfig:
    return load_project_config_or_exit(project_root)


def _bundle_sops(
    plan: FlatPlan,
    plan_root: Path,
    *,
    project_root: Path,
    dry_run: bool,
    recompile_sops: bool,
) -> BundleResult:
    sops_dir = project_root / ".dgov" / "sops"
    bundler = _select_sop_bundler(dry_run)
    cache = _load_sop_cache(plan_root / "_compiled.toml", recompile_sops=recompile_sops)
    result = _run_sop_bundle(plan, sops_dir, bundler, cache)

    if dry_run:
        result = replace(result, sop_set_hash="")
    _print_sop_assignments(result, dry_run=dry_run)
    return result


def _select_sop_bundler(dry_run: bool) -> SopBundler:
    from dgov.sop_bundler import IdentityBundler, TagBasedSopBundler

    return IdentityBundler() if dry_run else TagBasedSopBundler()


def _load_sop_cache(compiled_path: Path, *, recompile_sops: bool) -> _SopCache:
    if recompile_sops or not compiled_path.exists():
        return _SopCache()

    try:
        from dgov.dag_parser import parse_dag_file

        old = parse_dag_file(str(compiled_path))
    except Exception:
        return _SopCache()

    return _SopCache(
        mapping={uid: task.sop_mapping for uid, task in old.tasks.items()},
        sop_set_hash=old.sop_set_hash,
    )


def _run_sop_bundle(
    plan: FlatPlan,
    sops_dir: Path,
    bundler: SopBundler,
    cache: _SopCache,
) -> BundleResult:
    from dgov.sop_bundler import bundle

    try:
        return bundle(
            plan,
            sops_dir,
            bundler,
            cached_mapping=cache.mapping,
            cached_hash=cache.sop_set_hash,
        )
    except Exception as exc:
        _exit_with_error(f"  ERROR: {exc!s}")


def _print_sop_assignments(result: BundleResult, *, dry_run: bool) -> None:
    if dry_run:
        return

    for uid, names in sorted(result.sop_mapping.items()):
        if names:
            click.echo(f"  SOPs: {uid.split('.')[-1]} → {', '.join(names)}", err=True)
    no_sop_units = [
        uid
        for uid, names in result.sop_mapping.items()
        if not names and _unit_needs_sop_attention(result.plan.units[uid])
    ]
    if no_sop_units:
        click.echo(
            f"  WARNING: {len(no_sop_units)} unit(s) matched zero SOPs",
            err=True,
        )
        for uid in no_sop_units[:5]:
            click.echo(
                "  WARNING"
                f" [{uid}]: zero SOP match. The deterministic matcher uses summary words, "
                "file extensions, test paths, and role. Add matching applies_to tags or set "
                'sop_mapping = ["sop-name"] in the source task.',
                err=True,
            )
        if len(no_sop_units) > 5:
            click.echo(f"  WARNING: ... and {len(no_sop_units) - 5} more", err=True)


_SOP_ATTENTION_EXTENSIONS = (".py", ".swift", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go")


def _unit_needs_sop_attention(unit) -> bool:
    paths = (
        *unit.files.create,
        *unit.files.edit,
        *unit.files.touch,
        *unit.files.delete,
    )
    return any(_path_needs_sop_attention(path) for path in paths)


def _path_needs_sop_attention(path: str) -> bool:
    normalized = path.strip().lstrip("./").lower()
    parts = tuple(part for part in normalized.split("/") if part)
    return normalized.endswith(_SOP_ATTENTION_EXTENSIONS) or any(
        part in {"test", "tests"} for part in parts[:-1]
    )


def _write_compiled_plan(
    plan_root: Path,
    result: BundleResult,
    resolved: FlatPlan,
) -> Path:
    from dgov.serializer import serialize_compiled_toml

    toml_str = serialize_compiled_toml(result, resolved.source_mtime_max)
    out_path = plan_root / "_compiled.toml"
    out_path.write_text(toml_str)
    return out_path


def _validate_compiled_plan(
    out_path: Path,
    project_config: ProjectConfig,
    project_root: Path,
    *,
    require_provider: bool,
) -> tuple[PlanIssue, ...]:
    from dgov.plan import parse_plan_file, validate_plan

    compiled_spec = parse_plan_file(str(out_path))
    plan_issues = validate_plan(compiled_spec, departments=project_config.departments)
    if require_provider:
        plan_issues.extend(_execution_resolution_issues(compiled_spec, project_config))
    plan_issues.extend(_setup_cmd_warnings(compiled_spec, project_config, project_root))
    plan_issues.extend(_prompt_tool_warnings(compiled_spec, project_config, project_root))
    plan_issues.extend(_archive_ignore_warnings(project_root))
    plan_errors = tuple(i for i in plan_issues if i.severity == "error")
    plan_warnings = tuple(i for i in plan_issues if i.severity == "warning")

    if plan_errors:
        out_path.unlink(missing_ok=True)
        _print_plan_errors(plan_errors)
        raise click.exceptions.Exit(code=1) from None

    return plan_warnings


def _execution_resolution_issues(plan, project_config: ProjectConfig) -> list[PlanIssue]:
    from dgov.plan import validate_execution_resolution

    return validate_execution_resolution(
        plan,
        project_agent=project_config.default_agent,
        project_provider=project_config.llm_provider,
        provider_agents=project_config.provider_default_agents(),
        provider_names=tuple(project_config.providers),
        require_provider=True,
    )


_SETUP_PATH_RE = re.compile(r"(?<![\w/.-])([\w.-]+\.(?:yml|yaml|json|toml|xcodeproj|xcworkspace))")


def _setup_cmd_warnings(
    plan,
    project_config: ProjectConfig,
    project_root: Path,
) -> list[PlanIssue]:
    from dgov.plan import PlanIssue

    setup_cmd = (project_config.setup_cmd or "").strip()
    if not setup_cmd:
        return []
    created = {
        path
        for unit in plan.units.values()
        for path in (*unit.files.create, *unit.files.touch)
        if path.strip()
    }
    warnings: list[PlanIssue] = []
    for path in sorted(set(_SETUP_PATH_RE.findall(setup_cmd))):
        if _setup_cmd_guards_path(setup_cmd, path) or (project_root / path).exists():
            continue
        if path in created:
            message = (
                f"setup_cmd references {path!r}, which this plan creates. setup_cmd runs before "
                "the first worker task. Guard it, for example: "
                f"`if [ -f {path} ]; then ...; fi`."
            )
        else:
            message = (
                f"setup_cmd references missing input {path!r}. "
                "Worker preparation will fail unless the command guards the missing file "
                "or the file exists before run."
            )
        warnings.append(PlanIssue(severity="warning", message=message))
    return warnings


def _setup_cmd_guards_path(setup_cmd: str, path: str) -> bool:
    return f"[ -f {path} ]" in setup_cmd or f"test -f {path}" in setup_cmd


_PROMPT_COMMAND_RE = re.compile(r"`([^`\n]+)`")
_PROMPT_HEADING_RE = re.compile(r"^\s*(?:#{1,6}\s+)?(?:\*\*)?(orient|edit|verify)\b", re.I)
_PROMPT_ORIENT_RE = re.compile(r"^\s*(?:#{1,6}\s+)?(?:\*\*)?orient\b", re.I | re.M)
_MARKDOWN_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S+")
_VERIFY_COMMAND_TOOLS = {
    "actionlint",
    "bun",
    "cargo",
    "dgov",
    "git",
    "go",
    "make",
    "npm",
    "npx",
    "pnpm",
    "python",
    "python3",
    "pytest",
    "rg",
    "ruff",
    "shellcheck",
    "shfmt",
    "swift",
    "ty",
    "uv",
    "xcodebuild",
    "xcrun",
}
_SHELL_BUILTINS = {
    "[",
    "cd",
    "command",
    "echo",
    "export",
    "false",
    "if",
    "printf",
    "pwd",
    "set",
    "test",
    "then",
    "true",
}


def _prompt_tool_warnings(
    plan,
    project_config: ProjectConfig,
    project_root: Path,
) -> list[PlanIssue]:
    from dgov.plan import PlanIssue

    worker_path = _worker_path(project_root, project_config)
    warnings: list[PlanIssue] = []
    for unit_id, unit in plan.units.items():
        seen: set[str] = set()
        for command in _verify_prompt_commands(unit.prompt):
            tool = _verification_tool(command)
            if not tool or tool in seen or _tool_available(tool, worker_path, project_root):
                continue
            seen.add(tool)
            warnings.append(
                PlanIssue(
                    severity="warning",
                    unit=unit_id,
                    message=(
                        f"Verify command references tool {tool!r}, which is not available "
                        "in the worker PATH. Configure the tool in .dgov/project.toml "
                        "or use an available wrapper."
                    ),
                )
            )
    return warnings


def _worker_path(project_root: Path, project_config: ProjectConfig) -> str:
    from dgov.workers.atomic import AtomicTools

    tools = AtomicTools(project_root, project_config)
    try:
        return tools._sandbox_env()["PATH"]
    finally:
        shutil.rmtree(tools._sandbox_home, ignore_errors=True)


def _verify_prompt_commands(prompt: str) -> list[str]:
    commands: list[str] = []
    in_verify = False
    for line in _task_prompt_body(prompt).splitlines():
        if heading := _PROMPT_HEADING_RE.match(line):
            in_verify = heading.group(1).lower() == "verify"
            continue
        if _MARKDOWN_HEADING_RE.match(line):
            in_verify = False
            continue
        if not in_verify:
            continue
        commands.extend(
            command
            for match in _PROMPT_COMMAND_RE.finditer(line)
            if (command := match.group(1).strip()) and _looks_like_verify_command(command)
        )
    return [command for command in commands if command]


def _task_prompt_body(prompt: str) -> str:
    """Return worker-authored task instructions, excluding prepended SOP blocks."""
    match = _PROMPT_ORIENT_RE.search(prompt)
    return prompt[match.start() :] if match else prompt


def _looks_like_verify_command(command: str) -> bool:
    """Classify command-like snippets conservatively.

    Verify sections often contain API names and filenames in backticks. The
    warning pass should only inspect snippets that look like shell commands,
    otherwise SOP prose such as `.get()` or `project.toml` becomes noise.
    """
    if "(" in command or ")" in command:
        return False
    tokens = _shell_tokens(command)
    if tokens is None:
        return False
    tokens = _drop_env_assignments(tokens)
    if not tokens:
        return False
    if _is_assignment_snippet(tokens):
        return False
    return _tokens_look_like_command(tokens)


def _shell_tokens(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _drop_env_assignments(tokens: list[str]) -> list[str]:
    idx = 0
    while idx < len(tokens) and _is_env_assignment(tokens[idx]):
        idx += 1
    return tokens[idx:]


def _is_assignment_snippet(tokens: list[str]) -> bool:
    return len(tokens) >= 2 and tokens[1] == "="


def _tokens_look_like_command(tokens: list[str]) -> bool:
    tool = tokens[0]
    if tool in _VERIFY_COMMAND_TOOLS or _looks_like_executable_path(tool):
        return True
    if len(tokens) == 1:
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.+-]*", tool):
        return False
    return any(_looks_like_command_argument(arg) for arg in tokens[1:])


def _looks_like_executable_path(token: str) -> bool:
    return token.startswith(("./", "../", "/"))


def _looks_like_command_argument(token: str) -> bool:
    return token.startswith(("-", "./", "../", "/")) or "/" in token or Path(token).suffix != ""


def _verification_tool(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    while tokens and _is_env_assignment(tokens[0]):
        tokens.pop(0)
    if not tokens:
        return ""
    tool = tokens[0]
    return "" if tool in _SHELL_BUILTINS else tool


def _is_env_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def _tool_available(tool: str, worker_path: str, project_root: Path) -> bool:
    if "/" in tool:
        path = Path(tool)
        return path.exists() if path.is_absolute() else (project_root / path).exists()
    return shutil.which(tool, path=worker_path) is not None


def _archive_ignore_warnings(project_root: Path) -> list[PlanIssue]:
    from dgov.plan import PlanIssue

    ignored_by = [
        path
        for path in (project_root / ".dgov" / ".gitignore", project_root / ".gitignore")
        if _ignores_plan_archive(path)
    ]
    if not ignored_by:
        return []
    files = ", ".join(str(path.relative_to(project_root)) for path in ignored_by)
    return [
        PlanIssue(
            severity="warning",
            message=(
                f"{files} ignores .dgov/plans/archive. Automatic archive moves can leave "
                "tracked plan deletions without tracked archived source."
            ),
        )
    ]


def _ignores_plan_archive(ignore_file: Path) -> bool:
    if not ignore_file.exists():
        return False
    ignored_patterns = {"plans/archive/", "/plans/archive/", ".dgov/plans/archive/"}
    return any(line.strip() in ignored_patterns for line in ignore_file.read_text().splitlines())


def _print_plan_errors(plan_errors: tuple[PlanIssue, ...]) -> None:
    for issue in plan_errors:
        unit_info = f" [{issue.unit}]" if issue.unit else ""
        click.echo(f"  ERROR{unit_info}: {issue.message}", err=True)


def _build_summary(
    out_path: Path,
    resolved: FlatPlan,
    result: BundleResult,
    dry_run: bool,
    warnings: tuple[PlanIssue, ...],
) -> _CompileSummary:
    return _CompileSummary(
        output_path=out_path,
        unit_count=len(resolved.units),
        edge_count=sum(len(u.depends_on) for u in resolved.units.values()),
        sop_count=sum(1 for mapping in result.sop_mapping.values() if mapping),
        sop_set_hash=result.sop_set_hash,
        dry_run=dry_run,
        warnings=warnings,
    )


def _print_summary(summary: _CompileSummary) -> None:
    if want_json():
        _print_json_summary(summary)
        return

    _print_text_summary(summary)


def _print_json_summary(summary: _CompileSummary) -> None:
    click.echo(
        json.dumps(
            {
                "status": "compiled",
                "output": str(summary.output_path),
                "units": summary.unit_count,
                "edges": summary.edge_count,
                "sops_assigned": summary.sop_count,
                "sop_set_hash": summary.sop_set_hash,
                "dry_run": summary.dry_run,
                "warnings": [{"unit": w.unit, "message": w.message} for w in summary.warnings],
            },
            indent=2,
        )
    )


def _print_text_summary(summary: _CompileSummary) -> None:
    click.echo(
        f"Compiled {summary.unit_count} units, {summary.edge_count} edges → {summary.output_path}"
    )
    if summary.sop_count:
        click.echo(f"  SOPs assigned to {summary.sop_count} unit(s)")
    if summary.dry_run:
        click.echo("  (dry-run: identity bundler, no LLM call)")
    for warning in summary.warnings:
        unit_info = f" [{warning.unit}]" if warning.unit else ""
        click.echo(f"  WARNING{unit_info}: {warning.message}", err=True)


def _print_graph_if_requested(graph: bool, resolved: FlatPlan) -> None:
    if graph and not want_json():
        print_dag_graph(resolved.units)


def _exit_with_error(message: str) -> NoReturn:
    click.echo(message, err=True)
    raise click.exceptions.Exit(code=1) from None
