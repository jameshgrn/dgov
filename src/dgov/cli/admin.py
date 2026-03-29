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
from dgov.cli import SESSION_ROOT_OPTION, want_json
from dgov.cli.pane import _autocorrect_roots
from dgov.persistence import PaneState


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
    mappings: dict[str, list[str]] = {}
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
        "executor.py": "orchestration core",
        "kernel.py": "orchestration core",
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
        "decision.py": "decision system",
        "decision_providers.py": "decision system",
        "provider_registry.py": "decision system",
        "context_packet.py": "decision system",
        "mission.py": "higher-level workflows",
        "batch.py": "higher-level workflows",
        "dag.py": "higher-level workflows",
        "dag_parser.py": "higher-level workflows",
        "dag_graph.py": "higher-level workflows",
        "review_fix.py": "higher-level workflows",
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
    """Generate CODEBASE.md in dense LLM-native format.

    Optimized for token efficiency and pattern matching, not human readability.
    Uses structured blocks instead of markdown tables.
    """
    lines: list[str] = []

    lines.append("# CODEBASE")
    lines.append("")

    # Task routing: compact key→value
    lines.append("## ROUTING")
    routing = [
        ("pane lifecycle", "lifecycle.py", "persistence.py done.py gitops.py"),
        ("merge/review", "merger.py", "inspection.py persistence.py"),
        ("review diffs", "inspection.py", "merger.py"),
        ("retry/escalation", "recovery.py", "responder.py monitor.py"),
        ("monitor daemon", "monitor.py", "monitor_hooks.py recovery.py"),
        ("done detection", "done.py waiter.py", "lifecycle.py"),
        ("agent routing", "router.py agents.py", "strategy.py"),
        ("decisions", "decision.py decision_providers.py", "provider_registry.py"),
        ("templates", "templates.py strategy.py", "lifecycle.py"),
        ("dashboard TUI", "dashboard.py terrain.py", "terrain_pane.py"),
        ("DAG/batch/plan", "dag.py batch.py plan.py", "dag_parser.py dag_graph.py kernel.py"),
        ("state DB", "persistence.py", "status.py"),
        ("CLI", "cli/admin.py cli/pane.py", "cli/__init__.py"),
    ]
    for task, start, deps in routing:
        lines.append(f"{task}: {start} + {deps}")
    lines.append("")

    # Invariants: flat rules, no bold/emphasis (wastes tokens)
    lines.append("## INVARIANTS")
    lines.append("- git worktree, not main repo. no merge/rebase/pull.")
    lines.append("- CLAUDE.md git-excluded. read-only, cannot commit.")
    lines.append("- dgov worker complete auto-commits unstaged changes.")
    lines.append("- protected files restored at merge. changes discarded.")
    lines.append("- no push to remote. no full test suite.")
    lines.append("")

    # Modules: one line per file, dense
    lines.append("## MODULES")
    group_order = [
        "orchestration core",
        "merge and review",
        "automation and recovery",
        "agent integration",
        "decision system",
        "higher-level workflows",
        "cli",
        "other",
    ]
    for group_name in group_order:
        if group_name not in groups or not groups[group_name]:
            continue
        lines.append(f"[{group_name}]")
        for module_key in sorted(groups[group_name]):
            mod_info = modules.get(module_key, {})
            doc = mod_info.get("docstring", "").replace("|", "/")
            sz = mod_info.get("size_category", "S")
            tests = test_mappings.get(module_key, [])
            test_str = " ".join(t.replace("tests/", "") for t in tests[:3])
            if len(tests) > 3:
                test_str += f" +{len(tests) - 3}"
            entry = f"  {module_key} ({sz}): {doc}"
            if test_str:
                entry += f" -> {test_str}"
            lines.append(entry)
    lines.append("")

    # Call graphs: indented notation
    lines.append("## CALL GRAPH")
    lines.append("create_worker_pane:")
    lines.append("  load_registry + resolve_agent")
    lines.append("  get_backend.create_worker_pane (tmux)")
    lines.append("  add_pane (state.db)")
    lines.append("  _write_worktree_instructions (context)")
    lines.append("  _wrap_done_signal (exit detection)")
    lines.append("merge_worker_pane:")
    lines.append("  _restore_protected_files")
    lines.append("  _commit_worktree (auto-commit)")
    lines.append("  _rebase_onto_head")
    lines.append("  _plumbing_merge (in-memory)")
    lines.append("  _full_cleanup (kill + remove)")
    lines.append("  _lint_fix_merged_files (ruff)")
    lines.append("  _run_related_tests (pytest)")
    lines.append("")

    # State machine: flat transitions
    lines.append("## STATES")
    lines.append("active -> done -> merged -> removed")
    lines.append("active -> timed_out -> retry -> active")
    lines.append("active -> failed -> retry -> active")
    lines.append("active -> abandoned -> closed")
    lines.append("any terminal -> closed")
    lines.append("")

    # CLI registration: minimal
    lines.append("## CLI REGISTRATION")
    lines.append('pane sub: @pane.command("name") in cli/pane.py')
    lines.append("top-level: fn in cli/*.py, import+add_command in cli/__init__.py")
    lines.append("")

    # Test manifest: compact source->tests
    manifest_path = project_root / ".test-manifest.json"
    if manifest_path.exists():
        import json as _json

        try:
            manifest = _json.loads(manifest_path.read_text())
            lines.append("## TESTS")
            for src, tests in sorted(manifest.items()):
                if src.startswith("_"):
                    continue
                test_str = " ".join(t.replace("tests/", "") for t in tests[:3])
                if len(tests) > 3:
                    test_str += f" +{len(tests) - 3}"
                lines.append(f"{src} -> {test_str}")
        except Exception:
            pass

    return "\n".join(lines) + "\n"


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
    """Scan source tree and generate CODEBASE.md.

    Examples:
      dgov codebase -r .
      dgov codebase -r . --dry-run
    """
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
    """Run pre-flight checks before dispatch.

    Examples:
      dgov preflight -r .
      dgov preflight -r . -a qwen-35b --fix
    """
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
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output raw JSON instead of human-readable summary",
)
def status(project_root, session_root, output_json):
    """Get dgov status.

    Examples:
      dgov status -r .
      dgov status -r . --json
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.agents import detect_installed_agents, load_registry
    from dgov.persistence import PaneState, read_events
    from dgov.spans import ledger_query
    from dgov.status import list_worker_panes

    sr = session_root or project_root
    session_root_abs = os.path.abspath(sr)
    panes = list_worker_panes(project_root, session_root=session_root)
    live_panes, preserved_panes = _split_live_and_preserved_panes(panes)
    registry = load_registry(project_root)
    installed = set(detect_installed_agents(registry))

    if output_json or want_json():
        # Machine-readable JSON output
        click.echo(
            json.dumps(
                {
                    "panes": live_panes,
                    "preserved": preserved_panes,
                    "total": len(live_panes),
                    "preserved_total": len(preserved_panes),
                    "alive": sum(1 for p in live_panes if p["alive"]),
                    "done": sum(1 for p in live_panes if p.get("state") == PaneState.DONE),
                    "merged": sum(1 for p in live_panes if p.get("state") == PaneState.MERGED),
                    "failed": sum(1 for p in live_panes if p.get("state") == PaneState.FAILED),
                },
                indent=2,
            )
        )
    else:
        # Human-readable summary
        total = len(live_panes)
        by_state: dict[str, int] = {}
        for p in live_panes:
            state = p.get("state") or "active"
            by_state[state] = by_state.get(state, 0) + 1

        # Count healthy agents (parallel health checks — serial was 3-4s)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _check_health(agent_id: str) -> str | None:
            agent_def = registry.get(agent_id)
            if not agent_def or not agent_def.health_check:
                return None
            try:
                result = subprocess.run(
                    agent_def.health_check, shell=True, capture_output=True, text=True, timeout=5
                )
                return agent_id if result.returncode != 0 else None
            except (subprocess.TimeoutExpired, OSError):
                return agent_id

        unhealthy: list[str] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_check_health, aid): aid for aid in installed}
            for fut in as_completed(futures):
                bad = fut.result()
                if bad:
                    unhealthy.append(bad)

        # Format human-readable output
        lines = []

        # Pane summary with breakdown
        active_count = by_state.get(PaneState.ACTIVE, 0)
        done_count = by_state.get(PaneState.DONE, 0)
        merged_count = by_state.get(PaneState.MERGED, 0)
        failed_count = by_state.get(PaneState.FAILED, 0)

        parts = [f"{total} panes"]
        if active_count > 0:
            parts.append(f"{active_count} active")
        if done_count > 0:
            parts.append(f"{done_count} done")
        if merged_count > 0:
            parts.append(f"{merged_count} merged")

        panes_str = ", ".join(parts)
        lines.append(f"dgov status: {panes_str}")
        if preserved_panes:
            preserved_count = len(preserved_panes)
            noun = "pane" if preserved_count == 1 else "panes"
            lines.append(f"preserved evidence: {preserved_count} {noun}")

        # Failed count on separate line if any
        if failed_count > 0:
            lines[-1] += f", {failed_count} failed"

        # Agent health summary
        healthy_count = len(installed) - len(unhealthy)
        if unhealthy:
            unhealthy_str = f"{len(unhealthy)} unhealthy"
            lines.append(
                f"agents: {len(installed)} installed, {healthy_count} healthy, {unhealthy_str}"
            )
        else:
            lines.append(f"agents: {len(installed)} installed, all healthy")

        # Recent failures from events table (kind LIKE fail)
        try:
            events = read_events(session_root_abs, limit=100)
            recent_failures = sum(1 for e in events if "fail" in str(e.get("event", "")).lower())
            if recent_failures > 0:
                lines.append(f"recent failures: {recent_failures}")
        except Exception:
            pass

        # Open bugs from ledger table
        try:
            open_bugs = ledger_query(session_root_abs, category="bug", status="open", limit=50)
            bug_count = len(open_bugs)
            if bug_count > 0:
                lines.append(f"open bugs: {bug_count}")
        except Exception:
            pass

        click.echo("\n".join(lines))


_TERMINAL_EVIDENCE_STATES = {
    PaneState.DONE,
    PaneState.MERGED,
    PaneState.FAILED,
    PaneState.SUPERSEDED,
    PaneState.CLOSED,
    PaneState.ESCALATED,
    PaneState.TIMED_OUT,
    PaneState.ABANDONED,
}


def _split_live_and_preserved_panes(panes: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate live panes from preserved terminal evidence rows."""
    live: list[dict] = []
    preserved: list[dict] = []
    for pane in panes:
        state = str(pane.get("state") or "active")
        is_preserved = any(
            [
                pane.get("preserved_artifacts"),
                pane.get("preserved_reason"),
                pane.get("preserved_recoverable"),
            ]
        )
        if is_preserved and state in _TERMINAL_EVIDENCE_STATES:
            preserved.append(pane)
        else:
            live.append(pane)
    return live, preserved


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

    Examples:
      dgov rebase -r .
      dgov rebase -r . --onto main
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
    """Show which agent/pane last touched a file.

    Examples:
      dgov blame src/dgov/merger.py -r .
      dgov blame src/dgov/cli.py --line-level -L 10-20
    """
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
    """List available agents and which are installed.

    Examples:
      dgov agents -r .
    """
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
    """Show dgov version.

    Examples:
      dgov version
    """
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
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output raw JSON instead of human-readable table",
)
def stats(project_root, session_root, output_json):
    """Show pane and agent statistics.

    Examples:
      dgov stats -r .
      dgov stats -r . --json
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.inspection import compute_stats

    project_root = os.path.abspath(project_root)
    session_root = os.path.abspath(session_root) if session_root else project_root
    data = compute_stats(session_root)

    if output_json or want_json():
        click.echo(json.dumps(data, indent=2))
    else:
        reliability = data.get("reliability", {})
        if not reliability:
            click.echo("No agent statistics available.")
            return

        header = f"{'Agent':<15} {'Pass':>6} {'Dispatches':>10} {'Reviews':>7} {'Avg Review':>10}"
        click.echo(header)
        click.echo("-" * len(header))

        for agent_name, info in sorted(reliability.items()):
            pr = info.get("pass_rate", 0.0)
            disp = info.get("dispatch_count", 0)
            revs = info.get("review_count", 0)
            avg_ms = info.get("avg_review_ms", 0.0)
            pct = f"{int(pr * 100)}%" if pr > 0 else "0%"
            avg_str = f"{int(avg_ms)}ms" if avg_ms > 0 else "-"
            click.echo(f"{agent_name:<15} {pct:>6} {disp:>10} {revs:>7} {avg_str:>10}")


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
    """Launch live terminal dashboard.

    Examples:
      dgov dashboard -r .
      dgov dashboard -r . --pane
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    # Ensure the headless engine is running
    from dgov.monitor import ensure_monitor_running

    ensure_monitor_running(project_root, session_root=session_root)

    if pane:
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
    """Run standalone terrain erosion simulation.

    Examples:
      dgov terrain
    """
    from dgov.terrain_pane import run_terrain

    run_terrain(refresh)


