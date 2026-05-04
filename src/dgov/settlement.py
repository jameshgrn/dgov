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
import contextlib
import logging
import re
import shlex
import shutil
import subprocess
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import cast

from dgov.config import ProjectConfig, load_project_config
from dgov.persistence import read_events

logger = logging.getLogger(__name__)

_SENTRUX_BASELINE = ".sentrux/baseline.json"
_COVERAGE_BASELINE_DIR = ".coverage-baseline"
_COVERAGE_BASELINE = f"{_COVERAGE_BASELINE_DIR}/coverage.json"
_RESERVED_PATHS = (_SENTRUX_BASELINE, _COVERAGE_BASELINE_DIR + "/")


@dataclass(frozen=True)
class GateResult:
    """The outcome of a validation gate."""

    passed: bool
    error: str | None = None

    def __post_init__(self) -> None:
        if self.passed and self.error is not None:
            raise ValueError("GateResult: passed=True but error is set")
        if not self.passed and not self.error:
            raise ValueError("GateResult: passed=False but no error message")


@dataclass(frozen=True)
class ReviewResult:
    """The outcome of a fast review gate."""

    passed: bool
    verdict: str
    actual_files: frozenset[str] = frozenset()
    error: str | None = None

    def __post_init__(self) -> None:
        if self.passed and self.error is not None:
            raise ValueError("ReviewResult: passed=True but error is set")
        if not self.passed and not self.error:
            raise ValueError("ReviewResult: passed=False but no error message")


