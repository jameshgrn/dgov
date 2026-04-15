"""Settlement Layer: Validation Gates and Commit-or-Kill logic.

Pillar #8: Falsifiable Validation - All work is machine-verified before merge.
Pillar #10: Fail-Closed - Rejected work is never merged.

Three phases:
1. review_sandbox() — FAST git sanity checks BEFORE settlement (microseconds)
2. autofix_sandbox() — mechanical fixes (format, lint --fix) BEFORE commit
3. validate_sandbox() — read-only gate AFTER commit (milliseconds)
"""

from __future__ import annotations

import ast
import logging
import re
import shlex
import shutil
import subprocess
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from dgov.config import ProjectConfig, load_project_config
from dgov.persistence import read_events

logger = logging.getLogger(__name__)

_SENTRUX_BASELINE = ".sentrux/baseline.json"


@dataclass(frozen=True)
class GateResult:
    """The outcome of a validation gate."""

    passed: bool
    error: str | None = None


@dataclass(frozen=True)
class ReviewResult:
    """The outcome of a fast review gate."""

    passed: bool
    verdict: str
    actual_files: frozenset[str] = frozenset()
    error: str | None = None


class SmartFixer:
    """Harness-level refactorer for 'unfixable' lint rules.

    Pillar #8: Falsifiable Validation - Automates choice-making for stylistic/logical rules.
    """

    def __init__(self, worktree_path: Path, line_length: int = 99):
        self.worktree_path = worktree_path
        self.line_length = line_length

    def fix_all(self, files: list[str]) -> None:
        """Apply all smart fixes to the provided list of relative file paths."""
        for rel_path in files:
            path = self.worktree_path / rel_path
            if not path.exists() or path.suffix != ".py":
                continue

            content = path.read_text()
            # 1. Logical fixes (B904)
            content = self._fix_b904(content)
            # 2. Stylistic fixes (E501)
            content = self._fix_e501_comments(content)

            path.write_text(content)

    def _fix_b904(self, content: str) -> str:
        """Fix B904 (raise-without-from-inside-except).

        If an except block has 'except ... as exc:' and a bare 'raise NewError()',
        automatically convert to 'raise NewError() from exc'.
        """
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return content

        modified = False

        class B904Transformer(ast.NodeTransformer):
            def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.AST:
                # Need an exception variable name to chain from
                exc_name = node.name
                if not exc_name:
                    return self.generic_visit(node)

                # Look for bare 'raise' statements in this handler
                for body_node in ast.walk(node):
                    if isinstance(body_node, ast.Raise) and body_node.exc and not body_node.cause:
                        # Append 'from {exc_name}'
                        body_node.cause = ast.Name(id=exc_name, ctx=ast.Load())
                        nonlocal modified
                        modified = True
                return node

        new_tree = B904Transformer().visit(tree)
        return ast.unparse(new_tree) if modified else content

    def _fix_e501_comments(self, content: str) -> str:
        """Fix E501 (line-too-long) for comments.

        Wraps prose comments to fit within line_length while preserving
        URLs and indentation.
        """
        lines = content.splitlines()
        new_lines = []
        for line in lines:
            # Only process lines that exceed length and are comments
            if len(line) <= self.line_length or "#" not in line:
                new_lines.append(line)
                continue

            indent = line[: len(line) - len(line.lstrip())]
            parts = line.split("#", 1)
            code_part = parts[0]
            comment_part = parts[1].strip()

            # Skip if comment is likely a URL or pragma
            if any(x in comment_part for x in ("http://", "https://", "noqa:", "type:")):
                new_lines.append(line)
                continue

            # Wrap prose comment
            prefix = f"{indent}# " if not code_part.strip() else f"{code_part}# "
            wrap_width = self.line_length - len(prefix)

            if wrap_width < 20:  # Code part is too long, can't wrap comment reasonably
                new_lines.append(line)
                continue

            wrapped = textwrap.wrap(comment_part, width=wrap_width)
            if not wrapped:
                new_lines.append(line)
                continue

            # First line keeps code part
            new_lines.append(f"{prefix}{wrapped[0]}")
            # Subsequent lines are just indented comments
            for w in wrapped[1:]:
                new_lines.append(f"{indent}# {w}")

        return "\n".join(new_lines) + ("\n" if content.endswith("\n") else "")