@click.command("tunnel")
def tunnel_cmd():
    """Establish or refresh the River SSH tunnel.

    Examples:
      dgov tunnel
    """
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
@click.option(
    "--agent", "-a", default=None, help="Governor agent (skip interactive prompt if provided)"
)
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON")
def init_cmd(project_root, agent, output_json):
    """Initialize a new dgov project: scaffold .dgov/ and write config.

    Examples:
      dgov init -r .
      dgov init -r . --agent claude
    """
    project_root, _ = _autocorrect_roots(project_root)

    root = Path(project_root).resolve()
    config_path = root / ".dgov" / "config.toml"

    if config_path.is_file():
        if output_json:
            result = {
                "initialized": True,
                "config": str(config_path),
                "governor": agent,
            }
            click.echo(json.dumps(result))
        else:
            click.echo("Already initialized.")
        return

    governor = agent or "claude"
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

    if output_json:
        result = {
            "initialized": True,
            "config": str(config_path),
            "governor": governor,
        }
        click.echo(json.dumps(result))
    else:
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
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON")
def doctor_cmd(project_root, output_json):
    """Run diagnostics on the dgov environment.

    Examples:
      dgov doctor -r .
      dgov doctor -r . --json
    """
    project_root, _ = _autocorrect_roots(project_root)

    import platform
    import shutil

    from dgov.agents import detect_installed_agents, load_registry
    from dgov.persistence import all_panes, state_path

    root = Path(project_root).resolve()
    ok = True
    checks: list[dict] = []

    def _check(label, passed, detail=""):
        nonlocal ok
        if not passed:
            ok = False
        checks.append({"check": label, "passed": passed, "detail": detail})
        if not (output_json or want_json()):
            icon = "[ok]" if passed else "[FAIL]"
            msg = f"  {icon} {label}"
            if detail:
                msg += f" -- {detail}"
            click.echo(msg)

    if not (output_json or want_json()):
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

    # 5b. Agent protocol compliance
    from dgov.agents import check_all_agents

    violations = check_all_agents(registry)
    if violations:
        for agent_id, issues in violations.items():
            _check(f"protocol: {agent_id}", False, "; ".join(issues))
    else:
        _check("agent protocol", True, "all agents compliant")

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
        active_panes = [p for p in panes if p.get("state") == PaneState.ACTIVE]
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

    # 9. Auth validation: env vars conflicting with config auth mode
    from dgov.config import load_config

    config = load_config(str(root))
    providers = config.get("providers", {})
    for provider_name, provider_cfg in providers.items():
        if not isinstance(provider_cfg, dict):
            continue
        auth_mode = provider_cfg.get("auth", "")
        transport = provider_cfg.get("transport", "")
        if auth_mode == "oauth" and transport == "claude-cli":
            has_api_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
            if has_api_key:
                checks.append(
                    {
                        "check": f"auth: {provider_name}",
                        "passed": True,
                        "detail": "ANTHROPIC_API_KEY env var overrides OAuth — remove from .zshrc",
                        "warning": True,
                    }
                )
                if not (output_json or want_json()):
                    click.echo(
                        "  [WARN] auth: "
                        f"{provider_name} — ANTHROPIC_API_KEY env var overrides OAuth"
                        " — remove from .zshrc"
                    )
            else:
                _check(
                    f"auth: {provider_name}",
                    True,
                    "OAuth configured, no conflicting env var",
                )

    if output_json or want_json():
        click.echo(json.dumps({"checks": checks, "all_passed": ok}, indent=2))
        return

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
    """View a worker session transcript.

    Examples:
      dgov transcript fix-parser-1 -r .
      dgov transcript fix-parser-1 --json
    """
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