def _walk_shallow(node: ast.AST) -> list[ast.AST]:
    """Walk AST children without descending into nested scopes.

    Skips ``FunctionDef``, ``AsyncFunctionDef``, ``ClassDef``, and
    ``ExceptHandler`` nodes so that raises inside nested handlers or
    inner functions are not associated with the outer exception name.
    """
    _SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)
    result: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        result.append(child)
        if not isinstance(child, _SCOPE_NODES):
            stack.extend(ast.iter_child_nodes(child))
    return result


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

        Uses AST to locate targets but edits text directly — never round-trips
        through ast.unparse(), which would rewrite the entire file.
        """
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return content

        # Collect (line_number, end_col, exc_name) for each bare raise in an except-as block.
        # Process bottom-up (reversed) so earlier insertions don't shift later line offsets.
        # Only scan direct body statements — skip nested functions, classes, and inner
        # try/except blocks to avoid associating raises with the wrong exception name.
        fixes: list[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or not node.name:
                continue
            exc_name = node.name
            for child in _walk_shallow(node):
                if (
                    isinstance(child, ast.Raise)
                    and child.exc
                    and not child.cause
                    and child.end_col_offset is not None
                    and child.end_lineno is not None
                ):
                    fixes.append((child.end_lineno, child.end_col_offset, exc_name))

        if not fixes:
            return content

        lines = content.splitlines(keepends=True)
        for lineno, end_col, exc_name in sorted(fixes, reverse=True):
            idx = lineno - 1  # 1-indexed → 0-indexed
            line = lines[idx]
            # Insert ' from exc_name' at end_col (before any trailing comment/newline)
            lines[idx] = line[:end_col] + f" from {exc_name}" + line[end_col:]

        return "".join(lines)

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
    reserved = sorted(
        path
        for path in actual_files
        if any(path == reserved or path.startswith(reserved) for reserved in _RESERVED_PATHS)
    )
    if reserved:
        return ReviewResult(
            passed=False,
            verdict="reserved_path",
            actual_files=actual_files,
            error=f"Touched governor-owned files: {reserved}",
        )
    return None


def _is_scope_ignored(
    path: str,
    ignored_exact: frozenset[str],
    ignored_prefix_dirs: tuple[str, ...],
    ignored_named_dirs: frozenset[str],
    ignored_globs: tuple[str, ...],
) -> bool:
    """Check if a path matches the ignore list."""
    if path in ignored_exact:
        return True
    parts = PurePosixPath(path).parts
    return (
        any(path.startswith(prefix) for prefix in ignored_prefix_dirs)
        or any(name in parts for name in ignored_named_dirs)
        or any(
            fnmatch(path, pattern) or fnmatch(PurePosixPath(path).name, pattern)
            for pattern in ignored_globs
        )
    )


def _split_ignore_entries(
    scope_ignore_files: Sequence[str],
) -> tuple[frozenset[str], tuple[str, ...], frozenset[str], tuple[str, ...]]:
    """Split ignore entries into exact paths, directory rules, and globs.

    Entries ending with '/' become directory rules. Bare directory names such
    as `.venv` or `__pycache__` match any path segment with that name.
    Entries containing glob syntax are matched with fnmatch.
    """
    exact: set[str] = set()
    prefix_dirs: list[str] = []
    named_dirs: set[str] = set()
    globs: list[str] = []
    for entry in scope_ignore_files:
        if any(ch in entry for ch in "*?["):
            globs.append(entry)
        elif entry.endswith("/"):
            stripped = entry.rstrip("/")
            if "/" in stripped:
                prefix_dirs.append(stripped + "/")
            else:
                named_dirs.add(stripped)
        elif "." not in entry.rsplit("/", 1)[-1]:
            if "/" in entry:
                prefix_dirs.append(entry.rstrip("/") + "/")
            else:
                named_dirs.add(entry.rstrip("/"))
        else:
            exact.add(entry)
    return frozenset(exact), tuple(prefix_dirs), frozenset(named_dirs), tuple(globs)


def _check_scope(
    actual_files: frozenset[str],
    claimed_files: Sequence[str] | None,
    scope_ignore_files: Sequence[str] = (),
    read_files: Sequence[str] = (),
) -> ReviewResult | None:
    """Check that changed files are within claimed scope. Returns ReviewResult on failure.

    Files listed in `scope_ignore_files` (from `[scope] ignore_files` in
    project.toml) are treated as tooling side-effects and exempted from the
    unclaimed check — e.g. uv.lock updated by `uv run`, `.venv` directories,
    nested `__pycache__` dirs, and `*.pyc` bytecode files.

    Files in `read_files` that were edited produce a softer
    ``read_scope_violation`` verdict — the runner can retry the worker
    instead of cascading failure.
    """
    if not claimed_files:
        return None

    claimed = frozenset(claimed_files)
    ignored_exact, ignored_prefix_dirs, ignored_named_dirs, ignored_globs = _split_ignore_entries(
        scope_ignore_files
    )
    unclaimed = frozenset(
        f
        for f in actual_files - claimed
        if not _is_scope_ignored(
            f, ignored_exact, ignored_prefix_dirs, ignored_named_dirs, ignored_globs
        )
    )
    if not unclaimed:
        return None

    # Distinguish: all unclaimed files are in read claims → retriable soft violation.
    # Any truly unclaimed file (not in read either) → hard scope violation.
    read_set = frozenset(read_files)
    truly_unclaimed = unclaimed - read_set
    if truly_unclaimed:
        return ReviewResult(
            passed=False,
            verdict="scope_violation",
            actual_files=actual_files,
            error=f"Touched unclaimed files: {sorted(truly_unclaimed)}",
        )
    return ReviewResult(
        passed=False,
        verdict="read_scope_violation",
        actual_files=actual_files,
        error=(
            f"Edited read-only files: {sorted(unclaimed)}. "
            "Revert changes to these files and call done — "
            "files.read grants read access only, not write."
        ),
    )


def _check_transient_scope(
    session_root: str | None,
    task_slug: str | None,
    pane_slug: str | None,
    claimed_files: Sequence[str] | None,
    actual_files: frozenset[str],
    scope_ignore_files: Sequence[str] = (),
) -> ReviewResult | None:
    """Reject transient unclaimed writes observed in worker tool activity.

    Checks ALL panes for this task across the current run (not just the current
    pane). This ensures unclaimed writes from earlier retries are still caught
    even if a later retry cleans the worktree and succeeds.

    Only write-capable activities (write_file, edit_file, apply_patch, revert_file)
    are checked. Read-only activity such as read_file is ignored.
    """
    if not session_root or not task_slug or not claimed_files:
        return None

    # Write-capable tool kinds and modes that indicate file modification.
    _WRITE_KINDS = {"write_file", "edit_file", "apply_patch", "revert_file"}
    _WRITE_MODES = {"create", "edit", "patch", "revert"}

    claimed = frozenset(claimed_files)
    ignored_exact, ignored_prefix_dirs, ignored_named_dirs, ignored_globs = _split_ignore_entries(
        scope_ignore_files
    )
    transient_paths: set[str] = set()
    # Read all events for this task across all panes in the current run.
    # This ensures transient scope enforcement fails closed across retries:
    # an unclaimed write from any attempt in the active run causes rejection.
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
            if not isinstance(path, str):
                continue
            # Only collect write-capable activity; ignore read-only operations.
            kind = item.get("kind")
            mode = item.get("mode")
            if kind in _WRITE_KINDS or mode in _WRITE_MODES:
                transient_paths.add(path)

    unclaimed = sorted(
        p
        for p in transient_paths
        if p not in claimed
        and not _is_scope_ignored(
            p, ignored_exact, ignored_prefix_dirs, ignored_named_dirs, ignored_globs
        )
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
    read_files: Sequence[str] = (),
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
        result = _check_scope(actual_files, claimed_files, scope_ignore_files, read_files)
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


def _file_in_base(worktree_path: Path, rel_path: str) -> bool:
    """Return True if the file exists in the worktree's HEAD commit."""
    res = subprocess.run(
        ["git", "cat-file", "-t", f"HEAD:{rel_path}"],
        cwd=worktree_path,
        capture_output=True,
    )
    return res.returncode == 0