def _run_cmd(
    cmd_template: str, files: list[str], cwd: Path, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    """Run a command template, substituting {file} with the file list."""
    file_args = " ".join(shlex.quote(f) for f in files)
    cmd = cmd_template.replace("{file}", file_args)
    return subprocess.run(
        cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
    )


def _get_all_changes(worktree_path: Path) -> frozenset[str] | ReviewResult:
    """Get ALL changed/new files via git status --porcelain.

    Unlike git diff, this catches untracked files too — critical for workers
    that create new files.
    """
    # --untracked-files=all ensures new files in new directories are listed
    # individually (e.g. "scratch/foo.py") rather than as a directory marker
    # ("scratch/"), which would cause false scope_violation failures.
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return ReviewResult(passed=False, verdict="git_error", error="git status failed")

    files = set()
    for line in status.stdout.rstrip("\n").split("\n"):
        if not line:
            continue
        path_part = line[3:]
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1]
        files.add(path_part)

    if not files:
        return ReviewResult(passed=False, verdict="empty_diff", error="No changes produced")
    return frozenset(files)


def _check_size(actual_files: frozenset[str], max_diff_lines: int) -> ReviewResult | None:
    """Check file count against size limit."""
    if len(actual_files) > max_diff_lines:
        return ReviewResult(
            passed=False,
            verdict="diff_too_large",
            error=f"Diff has {len(actual_files)} files, max is {max_diff_lines}",
        )
    return None


def _check_reserved_paths(actual_files: frozenset[str]) -> ReviewResult | None:
    """Reject worker changes to governor-owned files."""
    reserved = sorted(path for path in actual_files if path == _SENTRUX_BASELINE)
    if reserved:
        return ReviewResult(
            passed=False,
            verdict="reserved_path",
            actual_files=actual_files,
            error=f"Touched governor-owned files: {reserved}",
        )
    return None


def _is_scope_ignored(
    path: str, ignored_exact: frozenset[str], ignored_dirs: tuple[str, ...]
) -> bool:
    """Check if a path matches the ignore list (exact match or directory prefix)."""
    if path in ignored_exact:
        return True
    return any(path.startswith(d) for d in ignored_dirs)