@click.command("recover")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root ($DGOV_PROJECT_ROOT or cwd)",
)
@SESSION_ROOT_OPTION
@click.option("--json", "output_json", is_flag=True, default=False, help="Output as JSON")
def recover_cmd(project_root, session_root, output_json):
    """Recover pane states from event log after crash.

    Examples:
      dgov recover -r .
      dgov recover -r . --json
    """
    project_root, session_root = _autocorrect_roots(project_root, session_root)

    from dgov.recovery import recover_from_events

    sr = os.path.abspath(session_root) if session_root else os.path.abspath(project_root)
    recs = recover_from_events(sr)
    if not recs:
        click.echo("No recovery needed — all panes consistent with event log.")
        return

    if output_json or want_json():
        click.echo(json.dumps({"recoveries": recs, "count": len(recs)}, indent=2, default=str))
        return

    click.echo(f"Found {len(recs)} pane(s) needing recovery:\n")
    for slug, info in recs.items():
        click.echo(f"  {slug}:")
        click.echo(f"    action:     {info['action']}")
        click.echo(f"    reason:     {info['reason']}")
        click.echo(f"    last event: {info['last_event']}")
        click.echo(f"    db state:   {info['db_state']}")
        click.echo()

    click.echo("Run dgov pane land <slug> or dgov pane close <slug> to resolve.")


