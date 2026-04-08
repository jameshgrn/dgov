"""Atomic Actuators for dgov Workers.

Pillar #1: Separation of Powers - These tools implement; the Governor validates.
Pillar #7: Zero Ambient Authority - Sandboxed execution in worktree.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AtomicConfig:
    """Minimal project config for worker — no dgov imports."""

    language: str = "python"
    src_dir: str = "src/"
    test_dir: str = "tests/"
    test_cmd: str = "python -m pytest {test_dir} -q --tb=short"
    lint_cmd: str = "python -m ruff check {file}"
    format_cmd: str = "python -m ruff format {file}"
    lint_fix_cmd: str = "python -m ruff check --fix --unsafe-fixes {file}"
    line_length: int = 99
    test_markers: tuple[str, ...] = ()
    conventions: dict[str, str] | None = None


def shell_quote(s: str) -> str:
    """Shell-safe quoting for subprocess args."""
    import shlex

    return shlex.quote(s)


class AtomicTools:
    """The Actuator Layer: Strict, isolated tools."""

    def __init__(self, worktree: Path, config: AtomicConfig) -> None:
        self.worktree = worktree.resolve()
        self.config = config
        # Resolve python/venv paths once at init, not per-command
        self._python_bin = Path(sys.executable).parent
        self._python = sys.executable
        # Sandbox HOME outside worktree — prevents macOS Library/ polluting git status
        self._sandbox_home = Path(tempfile.mkdtemp(prefix="dgov-sandbox-"))

    def _sandbox_env(self) -> dict[str, str]:
        return {
            "PATH": f"{self._python_bin}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self._sandbox_home),
            "LANG": "en_US.UTF-8",
            "PYTHONPATH": str(self.worktree / self.config.src_dir.rstrip("/")),
        }

    def _check_path(self, path: str) -> Path | str:
        """Resolve and validate path is within worktree. Returns Path or error string."""
        target = (self.worktree / path).resolve()
        if not str(target).startswith(str(self.worktree)):
            return "Error: Path traversal attempt blocked."
        return target

    # -- Core tools --

    def read_file(self, path: str, start_line: int = 0, end_line: int = 0) -> str:
        """Read a file, optionally a specific line range (1-indexed, inclusive)."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        content = target.read_text()
        if start_line > 0:
            lines = content.splitlines(keepends=True)
            end = end_line if end_line > 0 else len(lines)
            start = max(1, start_line)
            selected = lines[start - 1 : end]
            numbered = [f"{start + i}: {line}" for i, line in enumerate(selected)]
            return "".join(numbered)
        return content

    def write_file(self, path: str, content: str) -> str:
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"Successfully wrote {len(content)} bytes to {path}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        """Replace old_text with new_text. Fails if not found or ambiguous."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        content = target.read_text()
        count = content.count(old_text)
        if count == 0:
            return f"Error: old_text not found in {path}."
        if count > 1:
            return f"Error: old_text matches {count} locations in {path}. Be more specific."
        target.write_text(content.replace(old_text, new_text, 1))
        return f"Successfully edited {path}"

    def apply_patch(self, path: str, patch: str) -> str:
        """Apply a unified diff patch to a file. Handles multi-hunk edits."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."

        original = target.read_text().splitlines(keepends=True)
        result: list[str] = []
        orig_idx = 0

        for line in patch.splitlines(keepends=True):
            # Skip diff headers
            if line.startswith(("---", "+++", "diff ")):
                continue
            if line.startswith("@@"):
                # Parse hunk header: @@ -start,count +start,count @@
                m = re.match(r"@@ -(\d+)", line)
                if not m:
                    return f"Error: Malformed hunk header: {line.rstrip()}"
                hunk_start = int(m.group(1)) - 1  # 0-indexed
                # Copy lines before this hunk
                result.extend(original[orig_idx:hunk_start])
                orig_idx = hunk_start
                continue
            if line.startswith("-"):
                # Remove line — advance past it in original
                if orig_idx < len(original):
                    orig_idx += 1
            elif line.startswith("+"):
                # Add line
                result.append(line[1:])
            elif line.startswith(" ") and orig_idx < len(original):
                # Context line — copy and advance
                result.append(original[orig_idx])
                orig_idx += 1

        # Copy remaining original lines after last hunk
        result.extend(original[orig_idx:])
        target.write_text("".join(result))
        return f"Successfully patched {path}"

    def run_bash(self, cmd: str) -> str:
        """Pillar #7: Zero Ambient Authority - sandboxed execution in worktree."""
        # Reject commands that reference absolute paths (escape attempts)
        if re.search(r"(?<![.\w])/(?:etc|tmp|var|usr|opt|home|Users|root|bin|sbin)\b", cmd):
            return "Error: Absolute paths are not allowed. Use relative paths within the worktree."
        try:
            res = subprocess.run(
                ["/bin/sh", "-c", f"cd {shell_quote(str(self.worktree))} && {cmd}"],
                cwd=self.worktree,
                env=self._sandbox_env(),
                capture_output=True,
                text=True,
                timeout=60,
            )
            return f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}\nEXIT:{res.returncode}"
        except subprocess.TimeoutExpired:
            return "Error: Command timed out after 60s."

    # -- Navigation tools --

    def grep(self, pattern: str, path: str = ".") -> str:
        """Search file contents by regex pattern. Returns matching lines with file:line prefix."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target

        try:
            regex = re.compile(pattern)
        except re.error as e:
            return f"Error: Invalid regex: {e}"

        results: list[str] = []
        search_root = target if target.is_dir() else target.parent
        files = [target] if target.is_file() else sorted(search_root.rglob("*"))

        for f in files:
            if not f.is_file() or f.suffix in (".pyc", ".pyo", ".so", ".dylib"):
                continue
            rel = str(f.relative_to(self.worktree))
            if any(part.startswith(".") for part in f.parts):
                continue
            try:
                for i, line in enumerate(f.read_text().splitlines(), 1):
                    if regex.search(line):
                        results.append(f"{rel}:{i}: {line}")
                        if len(results) >= 100:
                            results.append("... (truncated at 100 matches)")
                            return "\n".join(results)
            except (UnicodeDecodeError, PermissionError):
                continue

        return "\n".join(results) if results else "No matches found."

    def glob(self, pattern: str) -> str:
        """Find files matching a glob pattern. Returns newline-separated relative paths."""
        results: list[str] = []
        for f in sorted(self.worktree.rglob("*")):
            if not f.is_file():
                continue
            rel = str(f.relative_to(self.worktree))
            if any(part.startswith(".") for part in f.relative_to(self.worktree).parts):
                continue
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f.name, pattern):
                results.append(rel)
                if len(results) >= 200:
                    results.append("... (truncated at 200 files)")
                    break
        return "\n".join(results) if results else "No files matched."

    def list_dir(self, path: str = ".") -> str:
        """List directory contents with type indicators (/ for dirs, sizes for files)."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        if not target.is_dir():
            return f"Error: {path} is not a directory."

        entries: list[str] = []
        for item in sorted(target.iterdir()):
            if item.name.startswith("."):
                continue
            rel = str(item.relative_to(self.worktree))
            if item.is_dir():
                entries.append(f"{rel}/")
            else:
                size = item.stat().st_size
                entries.append(f"{rel}  ({size} bytes)")
        return "\n".join(entries) if entries else "(empty directory)"

    def git_diff(self) -> str:
        """Show uncommitted changes in the worktree."""
        try:
            res = subprocess.run(
                ["git", "diff", "HEAD"],
                cwd=self.worktree,
                capture_output=True,
                text=True,
                timeout=10,
            )
            diff = res.stdout.strip()
            if not diff:
                status = subprocess.run(
                    ["git", "status", "--short"],
                    cwd=self.worktree,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return status.stdout.strip() or "No changes."
            if len(diff) > 5000:
                return diff[:5000] + "\n... (truncated at 5000 chars)"
            return diff
        except subprocess.TimeoutExpired:
            return "Error: git diff timed out."

    def recent_changes(self, path: str) -> str:
        """Show recent git commits that touched a file. Gives context on intent."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        rel = str(target.relative_to(self.worktree))
        try:
            res = subprocess.run(
                ["git", "log", "--oneline", "-10", "--", rel],
                cwd=self.worktree,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return res.stdout.strip() or f"No git history for {path}."
        except subprocess.TimeoutExpired:
            return "Error: git log timed out."

    def assert_file_unchanged(self, path: str) -> str:
        """Verify a file has NOT been modified from HEAD. Use to self-check scope."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        rel = str(target.relative_to(self.worktree))
        try:
            res = subprocess.run(
                ["git", "diff", "HEAD", "--", rel],
                cwd=self.worktree,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.stdout.strip():
                return f"FAIL: {path} has been modified:\n{res.stdout[:500]}"
            return f"OK: {path} is unchanged from HEAD."
        except subprocess.TimeoutExpired:
            return "Error: git diff timed out."

    def revert_file(self, path: str) -> str:
        """Undo all uncommitted changes to a file by checking it out from HEAD."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        rel = str(target.relative_to(self.worktree))
        try:
            subprocess.run(
                ["git", "checkout", "HEAD", "--", rel],
                cwd=self.worktree,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            return f"Successfully reverted {path} to HEAD."
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            return f"Error: Failed to revert {path}: {getattr(e, 'stderr', str(e))}"

    # -- Code intelligence tools --

    def find_references(self, symbol: str, exclude_tests: bool = False) -> str:
        """Find all occurrences of a symbol across the codebase (excluding binary/hidden files)."""
        flags = "-w"  # word boundary
        if exclude_tests:
            # Escape ! for shell if needed, but ripgrep handles it in quotes
            flags += f" -g '!{self.config.test_dir}*'"

        # Try ripgrep first for speed and ignore-file respect
        result = self.ripgrep(symbol, flags=flags)
        if "command not found" in result:
            return self.grep(rf"\b{re.escape(symbol)}\b")
        if "EXIT:0" in result:
            # Extract just the matches from STDOUT: ... EXIT:0 format
            m = re.search(r"STDOUT:\n(.*?)\nSTDERR:", result, re.DOTALL)
            if m:
                return m.group(1).strip()
        if "EXIT:1" in result:
            return f"No matches found for '{symbol}'."
        return result

    def file_symbols(self, path: str) -> str:
        """List functions, classes, and top-level assignments with line numbers."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        if target.suffix != ".py":
            return f"Error: file_symbols only works on .py files, got {target.suffix}"

        try:
            tree = ast.parse(target.read_text(), filename=path)
        except SyntaxError as e:
            return f"Error: SyntaxError in {path}: {e}"

        symbols: list[str] = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                symbols.append(f"  class {node.name}:{node.lineno}")
                for item in ast.iter_child_nodes(node):
                    if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                        symbols.append(f"    def {node.name}.{item.name}:{item.lineno}")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                symbols.append(f"  def {node.name}:{node.lineno}")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        symbols.append(f"  {t.id} = ...:{node.lineno}")

        if not symbols:
            return f"No symbols found in {path}."
        return f"{path}:\n" + "\n".join(symbols)

    def check_syntax(self, path: str) -> str:
        """Quick syntax check via compile(). Faster than full linter."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        try:
            compile(target.read_text(), path, "exec")
            return f"OK: {path} has valid syntax."
        except SyntaxError as e:
            return f"SyntaxError in {path} line {e.lineno}: {e.msg}"

    def related_files(self, path: str) -> str:
        """Show files that import from this file AND files this file imports from."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        if target.suffix != ".py":
            return "Error: related_files only works on .py files."

        rel = str(target.relative_to(self.worktree))
        # Determine the module path for this file
        module_name = self._path_to_module(rel)

        imports_from: list[str] = []  # what this file imports
        imported_by: list[str] = []  # what imports this file

        # Parse this file's imports
        try:
            tree = ast.parse(target.read_text(), filename=path)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imports_from.append(node.module)
        except SyntaxError:
            pass

        # Scan all .py files for imports of this module
        for py_file in sorted(self.worktree.rglob("*.py")):
            if py_file == target:
                continue
            py_rel = str(py_file.relative_to(self.worktree))
            if any(part.startswith(".") for part in py_file.relative_to(self.worktree).parts):
                continue
            try:
                file_tree = ast.parse(py_file.read_text(), filename=py_rel)
                for node in ast.walk(file_tree):
                    if (
                        isinstance(node, ast.ImportFrom)
                        and node.module
                        and (
                            node.module == module_name or node.module.startswith(module_name + ".")
                        )
                    ):
                        imported_by.append(py_rel)
                        break
            except (SyntaxError, UnicodeDecodeError):
                continue

        lines: list[str] = [f"== {rel} =="]
        if imports_from:
            lines.append(f"\nImports from ({len(imports_from)}):")
            for mod in sorted(set(imports_from)):
                lines.append(f"  {mod}")
        if imported_by:
            lines.append(f"\nImported by ({len(imported_by)}):")
            for f in imported_by:
                lines.append(f"  {f}")
        if not imports_from and not imported_by:
            lines.append("  (no import relationships found)")
        return "\n".join(lines)

    def search_tests_for(self, symbol: str) -> str:
        """Find test files that reference a function, class, or module name."""
        test_dir = self.worktree / self.config.test_dir.rstrip("/")
        if not test_dir.is_dir():
            return f"Error: test directory {self.config.test_dir} does not exist."

        matches: list[str] = []
        try:
            pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
        except re.error:
            return f"Error: Invalid symbol name: {symbol}"

        for test_file in sorted(test_dir.rglob("test_*.py")):
            rel = str(test_file.relative_to(self.worktree))
            try:
                content = test_file.read_text()
                hit_lines: list[str] = []
                for i, line in enumerate(content.splitlines(), 1):
                    if pattern.search(line):
                        hit_lines.append(f"    {i}: {line.strip()}")
                if hit_lines:
                    matches.append(f"  {rel}:")
                    matches.extend(hit_lines[:5])
                    if len(hit_lines) > 5:
                        matches.append(f"    ... +{len(hit_lines) - 5} more")
            except (UnicodeDecodeError, PermissionError):
                continue

        if not matches:
            return f"No test files reference '{symbol}'."
        return f"Tests referencing '{symbol}':\n" + "\n".join(matches)

    def _path_to_module(self, rel_path: str) -> str:
        """Convert a relative file path to a Python module name."""
        # Strip src/ prefix and .py suffix
        mod = rel_path
        src = self.config.src_dir.rstrip("/")
        if mod.startswith(src + "/"):
            mod = mod[len(src) + 1 :]
        if mod.endswith(".py"):
            mod = mod[:-3]
        if mod.endswith("/__init__"):
            mod = mod[: -len("/__init__")]
        return mod.replace("/", ".")

    # -- Power tools (CLI wrappers) --

    def ripgrep(self, pattern: str, path: str = ".", flags: str = "") -> str:
        """Fast regex search via rg. Supports flags like -i, -l, -C3, --type py."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        rel = str(target.relative_to(self.worktree))
        cmd = f"rg {flags} -- {shell_quote(pattern)} {shell_quote(rel)}"
        result = self.run_bash(cmd)
        if "EXIT:2" in result or "command not found" in result:
            return self.grep(pattern, path)  # fallback to Python grep
        return result

    def jq(self, expr: str, path: str) -> str:
        """Query/transform JSON files with jq expressions."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        rel = str(target.relative_to(self.worktree))
        return self.run_bash(f"jq {shell_quote(expr)} {shell_quote(rel)}")

    def tree(self, path: str = ".", max_depth: int = 3) -> str:
        """Show directory structure as a tree. Excludes hidden dirs and __pycache__."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        rel = str(target.relative_to(self.worktree))
        # Try system tree, fall back to find-based
        result = self.run_bash(
            f"tree -L {max_depth} -I '__pycache__|.git|node_modules|.venv' "
            f"--noreport {shell_quote(rel)}"
        )
        if "command not found" in result:
            result = self.run_bash(
                f"find {shell_quote(rel)} -maxdepth {max_depth} "
                f"-not -path '*/__pycache__/*' -not -path '*/.git/*' "
                f"| head -100 | sort"
            )
        return result

    def word_count(self, path: str) -> str:
        """Count lines, words, chars in a file or directory of files."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        rel = str(target.relative_to(self.worktree))
        if target.is_dir():
            return self.run_bash(
                f"find {shell_quote(rel)} -name '*.py' -not -path '*/__pycache__/*' "
                f"| xargs wc -l | tail -20"
            )
        return self.run_bash(f"wc -l {shell_quote(rel)}")

    def head(self, path: str, n: int = 20) -> str:
        """Show first N lines of a file. Faster than read_file for quick peeks."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        lines = target.read_text().splitlines()[:n]
        return "\n".join(f"{i + 1}: {line}" for i, line in enumerate(lines))

    def tail(self, path: str, n: int = 20) -> str:
        """Show last N lines of a file."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: {path} does not exist."
        lines = target.read_text().splitlines()
        start = max(0, len(lines) - n)
        return "\n".join(f"{start + i + 1}: {line}" for i, line in enumerate(lines[start:]))

    # -- SOP compound tools --

    def run_tests(self, file: str = "") -> str:
        """Run tests using the project's declared test command."""
        cmd = self.config.test_cmd.replace("{test_dir}", self.config.test_dir)
        if file:
            cmd = cmd.replace(self.config.test_dir, file)
        return self.run_bash(cmd)

    def lint_check(self, file: str = "") -> str:
        """Run lint using the project's declared lint command."""
        target = file if file else self.config.src_dir
        cmd = self.config.lint_cmd.replace("{file}", target)
        return self.run_bash(cmd)

    def lint_fix(self, file: str = "") -> str:
        """Auto-fix lint issues (including unsafe fixes like unused variables)."""
        target = file if file else self.config.src_dir
        cmd = self.config.lint_fix_cmd.replace("{file}", target)
        return self.run_bash(cmd)

    def format_file(self, file: str) -> str:
        """Format a file using the project's formatter."""
        cmd = self.config.format_cmd.replace("{file}", file)
        return self.run_bash(cmd)


def get_tool_spec() -> list[Any]:
    return [
        # Core tools
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a file's contents. Use relative paths (e.g. 'src/foo.py'). "
                    "Optionally pass start_line and end_line (1-indexed, inclusive) to "
                    "read a specific range and save tokens on large files."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {
                            "type": "integer",
                            "description": "First line to read (1-indexed). 0 = read whole file.",
                            "default": 0,
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "Last line to read (inclusive). 0 = to end of file.",
                            "default": 0,
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "Write content to a file (full replacement). Creates parent dirs. "
                    "Prefer edit_file for modifying existing files."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": (
                    "Replace old_text with new_text in a file. Only the matched section "
                    "changes — all other content is preserved byte-for-byte. Fails if "
                    "old_text is not found or matches multiple locations (be more specific). "
                    "ALWAYS prefer this over write_file when modifying existing files."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_text": {
                            "type": "string",
                            "description": "Exact text to find (must match uniquely)",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "Replacement text",
                        },
                    },
                    "required": ["path", "old_text", "new_text"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": (
                    "Apply a unified diff patch to a file. Use when edit_file fails "
                    "due to ambiguity, or when making multi-hunk changes. Format: "
                    "standard unified diff (@@ -start,count +start,count @@, "
                    "lines prefixed with -, +, or space)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "patch": {
                            "type": "string",
                            "description": (
                                "Unified diff content (hunks with @@, -, +, space lines)"
                            ),
                        },
                    },
                    "required": ["path", "patch"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_bash",
                "description": "Run a shell command in the worktree. 60s timeout.",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            },
        },
        # Navigation tools
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "Search file contents by regex. Returns file:line: matches.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Regex pattern"},
                        "path": {
                            "type": "string",
                            "description": "File or directory to search (default: '.')",
                            "default": ".",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": "Find files matching a pattern (e.g. '*.py', 'tests/test_*.py').",
                "parameters": {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}},
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List directory contents with sizes.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory to list (default: '.')",
                            "default": ".",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "git_diff",
                "description": (
                    "Show your uncommitted changes so far. Use to review your work "
                    "before calling done."
                ),
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recent_changes",
                "description": (
                    "Show recent git commits that touched a file. Gives context "
                    "about recent modifications and intent before you edit."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "assert_file_unchanged",
                "description": (
                    "Verify that a file has NOT been modified from HEAD. Use as a "
                    "self-check to confirm you only touched the files you intended to."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "revert_file",
                "description": (
                    "Undo ALL uncommitted changes to a file. Use if you made a mistake "
                    "and want to start over from the file's original state."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        # Code intelligence tools
        {
            "type": "function",
            "function": {
                "name": "find_references",
                "description": (
                    "Search for all occurrences of a symbol across the entire codebase. "
                    "Use this to find usages, calls, and dependencies of a function or class."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "exclude_tests": {
                            "type": "boolean",
                            "default": False,
                            "description": "Skip test directory in results",
                        },
                    },
                    "required": ["symbol"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "file_symbols",
                "description": (
                    "List all functions, classes, and top-level assignments in a "
                    "Python file with line numbers. Use to quickly find where a "
                    "symbol is defined without reading the whole file."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_syntax",
                "description": (
                    "Quick syntax check (compile()) without running the full linter. "
                    "Use right after writing/editing to catch parse errors fast."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "related_files",
                "description": (
                    "Show the import neighborhood of a file: what it imports from "
                    "AND what other files import from it. Use before editing to "
                    "understand who depends on the code you're changing."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_tests_for",
                "description": (
                    "Find test files that reference a given function, class, or "
                    "module name. Returns matching test files with line numbers. "
                    "Use to find which tests to run after modifying code."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "symbol": {
                            "type": "string",
                            "description": "Function, class, or module name to search for",
                        },
                    },
                    "required": ["symbol"],
                },
            },
        },
        # Power tools (CLI wrappers)
        {
            "type": "function",
            "function": {
                "name": "ripgrep",
                "description": (
                    "Fast regex search via rg. Much faster than grep on large "
                    "codebases. Supports flags: -i (case insensitive), -l (files "
                    "only), -C3 (context), --type py (file type filter), -w (word)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {
                            "type": "string",
                            "description": "File or dir to search (default: '.')",
                            "default": ".",
                        },
                        "flags": {
                            "type": "string",
                            "description": "rg flags e.g. '-i -C3 --type py'",
                            "default": "",
                        },
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "jq",
                "description": (
                    "Query and transform JSON files with jq expressions. "
                    "Examples: '.key', '.[] | .name', 'keys', 'length'."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expr": {
                            "type": "string",
                            "description": "jq filter expression",
                        },
                        "path": {"type": "string"},
                    },
                    "required": ["expr", "path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tree",
                "description": (
                    "Show directory structure as a tree. Excludes __pycache__, "
                    ".git, node_modules. Great for understanding project layout."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "default": ".",
                        },
                        "max_depth": {
                            "type": "integer",
                            "description": "Max directory depth (default: 3)",
                            "default": 3,
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "word_count",
                "description": (
                    "Count lines in a file or all .py files in a directory. "
                    "Use to gauge file size before reading."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "head",
                "description": "Show first N lines of a file with line numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "n": {
                            "type": "integer",
                            "description": "Number of lines (default: 20)",
                            "default": 20,
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tail",
                "description": "Show last N lines of a file with line numbers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "n": {
                            "type": "integer",
                            "description": "Number of lines (default: 20)",
                            "default": 20,
                        },
                    },
                    "required": ["path"],
                },
            },
        },
        # SOP tools
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": "Run the project's test suite. Optionally target a specific file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Specific test file (default: run all)",
                            "default": "",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lint_check",
                "description": "Run the project's linter. Optionally target a specific file.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Specific file to lint (default: all source)",
                            "default": "",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lint_fix",
                "description": (
                    "Auto-fix lint issues including unused variables and imports. "
                    "Run this after editing to clean up trivial issues automatically."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "Specific file to fix (default: all source)",
                            "default": "",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "format_file",
                "description": "Format a file using the project's formatter.",
                "parameters": {
                    "type": "object",
                    "properties": {"file": {"type": "string"}},
                    "required": ["file"],
                },
            },
        },
        # Exit
        {
            "type": "function",
            "function": {
                "name": "done",
                "description": "Signal that the task is complete.",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            },
        },
    ]