def _split_ignore_entries(
    scope_ignore_files: Sequence[str],
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Split ignore entries into exact-file matches and directory prefixes.

    Entries ending with '/' are directory prefixes. Entries without a file
    extension (no '.' in the basename) are also treated as directory prefixes.
    """
    exact: set[str] = set()
    dirs: list[str] = []
    for entry in scope_ignore_files:
        if entry.endswith("/"):
            dirs.append(entry)
        elif "." not in entry.rsplit("/", 1)[-1]:
            dirs.append(entry.rstrip("/") + "/")
        else:
            exact.add(entry)
    return frozenset(exact), tuple(dirs)


def _check_scope(
    actual_files: frozenset[str],
    claimed_files: Sequence[str] | None,
    scope_ignore_files: Sequence[str] = (),
) -> ReviewResult | None:
    """Check that changed files are within claimed scope. Returns ReviewResult on failure.

    Files listed in `scope_ignore_files` (from `[scope] ignore_files` in
    project.toml) are treated as tooling side-effects and exempted from the
    unclaimed check — e.g. uv.lock updated by `uv run`, .venv/ touched by uv.

    Entries without a file extension are treated as directory prefixes.
    """
    if not claimed_files:
        return None

    claimed = frozenset(claimed_files)
    ignored_exact, ignored_dirs = _split_ignore_entries(scope_ignore_files)
    unclaimed = frozenset(
        f for f in actual_files - claimed if not _is_scope_ignored(f, ignored_exact, ignored_dirs)
    )
    if unclaimed:
        return ReviewResult(
            passed=False,
            verdict="scope_violation",
            actual_files=actual_files,
            error=f"Touched unclaimed files: {sorted(unclaimed)}",
        )
    return None


def _check_transient_scope(
    session_root: str | None,
    task_slug: str | None,
    pane_slug: str | None,
    claimed_files: Sequence[str] | None,
    actual_files: frozenset[str],
    scope_ignore_files: Sequence[str] = (),
) -> ReviewResult | None:
    """Reject transient unclaimed writes observed in worker tool activity."""
    if not session_root or not task_slug or not claimed_files:
        return None

    claimed = frozenset(claimed_files)
    ignored_exact, ignored_dirs = _split_ignore_entries(scope_ignore_files)
    transient_paths: set[str] = set()
    if pane_slug:
        events = read_events(session_root, slug=pane_slug, task_slug=task_slug)
    else:
        events = read_events(session_root, task_slug=task_slug)

    for event in events:
        if event.get("event") != "worker_log" or event.get("log_type") != "result":
            continue
        content = event.get("content")
        if not isinstance(content, dict):
            continue
        activity = content.get("activity")
        if not isinstance(activity, list):
            continue
        for item in activity:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            if isinstance(path, str):
                transient_paths.add(path)

    unclaimed = sorted(
        p
        for p in transient_paths
        if p not in claimed and not _is_scope_ignored(p, ignored_exact, ignored_dirs)
    )
    if not unclaimed:
        return None

    return ReviewResult(
        passed=False,
        verdict="scope_violation",
        actual_files=actual_files,
        error=(f"Transiently touched unclaimed files via worker tools: {unclaimed}"),
    )


def review_sandbox(
    worktree_path: Path,
    claimed_files: Sequence[str] | None = None,
    max_diff_lines: int = 100,
    project_root: str | None = None,
    task_slug: str | None = None,
    pane_slug: str | None = None,
    scope_ignore_files: Sequence[str] = (),
) -> ReviewResult:
    """FAST review gate — git sanity checks in microseconds.

    Checks:
    1. Empty diff (worker produced nothing)
    2. Diff size (runaway worker)
    3. Scope enforcement (touched unclaimed files)
    4. Review hooks (user-defined policy via .dgov/project.toml)

    `scope_ignore_files` (from project.toml `[scope] ignore_files`) is a list
    of paths exempted from scope checks — lockfiles and similar tooling-managed
    state that workers may incidentally touch via `uv run`, `npm install`, etc.

    Uses git status --porcelain to see ALL changes including new files.
    """
    try:
        # 1. Get all changed files (tracked + untracked)
        files_result = _get_all_changes(worktree_path)
        if isinstance(files_result, ReviewResult):
            return files_result
        actual_files = files_result

        # 2. Check size
        result = _check_size(actual_files, max_diff_lines)
        if result is not None:
            return result

        # 3. Reserved-path enforcement
        result = _check_reserved_paths(actual_files)
        if result is not None:
            return result

        # 4. Scope enforcement
        result = _check_scope(actual_files, claimed_files, scope_ignore_files)
        if result is not None:
            return result

        # 5. Transient scope enforcement from worker tool activity
        result = _check_transient_scope(
            project_root,
            task_slug,
            pane_slug,
            claimed_files,
            actual_files,
            scope_ignore_files,
        )
        if result is not None:
            return result

        # 6. Review hooks
        if project_root:
            config = load_project_config(project_root)
            if config.review_hooks:
                file_args = " ".join(shlex.quote(f) for f in actual_files)
                for hook in config.review_hooks:
                    cmd = hook.replace("{file}", file_args).replace("{files}", file_args)
                    res = subprocess.run(
                        cmd,
                        shell=True,
                        cwd=worktree_path,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if res.returncode != 0:
                        return ReviewResult(
                            passed=False,
                            verdict="hook_fail",
                            actual_files=actual_files,
                            error=f"Review hook failed: {hook}\n{res.stdout}{res.stderr}",
                        )

        # All checks passed
        return ReviewResult(
            passed=True,
            verdict="ok",
            actual_files=actual_files,
        )

    except Exception as exc:
        return ReviewResult(passed=False, verdict="exception", error=f"Review failed: {exc}")


def autofix_sandbox(
    worktree_path: Path,
    file_claims: tuple[str, ...] = (),
    config: ProjectConfig | None = None,
) -> None:
    """Mechanical auto-fix: lint fix then format. Called BEFORE commit.

    Order matters: lint fix can change formatting, so format runs LAST.
    Scoped to claimed files if provided, otherwise all source files.
    """
    if config is None:
        config = load_project_config(worktree_path)

    extensions = config.source_extensions

    if file_claims:
        rel = [
            f
            for f in file_claims
            if any(f.endswith(ext) for ext in extensions) and (worktree_path / f).exists()
        ]
    else:
        source_files: list[Path] = []
        for ext in extensions:
            source_files.extend(worktree_path.rglob(f"*{ext}"))
        if not source_files:
            return
        rel = [str(f.relative_to(worktree_path)) for f in source_files]

    if not rel:
        return

    # 1. Standard Lint fix (may remove imports, change lines)
    _run_cmd(config.lint_fix_cmd, rel, worktree_path, timeout=config.settlement_timeout)

    # 2. Smart Fixer (Logical and Prose fixes that Ruff skips)
    sf = SmartFixer(worktree_path, line_length=config.line_length)
    sf.fix_all(rel)

    # 3. Format LAST (canonical formatting after all mutations)
    _run_cmd(config.format_cmd, rel, worktree_path, timeout=config.settlement_timeout)


def _changed_source_files(
    worktree_path: Path, base_commit: str, extensions: tuple[str, ...]
) -> list[str]:
    """Return source files changed between base_commit and HEAD."""
    diff_res = subprocess.run(
        ["git", "diff", "--name-only", base_commit, "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return _filter_source_files(diff_res.stdout.strip().split("\n"), extensions)


def _filter_source_files(paths: Sequence[str], extensions: tuple[str, ...]) -> list[str]:
    """Filter an ordered path sequence down to unique source files."""
    seen: set[str] = set()
    source_files: list[str] = []
    for path in paths:
        if not path or not any(path.endswith(ext) for ext in extensions) or path in seen:
            continue
        seen.add(path)
        source_files.append(path)
    return source_files


def _working_tree_source_files(
    worktree_path: Path, extensions: tuple[str, ...]
) -> list[str] | GateResult:
    """Return source files changed in the working tree, including untracked files."""
    files_result = _get_all_changes(worktree_path)
    if isinstance(files_result, ReviewResult):
        if files_result.verdict == "empty_diff":
            return []
        return GateResult(passed=False, error=files_result.error or "git status failed")

    return _filter_source_files(sorted(files_result), extensions)


def _existing_files(worktree_path: Path, paths: Sequence[str]) -> list[str]:
    """Return only changed paths that still exist on disk."""
    return [f for f in paths if f and (worktree_path / f).exists()]


def _find_related_tests(source_files: list[str], test_dir: str, worktree_path: Path) -> list[str]:
    """Find test files that import from changed source modules."""
    # Build module names from changed source files
    modules: set[str] = set()
    for f in source_files:
        # src/dgov/cli/__init__.py -> dgov.cli, dgov/cli/__init__.py -> dgov.cli
        mod = f
        for prefix in ("src/", "lib/"):
            if mod.startswith(prefix):
                mod = mod[len(prefix) :]
        mod = mod.replace("/", ".").removesuffix(".py").removesuffix(".__init__")
        modules.add(mod)
        # Also match partial: dgov.cli matches "from dgov.cli import"
        parts = mod.split(".")
        for i in range(1, len(parts) + 1):
            modules.add(".".join(parts[:i]))

    if not modules:
        return []

    test_root = worktree_path / test_dir
    if not test_root.is_dir():
        return []

    related: list[str] = []
    for test_file in sorted(test_root.rglob("test_*.py")):
        try:
            content = test_file.read_text()
            for mod in modules:
                if f"from {mod}" in content or f"import {mod}" in content:
                    related.append(str(test_file.relative_to(worktree_path)))
                    break
        except (UnicodeDecodeError, OSError):
            continue
    return related


def _build_test_cmd(config: ProjectConfig, changed_files: list[str], worktree_path: Path) -> str:
    """Return test command scoped to related tests only.

    Changed test files run directly. Changed source files trigger only
    tests that import from the changed modules — never the full suite.
    """
    if "{test_dir}" not in config.test_cmd:
        return config.test_cmd
    test_dir = config.test_dir.rstrip("/")
    test_files = [f for f in changed_files if f.startswith(test_dir)]
    source_files = [f for f in changed_files if not f.startswith(test_dir)]

    targets: list[str] = list(test_files)
    if source_files:
        related = _find_related_tests(source_files, test_dir, worktree_path)
        for t in related:
            if t not in targets:
                targets.append(t)
        src_root = config.src_dir.rstrip("/")
        boundary_test = f"{test_dir}/test_boundaries.py"
        touches_src = any(f == src_root or f.startswith(f"{src_root}/") for f in source_files)
        if (
            touches_src
            and (worktree_path / boundary_test).is_file()
            and boundary_test not in targets
        ):
            targets.append(boundary_test)

    if not targets:
        return ""
    return config.test_cmd.replace("{test_dir}", " ".join(shlex.quote(f) for f in targets))


def _run_test_gate(test_cmd: str, worktree_path: Path, timeout: int = 120) -> GateResult | None:
    """Run tests. Return failure GateResult on non-zero exit, None on pass."""
    res = subprocess.run(
        test_cmd,
        shell=True,
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    # Exit code 5 = "no tests were collected" — not a failure (e.g. scaffold tasks)
    if res.returncode not in (0, 5):
        output = (res.stdout + res.stderr)[-500:]
        return GateResult(passed=False, error=f"Test failure:\n{output}")
    return None


_SENTRUX_WARN_ONLY = re.compile(
    r"(complex functions increased|coupling increased)",
    re.IGNORECASE,
)

_SENTRUX_HARD_FAIL = re.compile(
    r"(quality.*dropped|cycles increased|god files increased)",
    re.IGNORECASE,
)


def _sentrux_is_warn_only(output: str) -> bool:
    """Return True if the only degradation is complexity increase (not a hard failure).

    Complexity going up while overall quality improves is expected when adding
    new code. Hard-failing on it blocks legitimate work. We log a warning instead.
    Hard failures: quality drop, coupling increase, cycle increase, god-file increase.
    """
    lines = output.splitlines()
    failing = [ln for ln in lines if ln.strip().startswith("✗") and "DEGRADED" not in ln]
    if not failing:
        return False
    return all(_SENTRUX_WARN_ONLY.search(ln) for ln in failing) and not any(
        _SENTRUX_HARD_FAIL.search(ln) for ln in lines
    )


def _run_sentrux_gate(worktree_path: Path, project_root: str, timeout: int) -> GateResult:
    """Run sentrux policy gate — reject on hard degradation, warn on complexity only."""
    import json

    baseline = Path(project_root) / ".sentrux" / "baseline.json"
    if not baseline.exists():
        return GateResult(passed=True)

    if shutil.which("sentrux") is None:
        return GateResult(
            passed=False,
            error="Sentrux not found in PATH. Fix: install sentrux before running dgov.",
        )

    # Skip gate when baseline was captured from an empty project (no import edges).
    # Comparing against an empty baseline always shows "degradation" for any real code.
    try:
        bdata = json.loads(baseline.read_text())
        if bdata.get("total_import_edges") == 0:
            return GateResult(passed=True)
    except Exception:
        pass

    sx_dst = worktree_path / ".sentrux"
    baseline_dir = baseline.parent.resolve()
    if baseline_dir != sx_dst.resolve():
        if sx_dst.exists():
            shutil.rmtree(sx_dst)
        shutil.copytree(baseline.parent, sx_dst)

    try:
        res_sx = subprocess.run(
            ["sentrux", "gate", "."],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return GateResult(
            passed=False,
            error=f"Sentrux gate timed out after {timeout}s.",
        )

    sx_output = (res_sx.stdout or "") + (res_sx.stderr or "")

    if res_sx.returncode != 0:
        if _sentrux_is_warn_only(sx_output):
            logger.warning(
                "Sentrux: complexity increased (warn-only, not blocking):\n%s", sx_output
            )
            return GateResult(passed=True)
        return GateResult(passed=False, error=f"Sentrux architectural degradation:\n{sx_output}")

    return GateResult(passed=True)


def _run_setup_cmd(setup_cmd: str, worktree_path: Path, timeout: int = 300) -> GateResult | None:
    """Run the project setup command in the worktree. Returns failure or None on success."""
    if not setup_cmd:
        return None
    try:
        res = subprocess.run(
            setup_cmd,
            shell=True,
            cwd=worktree_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return GateResult(passed=False, error=f"setup_cmd timed out after {timeout}s")
    if res.returncode != 0:
        output = (res.stdout + res.stderr)[-500:]
        return GateResult(passed=False, error=f"setup_cmd failed:\n{output}")
    return None


def _run_acceptance_gates(
    worktree_path: Path,
    changed_files: Sequence[str],
    project_root: str,
    config: ProjectConfig,
) -> GateResult:
    """Run the shared acceptance gates for a resolved changed-file set."""
    if not changed_files:
        return GateResult(passed=True)

    # 0. Run setup command (e.g. npm ci for JS/TS worktrees)
    setup_failure = _run_setup_cmd(config.setup_cmd, worktree_path)
    if setup_failure is not None:
        return setup_failure

    existing_changed_files = _existing_files(worktree_path, changed_files)

    try:
        if existing_changed_files:
            res_lint = _run_cmd(
                config.lint_cmd,
                existing_changed_files,
                worktree_path,
                timeout=config.settlement_timeout,
            )
            if res_lint.returncode != 0:
                output = (res_lint.stdout + res_lint.stderr)[-500:]
                return GateResult(passed=False, error=f"Lint failure:\n{output}")

            res_fmt = _run_cmd(
                config.format_check_cmd,
                existing_changed_files,
                worktree_path,
                timeout=config.settlement_timeout,
            )
            if res_fmt.returncode != 0:
                output = (res_fmt.stdout + res_fmt.stderr)[-500:]
                return GateResult(passed=False, error=f"Format failure:\n{output}")

        if config.type_check_cmd:
            res_ty = subprocess.run(
                config.type_check_cmd,
                shell=True,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=config.settlement_timeout,
            )
            if res_ty.returncode != 0:
                output = (res_ty.stdout + res_ty.stderr)[-500:]
                return GateResult(passed=False, error=f"Type check failure:\n{output}")

        test_cmd = _build_test_cmd(config, list(changed_files), worktree_path)
        if test_cmd:
            test_failure = _run_test_gate(
                test_cmd, worktree_path, timeout=config.settlement_timeout
            )
            if test_failure is not None:
                return test_failure

        sx_result = _run_sentrux_gate(worktree_path, project_root, config.settlement_timeout)
        if not sx_result.passed:
            return sx_result

        return GateResult(passed=True)

    except Exception as exc:
        return GateResult(passed=False, error=f"Unexpected validation error: {exc}")


def preflight_sandbox(
    worktree_path: Path,
    project_root: str,
    config: ProjectConfig | None = None,
) -> GateResult:
    """Run the settlement acceptance gates against local working-tree changes."""
    if config is None:
        config = load_project_config(project_root)

    changed_files = _working_tree_source_files(worktree_path, config.source_extensions)
    if isinstance(changed_files, GateResult):
        return changed_files
    return _run_acceptance_gates(worktree_path, changed_files, project_root, config)


def validate_sandbox(
    worktree_path: Path,
    base_commit: str,
    project_root: str,
    config: ProjectConfig | None = None,
) -> GateResult:
    """Read-only validation gate. Called AFTER commit. No mutations."""
    if config is None:
        config = load_project_config(project_root)

    changed_files = _changed_source_files(worktree_path, base_commit, config.source_extensions)
    return _run_acceptance_gates(worktree_path, changed_files, project_root, config)