@click.group("config")
def config_cmd():
    """View and manage dgov configuration."""
    pass


@config_cmd.command("show")
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
def config_show(project_root):
    """Print effective configuration (merged defaults + user + project).

    Examples:
      dgov config show -r .
    """
    from dgov.config import load_config

    config = load_config(project_root=os.path.abspath(project_root))
    _print_toml(config)


@config_cmd.command("set")
@click.argument("key")
@click.argument("value")
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@click.option("--project", is_flag=True, help="Write to project config instead of user config")
def config_set(key, value, project_root, project):
    """Set a config value. KEY is a dotted path like providers.review.model.

    Examples:
      dgov config set providers.review.model "qwen/qwen3.5-122b"
    """
    from dgov.config import write_config

    scope = "project" if project else "user"
    path = write_config(key, value, scope=scope, project_root=os.path.abspath(project_root))
    click.echo(f"{key} = {value!r} -> {path}")


@config_cmd.command("get")
@click.argument("key")
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
def config_get(key, project_root):
    """Get a config value. KEY is a dotted path like providers.review.model.

    Examples:
      dgov config get defaults.agent
    """
    from dgov.config import load_config

    config = load_config(project_root=os.path.abspath(project_root))

    # Traverse the dictionary using the dotted key
    value = config
    for part in key.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            click.echo("not set")
            sys.exit(1)

    # Handle boolean values for consistent output with `set`
    if isinstance(value, bool):
        click.echo("true" if value else "false")
    else:
        click.echo(str(value))


