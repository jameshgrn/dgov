"""Administrative and diagnostic commands."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click

from dgov.agents import detect_installed_agents
from dgov.cli import SESSION_ROOT_OPTION
from dgov.cli.pane import _autocorrect_roots


def _scan_py_files(src_dirs: list[Path], project_root: Path) -> dict[str, dict[str, Any]]:
    """Scan Python files and extract metadata.

    Returns a dict mapping module paths to their metadata:
    - line_count: int
    - docstring: str or None
    - size_category: 'S'/'M'/'L'
    """
    modules = {}
    for src_dir in src_dirs:
        if not src_dir.exists():
            continue
        for py_file in src_dir.rglob("*.py"):
            # Skip __init__.py and test files (they'll be mapped later)
            if py_file.name == "__init__.py":
                continue
            rel_path = py_file.relative_to(project_root)
            module_key = str(rel_path)

            try:
                text = py_file.read_text(encoding="utf-8")
                lines = text.splitlines()
                line_count = len(lines)

                # Extract docstring using AST
                tree = ast.parse(text, filename=str(py_file))
                docstring = (ast.get_docstring(tree) or "").split("\n")[0]

                # Size category
                if line_count < 200:
                    size_category = "S"
                elif line_count <= 500:
                    size_category = "M"
                else:
                    size_category = "L"

                modules[module_key] = {
                    "line_count": line_count,
                    "docstring": docstring,
                    "size_category": size_category,
                }
            except (SyntaxError, UnicodeDecodeError) as exc:
                # Skip files that can't be parsed
                modules[module_key] = {
                    "line_count": 0,
                    "docstring": f"# Error parsing: {exc}",
                    "size_category": "S",
                }
    return modules


def _map_test_files(modules: dict[str, dict], tests_dir: Path) -> dict[str, list[str]]:
    """Map source modules to their test files.

    Returns a dict mapping module paths to lists of test file names.
    """
    mappings = {}
    if not tests_dir.exists():
        return mappings

    # Find all test files
    test_files = {}
    for tf in tests_dir.rglob("test_*.py"):
        if tf.name == "__init__.py":
            continue
        test_rel = tf.relative_to(tests_dir)
        # Extract module name from test file (e.g., test_merger.py -> merger)
        stem = tf.stem  # e.g., "test_merger"
        if stem.startswith("test_"):
            mod_name = stem[5:]  # Remove "test_" prefix
            test_files[mod_name] = str(test_rel)

    # Map each module to matching tests
    for mod_key in modules:
        # Extract module name from path (e.g., "merger.py" -> "merger")
        mod_name = Path(mod_key).stem
        mods_to_match = [mod_name]

        # Also try without .py suffix
        if mod_name.endswith(".py"):
            mods_to_match.append(mod_name[:-3])

        matching_tests = []
        for candidate in mods_to_match:
            if candidate in test_files:
                matching_tests.append(test_files[candidate])

        mappings[mod_key] = sorted(matching_tests)

    return mappings


def _group_modules(modules: dict[str, dict]) -> dict[str, list[str]]:
    """Categorize modules into groups."""
    groups: dict[str, list[str]] = {
        "orchestration core": [],
        "merge and review": [],
        "automation and recovery": [],
        "agent integration": [],
        "decision system": [],
        "higher-level workflows": [],
        "visualization": [],
        "cli": [],
        "other": [],
    }

    # Define group mappings (module name -> group)
    # These match the current CODEBASE.md groupings
    group_map: dict[str, str] = {
        "lifecycle.py": "orchestration core",
        "persistence.py": "orchestration core",
        "done.py": "orchestration core",
        "gitops.py": "orchestration core",
        "waiter.py": "orchestration core",
        "status.py": "orchestration core",
        "merger.py": "merge and review",
        "inspection.py": "merge and review",
        "monitor.py": "automation and recovery",
        "recovery.py": "automation and recovery",
        "responder.py": "automation and recovery",
        "monitor_hooks.py": "automation and recovery",
        "agents.py": "agent integration",
        "router.py": "agent integration",
        "strategy.py": "agent integration",
        "templates.py": "agent integration",
        "openrouter.py": "agent integration",
        "mission.py": "higher-level workflows",
        "batch.py": "higher-level workflows",
        "dag.py": "higher-level workflows",
        "dag_parser.py": "higher-level workflows",
        "dag_graph.py": "higher-level workflows",
        "review_fix.py": "higher-level workflows",
        "experiment.py": "higher-level workflows",
        "dashboard.py": "visualization",
        "terrain.py": "visualization",
        "terrain_pane.py": "visualization",
    }

    for module_key in sorted(modules.keys()):
        mod_name = Path(module_key).name

        if module_key.startswith("cli/"):
            # All cli/* modules go to CLI group
            groups["cli"].append(module_key)
        elif mod_name in group_map:
            groups[group_map[mod_name]].append(module_key)
        else:
            groups["other"].append(module_key)

    return groups


def _generate_codebase_md(
    modules: dict[str, dict],
    test_mappings: dict[str, list[str]],
    groups: dict[str, list[str]],
    project_root: Path,
) -> str:
    """Generate the CODEBASE.md content."""
    lines = []

    # Header
    lines.append("# dgov Codebase Map")
    lines.append("")
    lines.append("## Task routing — start here")
    lines.append("")
    lines.append("| If your task is about... | Start in | Also check | Tests |")
    lines.append("|--------------------------|----------|------------|-------|")

    # Generate task routing table from modules
    routing_table = [
        (
            "Pane create/close/resume",
            "lifecycle.py",
            ["persistence.py", "done.py", "gitops.py"],
            "test_lifecycle.py",
        ),
        (
            "Merge/review behavior",
            "merger.py",
            ["inspection.py", "persistence.py"],
            "test_merger*.py",
        ),
        (
            "Review diffs, verdicts, freshness",
            "inspection.py",
            ["merger.py"],
            "test_inspection*.py",
        ),
        (
            "Retry/escalation/recovery",
            "recovery.py",
            ["responder.py", "monitor.py"],
            "test_retry*.py",
        ),
        (
            "Monitor daemon logic",
            "monitor.py",
            ["monitor_hooks.py", "recovery.py"],
            "test_monitor.py",
        ),
        (
            "Worker completion/done",
            "done.py, waiter.py",
            ["lifecycle.py"],
            "test_done_strategy.py",
        ),
        (
            "Agent routing/selection",
            "router.py, agents.py",
            ["strategy.py"],
            "test_router.py",
        ),
        (
            "Decision providers",
            "decision.py, decision_providers.py",
            ["provider_registry.py"],
            "test_decision.py",
        ),
        (
            "Prompt templates",
            "templates.py, strategy.py",
            ["lifecycle.py"],
            "test_templates.py",
        ),
        (
            "Dashboard/terrain TUI",
            "dashboard.py, terrain.py",
            ["terrain_pane.py"],
            "test_dashboard.py",
        ),
        (
            "DAG/batch/mission",
            "dag.py, batch.py, mission.py",
            ["dag_parser.py", "dag_graph.py"],
            "test_dag.py, test_batch.py, test_mission.py",
        ),
        (
            "State DB/events",
            "persistence.py",
            ["status.py", "metrics.py"],
            "test_persistence*.py",
        ),
        (
            "Top-level CLI command",
            "cli/admin.py, cli/pane.py",
            ["cli/__init__.py"],
            "test_cli_admin.py, test_dgov_cli.py",
        ),
    ]

    for task_desc, start_mod, check_mods, test_pattern in routing_table:
        tests = []
        if test_pattern:
            # Find matching test files
            if test_pattern in test_mappings and test_mappings[test_pattern]:
                tests.extend(test_mappings[test_pattern])
            else:
                # Fallback: just show the pattern
                tests.append(test_pattern)
        elif start_mod in test_mappings:
            tests = test_mappings[start_mod]

        tests_str = ", ".join(sorted(set(tests))) if tests else "N/A"
        check_str = ", ".join(check_mods)
        lines.append(f"| {task_desc} | `{start_mod}` | {check_str} | {tests_str} |")

    lines.append("")

    # Invariants section (hardcoded from current CODEBASE.md)
    lines.append("## Invariants — do not break these")
    lines.append("")
    lines.append(
        "- You are in a **git worktree**, not the main repo. Do not merge, rebase, or pull."
    )
    lines.append(
        "- `CLAUDE.md` and `AGENTS.md` are "
        "**git-excluded** — exist on disk for read, cannot commit."
    )
    lines.append(
        "- `dgov worker complete` will **auto-commit** any unstaged changes before signaling done."
    )
    lines.append(
        "- Protected files (CLAUDE.md, THEORY.md, .napkin.md) "
        "**restored during merge** — changes discarded."
    )
    lines.append("- Do NOT push to remote. Do NOT run the full test suite.")
    lines.append("")

    # Module groups section
    lines.append("## Module groups")
    lines.append("")

    group_order = [
        "orchestration core",
        "merge and review",
        "automation and recovery",
        "agent integration",
        "cli",
        "higher-level workflows",
        "visualization",
        "other",
    ]

    for group_name in group_order:
        if group_name not in groups or not groups[group_name]:
            continue

        lines.append(f"### {group_name.capitalize().replace('_', ' ')}")
        lines.append("| File | Size | Purpose |")
        lines.append("|------|------|---------|")

        for module_key in sorted(groups[group_name]):
            mod_info = modules.get(module_key, {})
            docstring = mod_info.get("docstring", "")
            size_cat = mod_info.get("size_category", "S")

            # Truncate docstring if too long
            if len(docstring) > 100:
                display_docstring = docstring[:97] + "..."
            else:
                display_docstring = docstring

            # Escape markdown pipes in docstring
            display_docstring = display_docstring.replace("|", "\\|")

            lines.append(f"| `{module_key}` | {size_cat} | {display_docstring or 'N/A'} |")

        lines.append("")

    # CLI command registration section (hardcoded from current CODEBASE.md)
    lines.append("## CLI command registration")
    lines.append("")
    lines.append("**Pane subcommands** (no registration needed):")
    lines.append('1. Add `@pane.command("name")` function to `cli/pane.py`')
    lines.append("")
    lines.append("**Top-level commands**:")
    lines.append(
        "1. Add function to appropriate `cli/*.py` file (or create a new `cli/foo_cmd.py`)"
    )
    lines.append("2. Import in `cli/__init__.py` (alphabetical)")
    lines.append("3. Add `cli.add_command(your_cmd)` after the import block")
    lines.append("")

    # Data flow section (hardcoded from current CODEBASE.md)
    lines.append("## Data flow")
    lines.append("")
    lines.append("```")
    lines.append("create_worker_pane()")
    lines.append("  → load_registry() + resolve_agent()   # find and route agent")
    lines.append("  → get_backend().create_worker_pane()   # tmux split-pane")
    lines.append("  → add_pane()                           # write to state.db")
    lines.append("  → _write_worktree_instructions()       # inject worker context")
    lines.append("  → _wrap_done_signal()                  # setup done detection")
    lines.append("")
    lines.append("merge_worker_pane()")
    lines.append("  → get_pane()                           # read pane record")
    lines.append("  → _restore_protected_files()           # fix CLAUDE.md on branch")
    lines.append("  → _commit_worktree()                   # auto-commit uncommitted")
    lines.append("  → _rebase_onto_head()                  # rebase branch")
    lines.append("  → _plumbing_merge()                    # in-memory git merge")
    lines.append("  → _full_cleanup()                      # kill pane + remove worktree")
    lines.append("  → _lint_fix_merged_files()             # ruff check + format")
    lines.append("  → _run_related_tests()                 # pytest on changed files")
    lines.append("```")
    lines.append("")

    # State machine section (hardcoded from current CODEBASE.md)
    lines.append("## State machine")
    lines.append("")
    lines.append("```")
    lines.append("active → done → merged → (removed)")
    lines.append("active → timed_out → (retry) → active")
    lines.append("active → failed → (retry) → active")
    lines.append("active → abandoned → closed")
    lines.append("any terminal state → closed")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def regenerate_codebase_md(project_root: str) -> None:
    """Scan source tree and write CODEBASE.md. Called by merger post-merge."""
    root_path = Path(project_root).resolve()
    src_dirs = [root_path / "src" / "dgov", root_path / "src" / "dgov" / "cli"]
    modules = _scan_py_files(src_dirs, root_path)
    test_mappings = _map_test_files(modules, root_path / "tests")
    groups = _group_modules(modules)
    content = _generate_codebase_md(modules, test_mappings, groups, root_path)
    (root_path / "CODEBASE.md").write_text(content, encoding="utf-8")


@click.command("codebase")
@click.option("--project-root", "-r", default=".", help="Project root")
@click.option("--dry-run", is_flag=True, help="Print to stdout instead of writing file")
@click.option(
    "--commit",
    is_flag=True,
    default=False,
    help="Stage and commit CODEBASE.md after regeneration",
)
def codebase_cmd(project_root: str, dry_run: bool, commit: bool) -> None:
    """Scan source tree and generate CODEBASE.md."""
    from dgov.cli.pane import _autocorrect_roots

    project_root, _ = _autocorrect_roots(project_root, None)

    if dry_run:
        root_path = Path(project_root).resolve()
        src_dirs = [root_path / "src" / "dgov", root_path / "src" / "dgov" / "cli"]
        modules = _scan_py_files(src_dirs, root_path)
        test_mappings = _map_test_files(modules, root_path / "tests")
        groups = _group_modules(modules)
        content = _generate_codebase_md(modules, test_mappings, groups, root_path)
        click.echo(content)
    else:
        regenerate_codebase_md(project_root)
        if commit:
            root_path = Path(project_root).resolve()
            env = os.environ.copy()
            env["DGOV_SKIP_GOVERNOR_CHECK"] = "1"
            subprocess.run(
                ["git", "add", "CODEBASE.md"],
                cwd=root_path,
                check=True,
                env=env,
            )
            subprocess.run(
                ["git", "commit", "-m", "Regenerate CODEBASE.md"],
                cwd=root_path,
                check=True,
                env=env,
            )
        click.echo(f"Written to {Path(project_root).resolve() / 'CODEBASE.md'}")


def _count_lines(file_path: Path) -> int:
    """Count lines in a file (utility function for scanning)."""
    return len(file_path.read_text(encoding="utf-8").splitlines())


def _scaffold_dgov_dirs(root: Path) -> None:
    """Create .dgov/ directory structure (idempotent)."""
    dirs = [
        root / ".dgov" / "hooks",
        root / ".dgov" / "templates",
        root / ".dgov" / "batch",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


@click.command("preflight")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--agent", "-a", default=None, help="Agent to validate for")
@click.option("--fix", is_flag=True, help="Auto-fix fixable failures")
@click.option(
    "--touches",
    "-t",
    multiple=True,
    help="Files the task will touch (repeatable)",
)
@click.option("--branch", "-b", default=None, help="Expected branch name")
def preflight_cmd(project_root, session_root, agent, fix, touches, branch):
    """Run pre-flight checks before dispatch."""
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.agents import get_default_agent, load_registry

    if agent is None:
        agent = get_default_agent(load_registry(project_root))
    from dgov.preflight import fix_preflight, run_preflight

    report = run_preflight(
        project_root=project_root,
        agent=agent,
        touches=list(touches) if touches else None,
        expected_branch=branch,
        session_root=session_root,
    )
    if not report.passed and fix:
        report = fix_preflight(report, project_root)

    click.echo(json.dumps(report.to_dict(), indent=2))
    if not report.passed:
        sys.exit(1)


@click.command("status")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
def status(project_root, session_root):
    """Get full dgov status as JSON."""
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.status import list_worker_panes

    panes = list_worker_panes(project_root, session_root=session_root)
    click.echo(
        json.dumps(
            {
                "panes": panes,
                "total": len(panes),
                "alive": sum(1 for p in panes if p["alive"]),
                "done": sum(1 for p in panes if p.get("state") == "done"),
                "merged": sum(1 for p in panes if p.get("state") == "merged"),
                "failed": sum(1 for p in panes if p.get("state") == "failed"),
            },
            indent=2,
        )
    )


@click.command("rebase")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@click.option(
    "--onto",
    default=None,
    help="Explicit base branch to rebase onto (default: auto-detect upstream or main)",
)
def rebase(project_root, onto):
    """Rebase the governor worktree onto its base branch.

    Stashes dirty changes, rebases onto upstream (or main), and pops stash.
    On conflict: aborts rebase and restores working tree.
    """
    project_root, _ = _autocorrect_roots(project_root)

    from dgov.inspection import rebase_governor

    result = rebase_governor(project_root, onto=onto)
    click.echo(json.dumps(result, indent=2))
    if not result.get("rebased"):
        sys.exit(1)


@click.command("blame")
@click.argument("file_path")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@click.option("--session-root", "-R", default=None, help="Session root")
@click.option("--all", "-a", "show_all", is_flag=True, default=False, help="Show full history")
@click.option("--agent", default=None, help="Filter by agent")
@click.option("--line-level", is_flag=True, default=False, help="Show line-level blame")
@click.option("--lines", "-L", default=None, help="Line range for line-level blame (e.g. 10-20)")
def blame(file_path, project_root, session_root, show_all, agent, line_level, lines):
    """Show which agent/pane last touched a file."""
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    if lines or line_level:
        from dgov.blame import blame_lines

        start_line = None
        end_line = None
        if lines:
            parts = lines.split("-", 1)
            try:
                start_line = int(parts[0])
                if len(parts) > 1:
                    end_line = int(parts[1])
                else:
                    end_line = start_line
            except ValueError:
                click.echo(f"Invalid line range: {lines} (expected N or N-M)", err=True)
                sys.exit(1)

        result = blame_lines(
            project_root=project_root,
            file_path=file_path,
            session_root=session_root,
            start_line=start_line,
            end_line=end_line,
            agent_filter=agent,
        )
    else:
        from dgov.blame import blame_file

        result = blame_file(
            project_root=project_root,
            file_path=file_path,
            session_root=session_root,
            last_only=not show_all,
            agent_filter=agent,
        )
    click.echo(json.dumps(result, indent=2))


@click.command("agents")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root for registry loading ($DGOV_PROJECT_ROOT or cwd)",
)
def list_agents(project_root):
    """List available agents and which are installed."""
    project_root, _ = _autocorrect_roots(project_root)

    from dgov.agents import load_registry

    registry = load_registry(project_root)
    installed = set(detect_installed_agents(registry))
    agents = []
    for agent_id, defn in registry.items():
        entry = {
            "id": agent_id,
            "name": defn.name,
            "installed": agent_id in installed,
            "transport": defn.prompt_transport,
            "source": defn.source,
        }
        if defn.health_check:
            hc = subprocess.run(defn.health_check, shell=True, capture_output=True, text=True)
            entry["healthy"] = hc.returncode == 0
        agents.append(entry)
    click.echo(json.dumps(agents, indent=2))


@click.command("version")
def version_cmd():
    """Show dgov version."""
    from dgov import __version__

    result = {"dgov": __version__}
    click.echo(json.dumps(result, indent=2))


@click.command("stats")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
def stats(project_root, session_root):
    """Show pane and agent statistics."""
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.inspection import compute_stats

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    data = compute_stats(session_root)
    click.echo(json.dumps(data, indent=2))


@click.command("dashboard")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--refresh", default=1, type=float, help="Refresh interval in seconds")
@click.option("--pane", is_flag=True, help="Launch dashboard in a tmux split pane")
def dashboard(project_root, session_root, refresh, pane):
    """Launch live terminal dashboard."""
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    if pane:
        import subprocess

        cmd = f"dgov dashboard -r {os.path.abspath(project_root)} --refresh {refresh}"
        if session_root:
            cmd += f" --session-root {os.path.abspath(session_root)}"
        subprocess.run(
            ["tmux", "split-window", "-d", "-l", "30%", cmd],
            check=True,
        )
        click.echo(json.dumps({"dashboard": "launched in pane"}))
        return
    from dgov.dashboard import run_dashboard

    run_dashboard(project_root, session_root, refresh)


@click.command("terrain")
@click.option("--refresh", default=0.5, help="Seconds between steps")
def terrain_cmd(refresh):
    """Run standalone terrain erosion simulation."""
    from dgov.terrain_pane import run_terrain

    run_terrain(refresh)


@click.command("tunnel")
def tunnel_cmd():
    """Establish or refresh the River SSH tunnel."""
    click.echo("Refreshing River multiplexed tunnel...")
    try:
        # We use zsh -c so we can pick up the function from .zshrc
        result = subprocess.run(
            ["zsh", "-c", "source ~/.zshrc && river-tunnel"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            click.echo(json.dumps({"river_tunnel": "up", "detail": result.stdout.strip()}))
        else:
            click.echo(json.dumps({"river_tunnel": "failed", "error": result.stderr.strip()}))
            sys.exit(1)
    except Exception as e:
        click.echo(json.dumps({"river_tunnel": "error", "message": str(e)}))
        sys.exit(1)


@click.command("init")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd, where .dgov/ will be created)",
)
def init_cmd(project_root):
    """Initialize a new dgov project: scaffold .dgov/ and write config."""
    project_root, _ = _autocorrect_roots(project_root)

    root = Path(project_root).resolve()
    config_path = root / ".dgov" / "config.toml"

    if config_path.is_file():
        click.echo("Already initialized.")
        return

    # Interactive prompt
    governor = click.prompt("Governor agent", default="claude", type=str)
    permissions = "bypassPermissions"

    # Create directories
    _scaffold_dgov_dirs(root)

    # Write config
    config_path.write_text(
        f'[dgov]\ngovernor_agent = "{governor}"\ngovernor_permissions = "{permissions}"\n',
        encoding="utf-8",
    )

    # Add .dgov/ to .gitignore if not already there
    from dgov.lifecycle import ensure_dgov_gitignored

    ensure_dgov_gitignored(str(root))

    click.echo("Initialized dgov project:")
    click.echo(f"  {config_path}")
    for name in ("hooks", "templates", "batch"):
        click.echo(f"  {root / '.dgov' / name}/")


@click.command("doctor")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root to diagnose ($DGOV_PROJECT_ROOT or cwd)",
)
def doctor_cmd(project_root):
    """Run diagnostics on the dgov environment."""
    project_root, _ = _autocorrect_roots(project_root)

    import platform
    import shutil

    from dgov.agents import detect_installed_agents, load_registry
    from dgov.persistence import all_panes, state_path

    root = Path(project_root).resolve()
    ok = True

    def _check(label, passed, detail=""):
        nonlocal ok
        icon = "[ok]" if passed else "[FAIL]"
        if not passed:
            ok = False
        msg = f"  {icon} {label}"
        if detail:
            msg += f" -- {detail}"
        click.echo(msg)

    click.echo("dgov doctor\n")

    # 1. tmux installed
    tmux_path = shutil.which("tmux")
    _check("tmux installed", tmux_path is not None)

    # tmux server running
    if tmux_path:
        tmux_running = (
            subprocess.run(
                ["tmux", "list-sessions"],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
        _check("tmux server running", tmux_running)
    else:
        _check("tmux server running", False, "tmux not installed")

    # 2. git installed
    git_path = shutil.which("git")
    _check("git installed", git_path is not None)

    # 3. Python >= 3.12
    py_ver = platform.python_version_tuple()
    py_ok = (int(py_ver[0]), int(py_ver[1])) >= (3, 12)
    _check(
        "Python >= 3.12",
        py_ok,
        f"found {platform.python_version()}",
    )

    # 4. state.db readable
    db = state_path(str(root))
    if db.is_file():
        try:
            all_panes(str(root))
            _check("state.db readable", True)
        except Exception as exc:
            _check("state.db readable", False, str(exc))
    else:
        _check("state.db exists", True, "no state.db yet (first run)")

    # 5. Installed agents
    registry = load_registry(str(root))
    installed = detect_installed_agents(registry)
    _check(
        "agents installed",
        len(installed) > 0,
        ", ".join(installed) if installed else "none found",
    )

    # 6. Hooks directory
    hooks_dir = root / ".dgov" / "hooks"
    if hooks_dir.is_dir():
        scripts = list(hooks_dir.iterdir())
        non_exec = [s.name for s in scripts if s.is_file() and not os.access(s, os.X_OK)]
        if non_exec:
            _check("hooks executable", False, f"not executable: {', '.join(non_exec)}")
        else:
            _check("hooks directory", True, f"{len(scripts)} script(s)")
    else:
        _check("hooks directory", True, "no .dgov/hooks/ (optional)")

    # 7. Orphaned worktrees
    try:
        wt_result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        git_worktrees = []
        first = True
        for line in wt_result.stdout.splitlines():
            if line.startswith("worktree "):
                if first:
                    first = False
                    continue
                git_worktrees.append(line.split(" ", 1)[1])

        if db.is_file():
            panes = all_panes(str(root))
            tracked = {p.get("worktree_path") for p in panes}
            orphaned = [wt for wt in git_worktrees if wt not in tracked]
            _check(
                "no orphaned worktrees",
                len(orphaned) == 0,
                f"{len(orphaned)} orphaned" if orphaned else f"{len(git_worktrees)} tracked",
            )
        else:
            _check("no orphaned worktrees", True, "no state.db to compare")
    except (subprocess.TimeoutExpired, OSError) as exc:
        _check("worktree check", False, str(exc))

    # 8. Stale panes (pane in state.db whose tmux pane is dead)
    if db.is_file():
        from dgov.backend import get_backend

        backend = get_backend()
        panes = all_panes(str(root))
        active_panes = [p for p in panes if p.get("state") == "active"]
        stale = [
            p["slug"]
            for p in active_panes
            if p.get("pane_id") and not backend.is_alive(p["pane_id"])
        ]
        _check(
            "no stale panes",
            len(stale) == 0,
            f"stale: {', '.join(stale)}" if stale else f"{len(active_panes)} active",
        )

    click.echo()
    if ok:
        click.echo("All checks passed.")
    else:
        click.echo("Some checks failed.")
    sys.exit(0 if ok else 1)


@click.command("transcript")
@click.argument("slug")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--json", "output_json", is_flag=True, help="Output raw JSONL")
def transcript_cmd(slug, project_root, session_root, output_json):
    """View a worker session transcript."""
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    # Default session_root to project_root if not provided
    session_root = os.path.abspath(session_root) if session_root else os.path.abspath(project_root)

    # Build path to transcript file: .dgov/logs/<slug>.transcript.jsonl
    logs_dir = Path(session_root) / ".dgov" / "logs"
    transcript_path = logs_dir / f"{slug}.transcript.jsonl"

    if not transcript_path.exists():
        click.echo(f"No transcript found for slug '{slug}'", err=True)
        sys.exit(1)

    lines = transcript_path.read_text(encoding="utf-8").splitlines()

    if output_json:
        # Raw JSONL output - each line preserved
        for line in lines:
            click.echo(line)
        return

    # Parse and display summary
    def _format_summary(entry: dict) -> str | None:
        """Extract summary from a transcript entry."""
        entry_type = entry.get("type")
        if entry_type != "message":
            return None

        message = entry.get("message", {})
        role = message.get("role")
        if role != "assistant":
            return None

        content = message.get("content", [])
        summary_parts = []

        for item in content:
            item_type = item.get("type")
            if item_type == "tool_use":
                name = item.get("name", "")
                input_data = item.get("input", {})
                summary_parts.append(f"🔧 {name}: {json.dumps(input_data)[:50]}")
            elif item_type == "text":
                text = item.get("text", "")
                # Truncate long responses
                if len(text) > 100:
                    text = text[:97] + "..."
                summary_parts.append(f"📝 {text}")

        return " ".join(summary_parts) if summary_parts else None

    def _format_timestamp(ts: str | None) -> str:
        """Format ISO timestamp to readable form."""
        if not ts:
            return ""
        try:
            dt = __import__("datetime").datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%H:%M:%S")
        except (ValueError, TypeError):
            return ""

    click.echo(f"=== Transcript: {slug} ===\n")
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        summary = _format_summary(entry)
        if summary is not None:
            ts = _format_timestamp(entry.get("timestamp"))
            timestamp_str = f"[{ts}]" if ts else ""
            click.echo(f"{timestamp_str} {summary}")

    click.echo()


@click.command("gc")
@click.option("--root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
@click.option("--dry-run", is_flag=True, help="Show what would be cleaned")
def gc_cmd(root, session_root, dry_run):
    """Garbage-collect stale tmux sessions, worktrees, and branches."""
    import shutil

    from dgov.backend import get_backend
    from dgov.persistence import STATE_DIR, all_panes, remove_pane

    root = Path(root).resolve()
    session_root = Path(session_root).resolve() if session_root else root
    backend = get_backend()
    removed = []

    # 1. Kill dead tmux panes in state DB
    panes = all_panes(str(session_root))
    for p in panes:
        pane_id = p.get("pane_id", "")
        slug = p["slug"]
        alive = backend.is_alive(pane_id) if pane_id else False
        state = p.get("state", "")
        if not alive and state in ("done", "failed", "abandoned", "closed", "merged"):
            if dry_run:
                click.echo(f"[dry-run] remove pane entry: {slug} ({state})")
            else:
                remove_pane(str(session_root), slug)
                done_path = session_root / STATE_DIR / "done" / slug
                done_path.unlink(missing_ok=True)
            removed.append(f"pane:{slug}")

    # 2. Remove orphaned worktree directories
    wt_dir = root / ".dgov" / "worktrees"
    if wt_dir.is_dir():
        known_wts = {p.get("worktree_path") for p in panes}
        for entry in wt_dir.iterdir():
            if not entry.is_dir():
                continue
            if str(entry) in known_wts:
                continue
            if dry_run:
                click.echo(f"[dry-run] remove orphan worktree: {entry.name}")
            else:
                shutil.rmtree(entry, ignore_errors=True)
            removed.append(f"worktree:{entry.name}")

    # 3. Remove orphaned git worktrees
    wt_list = subprocess.run(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if wt_list.returncode == 0:
        first = True
        for line in wt_list.stdout.splitlines():
            if line.startswith("worktree "):
                if first:
                    first = False
                    continue
                wt_path = line.split(" ", 1)[1]
                if not Path(wt_path).exists():
                    if dry_run:
                        click.echo(f"[dry-run] prune git worktree: {wt_path}")
                    else:
                        subprocess.run(
                            ["git", "-C", str(root), "worktree", "remove", "--force", wt_path],
                            capture_output=True,
                        )
                    removed.append(f"git-worktree:{Path(wt_path).name}")

    # 4. Delete stale dgov- branches (merged into main)
    br_result = subprocess.run(
        ["git", "-C", str(root), "branch", "--merged", "main"],
        capture_output=True,
        text=True,
    )
    if br_result.returncode == 0:
        for line in br_result.stdout.splitlines():
            branch = line.strip().lstrip("* ")
            if branch.startswith("dgov-"):
                if dry_run:
                    click.echo(f"[dry-run] delete merged branch: {branch}")
                else:
                    subprocess.run(
                        ["git", "-C", str(root), "branch", "-d", branch],
                        capture_output=True,
                    )
                removed.append(f"branch:{branch}")

    # 5. Kill dead dgov-* tmux sessions (no attached clients, no panes)
    sess_result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}:#{session_attached}"],
        capture_output=True,
        text=True,
    )
    if sess_result.returncode == 0:
        for line in sess_result.stdout.splitlines():
            parts = line.rsplit(":", 1)
            if len(parts) != 2:
                continue
            sess_name, attached = parts[0], parts[1]
            if sess_name.startswith("dgov-") and attached == "0":
                if dry_run:
                    click.echo(f"[dry-run] kill detached session: {sess_name}")
                else:
                    subprocess.run(
                        ["tmux", "kill-session", "-t", sess_name],
                        capture_output=True,
                    )
                removed.append(f"session:{sess_name}")

    click.echo(json.dumps({"gc": removed, "count": len(removed), "dry_run": dry_run}))