def _file_existed_at(worktree_path: Path, rel_path: str, commit: str) -> bool:
    """Return True if the file existed at the given commit."""
    res = subprocess.run(
        ["git", "cat-file", "-t", f"{commit}:{rel_path}"],
        cwd=worktree_path,
        capture_output=True,
    )
    return res.returncode == 0


def _worker_changed_lines(worktree_path: Path, rel_path: str) -> set[int]:
    """Parse ``git diff --unified=0`` to get 0-indexed line numbers the worker changed."""
    result = subprocess.run(
        ["git", "diff", "--unified=0", "--", rel_path],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    changed: set[int] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("@@"):
            continue
        # Format: @@ -a[,b] +c[,d] @@
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if match:
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            for i in range(start, start + count):
                changed.add(i - 1)  # git is 1-indexed, we use 0-indexed
    return changed


def _is_import_block_line(line: str, in_paren: bool) -> tuple[bool, bool]:
    """Return (is_part_of_import_block, currently_inside_parens).

    Handles multiline imports like ``from x import (\\n a, b\\n)`` and
    comment lines interleaved between imports.
    """
    stripped = line.strip()
    if in_paren:
        # Inside a parenthesized import — everything until closing paren
        return True, ")" not in stripped
    if stripped.startswith(("import ", "from ")):
        return True, "(" in stripped and ")" not in stripped
    # Blank lines and comments between imports are part of the block
    if stripped == "" or stripped.startswith("#"):
        return True, False
    return False, False


def _expand_to_import_blocks(lines: list[str], changed: set[int]) -> set[int]:
    """Expand changed lines to cover entire import blocks when any import was touched.

    Import reordering is a structural change that affects the whole block.
    If the worker added or modified any import line, autofix needs to own
    the entire contiguous import section to apply sorting correctly.

    Handles multiline imports (``from x import (...)``), comment lines
    between imports, and blank separator lines.
    """
    if not changed:
        return changed

    # Find contiguous import blocks
    blocks: list[tuple[int, int]] = []
    i = 0
    in_paren = False
    while i < len(lines):
        is_import, in_paren = _is_import_block_line(lines[i], in_paren)
        if is_import:
            start = i
            while i < len(lines):
                is_import, in_paren = _is_import_block_line(lines[i], in_paren)
                if is_import:
                    i += 1
                else:
                    break
            # Trim trailing blank/comment-only lines from block boundary
            end = i
            while end > start and lines[end - 1].strip() in ("", "#"):
                end -= 1
            if end > start:
                blocks.append((start, end))
        else:
            i += 1

    expanded = set(changed)
    for start, end in blocks:
        if any(ln in changed for ln in range(start, end)):
            expanded.update(range(start, end))
    return expanded


def _scope_to_changed(
    worker_lines: list[str],
    fixed_lines: list[str],
    changed_lines: set[int],
) -> list[str]:
    """Keep autofix changes only within worker-modified regions.

    Uses ``difflib.SequenceMatcher`` to align the pre- and post-autofix
    versions.  For each difference block:
    - **replace/delete**: if any original line is in ``changed_lines``, keep
      the autofix version; otherwise revert to the worker's text.
    - **insert** (``i1 == i2``): keep the insertion when the insertion point
      is adjacent to a changed line.  This handles import reordering where
      SequenceMatcher splits the block into separate insert + delete ops.
    """
    import difflib

    sm = difflib.SequenceMatcher(None, worker_lines, fixed_lines)
    result: list[str] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            result.extend(worker_lines[i1:i2])
        elif tag == "insert":
            # i1 == i2 for inserts — check adjacency
            adjacent = i1 in changed_lines or (i1 > 0 and i1 - 1 in changed_lines)
            if adjacent:
                result.extend(fixed_lines[j1:j2])
        elif tag == "delete":
            if any(ln in changed_lines for ln in range(i1, i2)):
                pass  # accept deletion
            else:
                result.extend(worker_lines[i1:i2])
        else:
            # replace
            if any(ln in changed_lines for ln in range(i1, i2)):
                result.extend(fixed_lines[j1:j2])
            else:
                result.extend(worker_lines[i1:i2])
    return result


def _autofix_full(
    rel_files: list[str],
    worktree_path: Path,
    config: ProjectConfig,
) -> None:
    """Run lint-fix + SmartFixer + format on full files (used for new files)."""
    if not rel_files:
        return
    _run_cmd(config.lint_fix_cmd, rel_files, worktree_path, timeout=config.settlement_timeout)
    SmartFixer(worktree_path, line_length=config.line_length).fix_all(rel_files)
    _run_cmd(config.format_cmd, rel_files, worktree_path, timeout=config.settlement_timeout)


def _autofix_scoped_single(
    worktree_path: Path,
    rel_path: str,
    config: ProjectConfig,
) -> None:
    """Autofix one existing file, scoped to worker-changed regions only.

    1. Record which lines the worker changed (via ``git diff``).
    2. Save the worker's content.
    3. Run full autofix (ruff + SmartFixer + format).
    4. Use ``_scope_to_changed`` to keep only autofix changes that overlap
       with the worker's edits. Pre-existing code style is preserved.
    """
    path = worktree_path / rel_path

    changed_lines = _worker_changed_lines(worktree_path, rel_path)
    if not changed_lines:
        return  # worker didn't touch this file

    worker_lines = path.read_text().splitlines(keepends=True)

    # Expand changed_lines to cover entire import blocks when worker touched imports
    changed_lines = _expand_to_import_blocks(worker_lines, changed_lines)

    # Run full autofix in-place
    _run_cmd(config.lint_fix_cmd, [rel_path], worktree_path, timeout=config.settlement_timeout)
    SmartFixer(worktree_path, line_length=config.line_length).fix_all([rel_path])
    _run_cmd(config.format_cmd, [rel_path], worktree_path, timeout=config.settlement_timeout)

    fixed_lines = path.read_text().splitlines(keepends=True)
    if worker_lines == fixed_lines:
        return  # autofix changed nothing

    scoped = _scope_to_changed(worker_lines, fixed_lines, changed_lines)
    path.write_text("".join(scoped))


def autofix_sandbox(
    worktree_path: Path,
    file_claims: tuple[str, ...] = (),
    config: ProjectConfig | None = None,
) -> None:
    """Mechanical auto-fix: lint fix then format. Called BEFORE commit.

    New files get full autofix (no pre-existing code to preserve).
    Existing files get scoped autofix — only worker-changed regions are
    modified, preserving pre-existing code style.

    Order matters: lint fix can change formatting, so format runs LAST.
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

    # Split into new files (full autofix) and existing files (scoped autofix)
    new_files = [f for f in rel if not _file_in_base(worktree_path, f)]
    existing_files = [f for f in rel if _file_in_base(worktree_path, f)]

    _autofix_full(new_files, worktree_path, config)

    for f in existing_files:
        _autofix_scoped_single(worktree_path, f, config)


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


def _test_targets_for_changed_files(
    config: ProjectConfig, changed_files: Sequence[str], worktree_path: Path
) -> list[str]:
    """Return test targets related to the changed source files."""
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

    return targets


def _build_test_cmd(config: ProjectConfig, changed_files: list[str], worktree_path: Path) -> str:
    """Return test command scoped to related tests only.

    Changed test files run directly. Changed source files trigger only
    tests that import from the changed modules — never the full suite.
    """
    if not config.test_cmd:
        return ""
    if "{test_dir}" not in config.test_cmd:
        return config.test_cmd

    targets = _test_targets_for_changed_files(config, changed_files, worktree_path)
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


def _normalize_coverage_path(path: str, worktree_path: Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        with contextlib.suppress(ValueError):
            candidate = candidate.relative_to(worktree_path)
    normalized = PurePosixPath(candidate.as_posix()).as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _coverage_percentages(data: object, worktree_path: Path) -> dict[str, float]:
    if not isinstance(data, Mapping):
        raise ValueError("coverage JSON root must be an object")
    root = cast(Mapping[str, object], data)
    files = root.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("coverage JSON missing files object")
    file_map = cast(Mapping[object, object], files)

    percentages: dict[str, float] = {}
    for raw_path, payload in file_map.items():
        if not isinstance(raw_path, str) or not isinstance(payload, Mapping):
            continue
        payload_map = cast(Mapping[str, object], payload)
        summary = payload_map.get("summary")
        if not isinstance(summary, Mapping):
            continue
        summary_map = cast(Mapping[str, object], summary)
        percent = summary_map.get("percent_covered")
        if isinstance(percent, int | float):
            percentages[_normalize_coverage_path(raw_path, worktree_path)] = float(percent)
    return percentages


def _format_percent(value: float) -> str:
    return f"{value:g}%"


def _copy_coverage_baseline_into_worktree(worktree_path: Path, project_root: str) -> Path | None:
    source = Path(project_root) / _COVERAGE_BASELINE
    if not source.exists():
        return None

    dst_dir = worktree_path / _COVERAGE_BASELINE_DIR
    source_dir = source.parent.resolve()
    if source_dir != dst_dir.resolve():
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
        shutil.copytree(source.parent, dst_dir)
    return worktree_path / _COVERAGE_BASELINE


def _build_coverage_cmd(
    config: ProjectConfig,
    changed_files: Sequence[str],
    worktree_path: Path,
    output_path: Path,
) -> str:
    if not config.coverage_cmd or "{output}" not in config.coverage_cmd:
        return ""

    targets = _test_targets_for_changed_files(config, changed_files, worktree_path)
    if not targets:
        return ""

    cmd = config.coverage_cmd.replace("{output}", str(output_path))
    target_args = " ".join(shlex.quote(target) for target in targets)
    if "{test_dir}" in cmd:
        return cmd.replace("{test_dir}", target_args)
    return f"{cmd} {target_args}"


def _run_coverage_gate(
    worktree_path: Path,
    changed_files: Sequence[str],
    project_root: str,
    config: ProjectConfig,
) -> GateResult | None:
    """Reject coverage regressions for changed Python files when coverage is configured."""
    if not config.coverage_cmd:
        return None
    if "{output}" not in config.coverage_cmd:
        logger.warning("Skipping coverage gate: coverage_cmd must include {output}")
        return None

    try:
        baseline_path = _copy_coverage_baseline_into_worktree(worktree_path, project_root)
        if baseline_path is None or not baseline_path.exists():
            return None

        import json
        import tempfile

        baseline_data = json.loads(baseline_path.read_text())
        with tempfile.NamedTemporaryFile(
            prefix="dgov-coverage-", suffix=".json", dir=worktree_path, delete=False
        ) as tmp:
            output_path = Path(tmp.name)

        try:
            coverage_cmd = _build_coverage_cmd(
                config,
                changed_files,
                worktree_path,
                output_path,
            )
            if not coverage_cmd:
                return None

            res = subprocess.run(
                coverage_cmd,
                shell=True,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=config.settlement_timeout,
                check=False,
            )
            if res.returncode != 0:
                output = ((res.stdout or "") + (res.stderr or ""))[-500:]
                logger.warning("Coverage gate measurement failed: %s", output)
                return None

            current_data = json.loads(output_path.read_text())
        finally:
            if output_path.exists():
                output_path.unlink()

        baseline = _coverage_percentages(baseline_data, worktree_path)
        current = _coverage_percentages(current_data, worktree_path)
        threshold = config.coverage_threshold

        for file in changed_files:
            rel = _normalize_coverage_path(file, worktree_path)
            if not rel.endswith(".py") or rel not in baseline:
                continue
            old = baseline[rel]
            new = current.get(rel, 0.0)
            if old - new > threshold:
                return GateResult(
                    passed=False,
                    error=(
                        f"Coverage regression: {rel} dropped from "
                        f"{_format_percent(old)} to {_format_percent(new)}"
                    ),
                )
    except Exception as exc:
        logger.warning("Coverage gate skipped after measurement error: %s", exc)
    return None


_DIAG_COUNT_RE = re.compile(r"Found (\d+) diagnostics?")


def _count_diagnostics(output: str) -> int:
    """Parse 'Found N diagnostics' from type checker output."""
    m = _DIAG_COUNT_RE.search(output)
    return int(m.group(1)) if m else 0


# Regex to parse ty/basedpyright output format:
#   error[error-code]: message
#      --> file/path.py:line:col
_DIAG_ERROR_CODE_RE = re.compile(r"^error\[([^\]]+)\]:", re.MULTILINE)
_DIAG_FILE_PATH_RE = re.compile(r"^\s+-->\s+([^:]+):\d+:\d+", re.MULTILINE)


def _parse_diagnostic_identities(
    output: str, project_root: Path | None = None
) -> set[tuple[str, str]]:
    """Extract (relative_file, error_code) tuples from type checker output.

    Ignores line numbers (which shift when code is edited) to enable
    identity-based comparison between baseline and worktree.

    The ty/basedpyright output format is:
        error[error-code]: message
           --> file/path.py:line:col
    """
    identities: set[tuple[str, str]] = set()

    # Find all error codes and file paths
    error_codes = _DIAG_ERROR_CODE_RE.findall(output)
    file_paths = _DIAG_FILE_PATH_RE.findall(output)

    # Pair them up - they appear in order in the output
    for i, code in enumerate(error_codes):
        if i < len(file_paths):
            file_path = file_paths[i]
            # Make path relative to project root if provided
            if project_root is not None:
                with contextlib.suppress(ValueError):
                    file_path = str(Path(file_path).relative_to(project_root))
            identities.add((file_path, code))

    return identities


def _type_check_gate(
    type_check_cmd: str,
    worktree_path: Path,
    project_root: str,
    timeout: int = 120,
) -> GateResult | None:
    """Run type checker with baseline comparison.

    Runs the type checker in both the project root (baseline) and the
    worktree. Only fails if the worktree introduces NEW diagnostic identities
    (file, error_code pairs) that don't exist in the baseline — pre-existing
    errors are not the worker's fault, even if line numbers shift.
    """
    # Baseline: run in project root (current HEAD)
    baseline_res = subprocess.run(
        type_check_cmd,
        shell=True,
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    baseline_output = (baseline_res.stdout or "") + (baseline_res.stderr or "")
    baseline_ids = _parse_diagnostic_identities(baseline_output, Path(project_root))

    # Worktree: run against worker's changes
    worktree_res = subprocess.run(
        type_check_cmd,
        shell=True,
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    worktree_output = (worktree_res.stdout or "") + (worktree_res.stderr or "")
    worktree_ids = _parse_diagnostic_identities(worktree_output, worktree_path)

    # Compare identity sets: new diagnostics are those in worktree but not baseline
    new_ids = worktree_ids - baseline_ids

    if worktree_res.returncode != 0 and new_ids:
        output = worktree_output[-500:]
        return GateResult(
            passed=False,
            error=f"Type check failure ({len(new_ids)} new diagnostic(s), "
            f"{len(worktree_ids)} total):\n{output}",
        )
    if worktree_res.returncode != 0 and not new_ids:
        logger.warning(
            "Type check: %d diagnostic(s) (all pre-existing) — not blocking",
            len(worktree_ids),
        )
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


def _scoped_lint_check(
    worktree_path: Path,
    existing_files: list[str],
    base_commit: str,
    config: ProjectConfig,
) -> GateResult | None:
    """Run lint check scoped to worker-changed lines only.

    Uses ``ruff check --output-format=json`` to get per-diagnostic line numbers,
    then filters to only lines changed between ``base_commit`` and HEAD.
    Pre-existing lint issues in unchanged regions are ignored.

    Only works with Ruff (requires ``--output-format=json``). The caller
    must check ``"ruff" in config.lint_cmd`` before calling this function.
    """
    import json as json_mod

    # Build changed-line sets per file
    changed_by_file: dict[str, set[int]] = {}
    for f in existing_files:
        diff_res = subprocess.run(
            ["git", "diff", "--unified=0", base_commit, "HEAD", "--", f],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        lines: set[int] = set()
        for line in diff_res.stdout.splitlines():
            if not line.startswith("@@"):
                continue
            match = re.search(r"\+(\d+)(?:,(\d+))?", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) is not None else 1
                lines.update(range(start, start + count))
        if lines:
            changed_by_file[f] = lines

    if not changed_by_file:
        return None

    # Run ruff with JSON output on all existing files at once
    file_args = " ".join(shlex.quote(f) for f in existing_files)
    lint_json_cmd = config.lint_cmd.replace("{file}", file_args)
    # Inject --output-format=json before the file args
    lint_json_cmd = lint_json_cmd + " --output-format=json"

    res = subprocess.run(
        lint_json_cmd,
        shell=True,
        cwd=worktree_path,
        capture_output=True,
        text=True,
        timeout=config.settlement_timeout,
    )

    if res.returncode == 0:
        return None  # no lint issues at all

    # Parse JSON diagnostics and filter to changed lines
    try:
        diagnostics = json_mod.loads(res.stdout)
    except (json_mod.JSONDecodeError, ValueError):
        # Can't parse JSON — fall back to full lint (fail-closed)
        output = (res.stdout + res.stderr)[-500:]
        return GateResult(passed=False, error=f"Lint failure:\n{output}")

    scoped_issues: list[str] = []
    for diag in diagnostics:
        filename = diag.get("filename", "")
        # ruff outputs absolute paths; convert to relative
        try:
            rel_name = str(Path(filename).relative_to(worktree_path))
        except ValueError:
            rel_name = filename
        row = diag.get("location", {}).get("row", 0)
        if rel_name in changed_by_file and row in changed_by_file[rel_name]:
            code = diag.get("code", "?")
            msg = diag.get("message", "")
            scoped_issues.append(f"{rel_name}:{row} {code} {msg}")

    if scoped_issues:
        detail = "\n".join(scoped_issues[:10])
        return GateResult(passed=False, error=f"Lint failure (worker-changed lines):\n{detail}")
    return None


def _run_acceptance_gates(
    worktree_path: Path,
    changed_files: Sequence[str],
    project_root: str,
    config: ProjectConfig,
    base_commit: str | None = None,
) -> GateResult:
    """Run the shared acceptance gates for a resolved changed-file set.

    When ``base_commit`` is provided (post-commit validation), lint and format
    checks are scoped to worker-changed lines for existing files, preventing
    false failures from pre-existing style issues.
    """
    if not changed_files:
        return GateResult(passed=True)

    # 0. Run setup command (e.g. npm ci for JS/TS worktrees)
    setup_failure = _run_setup_cmd(config.setup_cmd or "", worktree_path)
    if setup_failure is not None:
        return setup_failure

    existing_changed_files = _existing_files(worktree_path, changed_files)

    try:
        if existing_changed_files:
            if base_commit:
                # Post-commit: scope lint to worker-changed lines only.
                # Format check is skipped for existing files — scoped autofix
                # already formatted the worker's regions, and pre-existing
                # format issues are not the worker's responsibility.
                new_in_commit = [
                    f
                    for f in existing_changed_files
                    if not _file_existed_at(worktree_path, f, base_commit)
                ]
                preexisting = [f for f in existing_changed_files if f not in new_in_commit]

                # New files: full lint + format check
                if new_in_commit:
                    res_lint = _run_cmd(
                        config.lint_cmd,
                        new_in_commit,
                        worktree_path,
                        timeout=config.settlement_timeout,
                    )
                    if res_lint.returncode != 0:
                        output = (res_lint.stdout + res_lint.stderr)[-500:]
                        return GateResult(passed=False, error=f"Lint failure:\n{output}")
                    res_fmt = _run_cmd(
                        config.format_check_cmd,
                        new_in_commit,
                        worktree_path,
                        timeout=config.settlement_timeout,
                    )
                    if res_fmt.returncode != 0:
                        output = (res_fmt.stdout + res_fmt.stderr)[-500:]
                        return GateResult(passed=False, error=f"Format failure:\n{output}")

                # Existing files: scoped lint when Ruff available, else full lint
                if preexisting:
                    if "ruff" in config.lint_cmd:
                        lint_result = _scoped_lint_check(
                            worktree_path, preexisting, base_commit, config
                        )
                        if lint_result is not None:
                            return lint_result
                    else:
                        # Non-Ruff linters: can't scope, run full lint
                        res_lint = _run_cmd(
                            config.lint_cmd,
                            preexisting,
                            worktree_path,
                            timeout=config.settlement_timeout,
                        )
                        if res_lint.returncode != 0:
                            output = (res_lint.stdout + res_lint.stderr)[-500:]
                            return GateResult(passed=False, error=f"Lint failure:\n{output}")
            else:
                # Pre-commit / preflight: full checks (current behavior)
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
            ty_failure = _type_check_gate(
                config.type_check_cmd,
                worktree_path,
                project_root,
                timeout=config.settlement_timeout,
            )
            if ty_failure is not None:
                return ty_failure

        test_cmd = _build_test_cmd(config, list(changed_files), worktree_path)
        if test_cmd:
            test_failure = _run_test_gate(
                test_cmd, worktree_path, timeout=config.settlement_timeout
            )
            if test_failure is not None:
                return test_failure

        coverage_failure = _run_coverage_gate(
            worktree_path,
            changed_files,
            project_root,
            config,
        )
        if coverage_failure is not None:
            return coverage_failure

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
    return _run_acceptance_gates(
        worktree_path, changed_files, project_root, config, base_commit=base_commit
    )