def _print_toml(d: dict, prefix: str = ""):
    """Print a nested dict as TOML to stdout."""
    # Print simple keys first
    for k, v in d.items():
        if not isinstance(v, dict):
            if isinstance(v, str):
                click.echo(f'{prefix}{k} = "{v}"')
            elif isinstance(v, bool):
                click.echo(f"{prefix}{k} = {'true' if v else 'false'}")
            elif isinstance(v, int):
                click.echo(f"{prefix}{k} = {v}")
    # Then nested tables
    for k, v in d.items():
        if isinstance(v, dict):
            section = f"{prefix}{k}" if not prefix else f"{prefix}{k}"
            click.echo(f"\n[{section}]")
            _print_toml(v, prefix=f"{section}.")


@click.command("gc")
@click.option("--project-root", "-r", default=".", help="Project root")
@SESSION_ROOT_OPTION
@click.option("--dry-run", is_flag=True, help="Show what would be cleaned")
def gc_cmd(project_root, session_root, dry_run):
    """Garbage-collect stale tmux sessions, worktrees, and branches.

    Examples:
      dgov gc -r .
      dgov gc -r . --dry-run
    """
    import shutil

    from dgov.backend import get_backend
    from dgov.persistence import STATE_DIR, all_panes, emit_event, remove_pane

    project_root = Path(project_root).resolve()
    session_root = Path(session_root).resolve() if session_root else project_root
    backend = get_backend()
    removed = []

    # 0. Close orphaned spans (pending > 2 hours)
    from dgov.spans import close_orphaned_spans

    orphaned = close_orphaned_spans(str(session_root))
    if orphaned:
        removed.append(f"spans:{orphaned} orphaned")

    # 0b. Prune stale DB entries and retained panes
    from dgov.status import gc_retained_panes, prune_stale_panes

    pruned = prune_stale_panes(str(project_root), session_root=str(session_root))
    gc_result = gc_retained_panes(
        str(project_root), session_root=str(session_root), older_than_s=3600.0
    )
    if pruned:
        removed.extend(f"pruned:{s}" for s in pruned)
    for slug in gc_result.get("closed", []):
        removed.append(f"gc:{slug}")

    # 1. Kill dead tmux panes in state DB
    panes = all_panes(str(session_root))
    for p in panes:
        pane_id = p.get("pane_id", "")
        slug = p["slug"]
        alive = backend.is_alive(pane_id) if pane_id else False
        state = p.get("state", "")
        if not alive and state in (
            PaneState.DONE.value,
            PaneState.FAILED.value,
            PaneState.ABANDONED.value,
            PaneState.CLOSED.value,
            PaneState.MERGED.value,
        ):
            if dry_run:
                click.echo(f"[dry-run] remove pane entry: {slug} ({state})")
            else:
                remove_pane(str(session_root), slug)
                emit_event(str(session_root), "pane_pruned", slug, reason="gc")
                done_path = session_root / STATE_DIR / "done" / slug
                done_path.unlink(missing_ok=True)
            removed.append(f"pane:{slug}")

    # 2. Remove orphaned worktree directories
    wt_dir = project_root / ".dgov" / "worktrees"
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
        ["git", "-C", str(project_root), "worktree", "list", "--porcelain"],
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
                            [
                                "git",
                                "-C",
                                str(project_root),
                                "worktree",
                                "remove",
                                "--force",
                                wt_path,
                            ],
                            capture_output=True,
                        )
                    removed.append(f"git-worktree:{Path(wt_path).name}")

    # 4. Delete stale dgov- branches (merged into main)
    br_result = subprocess.run(
        ["git", "-C", str(project_root), "branch", "--merged", "main"],
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
                        ["git", "-C", str(project_root), "branch", "-d", branch],
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
