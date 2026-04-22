"""Atomic Actuators for dgov Workers.

Pillar #1: Separation of Powers - These tools implement; the Governor validates.
Pillar #7: Zero Ambient Authority - Sandboxed execution in worktree.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from dgov.tool_policy import ToolPolicy, parse_tool_policy


@dataclass(frozen=True)
class AtomicConfig:
    """Worker-facing project config. Single source of truth for every field
    the worker subprocess consumes. ProjectConfig inherits from this and adds
    governor-only fields. The worker subprocess never imports ProjectConfig,
    preserving subprocess isolation (Pillar #7).
    """

    language: str = "python"
    src_dir: str = "src/"
    test_dir: str = "tests/"
    llm_base_url: str = "https://api.fireworks.ai/inference/v1"
    llm_api_key_env: str = "FIREWORKS_API_KEY"
    test_cmd: str = "python -m pytest {test_dir} -q --tb=short"
    lint_cmd: str = "python -m ruff check {file}"
    format_cmd: str = "python -m ruff format {file}"
    lint_fix_cmd: str = "python -m ruff check --fix --unsafe-fixes {file}"
    type_check_cmd: str | None = None
    worker_iteration_budget: int = 50
    worker_iteration_warn_at: int = 40
    worker_tree_max_lines: int = 80
    line_length: int = 99
    test_markers: tuple[str, ...] = ()
    conventions: dict[str, str] = field(default_factory=dict)
    tool_policy: ToolPolicy = field(default_factory=ToolPolicy)


def _coerce_markers(v: object) -> tuple[str, ...]:
    if isinstance(v, (list, tuple)):
        return tuple(str(item) for item in v)
    return ()


def _coerce_conventions(v: object) -> dict[str, str]:
    if not isinstance(v, dict):
        return {}
    return {str(key): str(value) for key, value in v.items()}


def _coerce_tool_policy(v: object) -> ToolPolicy:
    return parse_tool_policy(v if isinstance(v, dict) else {})


# Per-field payload → AtomicConfig coercion. Fields omitted from this map
# are assigned directly (str/int/bool). One entry per non-trivial type.
_PAYLOAD_COERCERS: dict[str, Callable[[object], object]] = {
    "test_markers": _coerce_markers,
    "conventions": _coerce_conventions,
    "tool_policy": _coerce_tool_policy,
}

# Per-field AtomicConfig → JSON-serializable coercion. Must be idempotent
# with _PAYLOAD_COERCERS so payload → config → payload round-trips.
_PAYLOAD_SERIALIZERS: dict[str, Callable[[object], object]] = {
    "test_markers": lambda v: list(v) if v else [],
    "conventions": lambda v: dict(v) if v else {},
    "tool_policy": lambda v: v.as_jsonable() if isinstance(v, ToolPolicy) else {},
}


def atomic_config_from_payload(raw: Mapping[str, Any]) -> AtomicConfig:
    """Deserialize a worker payload into AtomicConfig. Unknown keys ignored;
    missing keys fall back to AtomicConfig field defaults."""
    values: dict[str, Any] = {}
    for f in fields(AtomicConfig):
        if f.name not in raw:
            continue
        coercer = _PAYLOAD_COERCERS.get(f.name)
        values[f.name] = coercer(raw[f.name]) if coercer else raw[f.name]
    return AtomicConfig(**values)


def atomic_config_to_payload(config: AtomicConfig) -> dict[str, object]:
    """Serialize AtomicConfig into a JSON-safe worker payload."""
    payload: dict[str, object] = {}
    for f in fields(AtomicConfig):
        value = getattr(config, f.name)
        serializer = _PAYLOAD_SERIALIZERS.get(f.name)
        payload[f.name] = serializer(value) if serializer else value
    return payload


def worker_payload_from_project_toml(raw: dict[str, Any]) -> dict[str, object]:
    """Normalize raw project.toml data into the worker payload shape.

    TOML has `[project]` plus top-level `[conventions]` and `[tool_policy]`
    sections. Flattens all three through AtomicConfig so payload shape stays
    in sync with field defs.
    """
    proj = raw.get("project", {})
    flat: dict[str, Any] = dict(proj)
    flat["conventions"] = raw.get("conventions", {})
    flat["tool_policy"] = raw.get("tool_policy", {})
    return atomic_config_to_payload(atomic_config_from_payload(flat))


def shell_quote(s: str) -> str:
    """Shell-safe quoting for subprocess args."""
    import shlex

    return shlex.quote(s)


def _unwrap_shell_command(tokens: list[str]) -> tuple[bool, list[str]]:
    """Peel off the narrow wrapper forms we intentionally understand."""
    if len(tokens) >= 2 and tokens[0] == "uv" and tokens[1] == "run":
        core = tokens[2:]
        if not core:
            return True, []
        return True, core
    return False, tokens


def _wrapped_verify_tool(tokens: list[str]) -> str | None:
    """Classify only the verification command forms we can identify confidently."""
    _, core = _unwrap_shell_command(tokens)
    if not core:
        return None

    if core[0] == "pytest":
        return "pytest"
    if core[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"]):
        return "pytest"
    if len(core) >= 2 and core[0] == "ruff" and core[1] == "check":
        if "--fix" in core:
            return "ruff_check_fix"
        return "ruff_check"
    if len(core) >= 2 and core[0] == "ruff" and core[1] == "format":
        return "ruff_format"
    if len(core) >= 2 and core[0] == "ty" and core[1] == "check":
        return "ty_check"
    if (
        core[:3] in (["python", "-m", "ty"], ["python3", "-m", "ty"])
        and len(core) >= 4
        and core[3] == "check"
    ):
        return "ty_check"
    return None


def _tool_bin_dirs(names: tuple[str, ...]) -> list[str]:
    """Return unique parent directories for the requested executables."""
    dirs: list[str] = []
    seen: set[str] = set()
    for name in names:
        resolved = shutil.which(name)
        if not resolved:
            continue
        parent = str(Path(resolved).resolve().parent)
        if parent in seen:
            continue
        seen.add(parent)
        dirs.append(parent)
    return dirs


class AtomicTools:
    """The Actuator Layer: Strict, isolated tools."""

    def __init__(self, worktree: Path, config: AtomicConfig) -> None:
        self.worktree = worktree.resolve()
        self.config = config
        # Resolve python/venv paths once at init, not per-command
        self._python_bin = Path(sys.executable).parent
        self._python = sys.executable
        self._tool_bin_dirs = _tool_bin_dirs(("uv", "sg"))
        # Sandbox HOME outside worktree — prevents macOS Library/ polluting git status
        self._sandbox_home = Path(tempfile.mkdtemp(prefix="dgov-sandbox-"))
        self._activity_log: list[dict[str, Any]] = []

    def _sandbox_env(self) -> dict[str, str]:
        path_parts = [
            str(self._python_bin),
            *self._tool_bin_dirs,
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
        ]
        return {
            "PATH": ":".join(dict.fromkeys(path_parts)),
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

    def _record_activity(self, kind: str, path: str, **extra: object) -> None:
        self._activity_log.append({"kind": kind, "path": path, **extra})

    def _consume_activity(self) -> list[dict[str, Any]]:
        activity = self._activity_log[:]
        self._activity_log.clear()
        return activity

    def _reject_shell_command(self, cmd: str) -> str | None:
        policy = self.config.tool_policy
        if not policy.restrict_run_bash:
            return None

        normalized = cmd.strip()
        lowered = normalized.lower()
        for denied in policy.deny_shell_commands:
            if lowered.startswith(denied.lower()):
                return (
                    f"Error: run_bash policy rejected '{cmd}'. "
                    f"Denied shell command prefix: {denied!r}."
                )

        if policy.deny_shell_file_mutations:
            if re.search(r"(^|[;&|]\s*)(rm|mv|cp|touch|mkdir)\b", normalized):
                return (
                    "Error: run_bash policy rejected file mutation shell command. "
                    "Use write_file/edit_file/apply_patch/revert_file instead."
                )
            if re.search(r"(>?>|<<|tee\b)", normalized):
                return (
                    "Error: run_bash policy rejected shell redirection into repo files. "
                    "Use write_file/edit_file/apply_patch instead."
                )

        try:
            tokens = shlex.split(normalized)
        except ValueError as exc:
            return f"Error: Invalid shell command: {exc}"
        if not tokens:
            return "Error: Empty shell command."

        uv_wrapped, core = _unwrap_shell_command(tokens)
        if not core:
            return "Error: Invalid 'uv run' command with no subcommand."

        tool = core[0]

        if policy.require_wrapped_verify_tools:
            verify_tool = _wrapped_verify_tool(tokens)
            if verify_tool == "pytest":
                return "Error: run_bash policy requires run_tests() for pytest invocations."
            if verify_tool == "ruff_check_fix":
                return "Error: run_bash policy requires lint_fix() for 'ruff check --fix'."
            if verify_tool == "ruff_check":
                return "Error: run_bash policy requires lint_check() for 'ruff check'."
            if verify_tool == "ruff_format":
                return "Error: run_bash policy requires format_file() for 'ruff format'."
            if verify_tool == "ty_check":
                return "Error: run_bash policy requires type_check() for 'ty check'."

        if (
            policy.require_uv_run
            and tool in {"python", "python3", "pytest", "ruff", "ty"}
            and not uv_wrapped
        ):
            return f"Error: run_bash policy requires 'uv run' for Python command '{tool}'."

        return None

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
        if target.exists():
            return (
                f"Error: {path} already exists. "
                "Use edit_file or apply_patch to modify existing files."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        self._record_activity("write_file", path, mode="create")
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
        self._record_activity("edit_file", path, mode="edit")
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
        self._record_activity("apply_patch", path, mode="patch")
        return f"Successfully patched {path}"

    def _execute_shell(self, cmd: str, *, enforce_policy: bool) -> str:
        """Run a shell command inside the sandbox, optionally enforcing run_bash policy."""
        if re.search(r"(?<![.\w])/(?:etc|tmp|var|usr|opt|home|Users|root|bin|sbin)\b", cmd):
            return "Error: Absolute paths are not allowed. Use relative paths within the worktree."
        if enforce_policy:
            policy_error = self._reject_shell_command(cmd)
            if policy_error is not None:
                return policy_error
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

    def run_bash(self, cmd: str) -> str:
        """Pillar #7: Zero Ambient Authority - sandboxed execution in worktree."""
        return self._execute_shell(cmd, enforce_policy=True)

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
            self._record_activity("revert_file", path, mode="revert")
            return f"Successfully reverted {path} to HEAD."
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            return f"Error: Failed to revert {path}: {getattr(e, 'stderr', str(e))}"

    # -- Code intelligence tools --

    def find_references(self, symbol: str, exclude_tests: bool = False) -> str:
        """Find lexical occurrences of a symbol across the codebase."""
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

    def ast_grep(self, pattern: str, path: str = ".", lang: str = "") -> str:
        """Search code structurally using ast-grep."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        if shutil.which("sg") is None:
            return "Error: ast-grep ('sg') not found in PATH."

        rel = str(target.relative_to(self.worktree))
        cmd = ["sg", "run", "--color", "never", "--heading", "never", "--pattern", pattern]
        if lang:
            cmd.extend(["--lang", lang])
        cmd.append(rel)

        try:
            res = subprocess.run(
                cmd,
                cwd=self.worktree,
                env=self._sandbox_env(),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "Error: ast-grep timed out after 30s."

        output = (res.stdout or "") + (res.stderr or "")
        if res.returncode == 0:
            stripped = output.strip()
            if len(stripped) > 5000:
                return stripped[:5000] + "\n... (truncated at 5000 chars)"
            return stripped or "No matches found."
        if res.returncode == 1:
            return "No matches found."
        return f"Error: ast-grep failed:\n{output.strip()}"

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
        """Heuristic import neighborhood for a Python file."""
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
        result = self._execute_shell(cmd, enforce_policy=False)
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
        return self._execute_shell(
            f"jq {shell_quote(expr)} {shell_quote(rel)}", enforce_policy=False
        )

    def tree(self, path: str = ".", max_depth: int = 3) -> str:
        """Show directory structure as a tree. Excludes hidden dirs and __pycache__."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        rel = str(target.relative_to(self.worktree))
        # Try system tree, fall back to find-based
        result = self._execute_shell(
            f"tree -L {max_depth} -I '__pycache__|.git|node_modules|.venv' "
            f"--noreport {shell_quote(rel)}",
            enforce_policy=False,
        )
        if "command not found" in result:
            result = self._execute_shell(
                f"find {shell_quote(rel)} -maxdepth {max_depth} "
                f"-not -path '*/__pycache__/*' -not -path '*/.git/*' "
                f"| head -100 | sort",
                enforce_policy=False,
            )
        return result

    def word_count(self, path: str) -> str:
        """Count lines, words, chars in a file or directory of files."""
        target = self._check_path(path)
        if isinstance(target, str):
            return target
        rel = str(target.relative_to(self.worktree))
        if target.is_dir():
            return self._execute_shell(
                f"find {shell_quote(rel)} -name '*.py' -not -path '*/__pycache__/*' "
                f"| xargs wc -l | tail -20",
                enforce_policy=False,
            )
        return self._execute_shell(f"wc -l {shell_quote(rel)}", enforce_policy=False)

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
        return self._execute_shell(cmd, enforce_policy=False)

    def lint_check(self, file: str = "") -> str:
        """Run lint using the project's declared lint command."""
        target = file if file else self.config.src_dir
        cmd = self.config.lint_cmd.replace("{file}", target)
        return self._execute_shell(cmd, enforce_policy=False)

    def lint_fix(self, file: str = "") -> str:
        """Auto-fix lint issues (including unsafe fixes like unused variables)."""
        target = file if file else self.config.src_dir
        cmd = self.config.lint_fix_cmd.replace("{file}", target)
        return self._execute_shell(cmd, enforce_policy=False)

    def format_file(self, file: str) -> str:
        """Format a file using the project's formatter."""
        cmd = self.config.format_cmd.replace("{file}", file)
        return self._execute_shell(cmd, enforce_policy=False)

    def type_check(self) -> str:
        """Run the project's type checker. Returns a message if not configured."""
        if not self.config.type_check_cmd:
            return "Type checking not configured for this project."
        return self._execute_shell(self.config.type_check_cmd, enforce_policy=False)


_RESEARCHER_EXCLUDED_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "apply_patch",
    "run_bash",
    "revert_file",
    "lint_fix",
    "format_file",
})

_PLANNER_EXCLUDED_TOOLS = frozenset(_RESEARCHER_EXCLUDED_TOOLS)

_EMIT_PLAN_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "emit_plan",
        "description": (
            "Emit a structured implementation plan. Terminal — calling this "
            "completes your mission. The plan must contain at least one task "
            "with file claims and a commit message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Kebab-case plan name (e.g. 'fix-auth-bug')",
                },
                "summary": {
                    "type": "string",
                    "description": "One-paragraph summary of the plan.",
                },
                "tasks": {
                    "type": "array",
                    "description": "Ordered list of tasks to execute.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "slug": {
                                "type": "string",
                                "description": "Unique kebab-case task identifier.",
                            },
                            "summary": {
                                "type": "string",
                                "description": "One-line task summary.",
                            },
                            "prompt": {
                                "type": "string",
                                "description": (
                                    "Full task prompt with Orient/Edit/Verify sections. "
                                    "This is what the worker will see."
                                ),
                            },
                            "commit_message": {
                                "type": "string",
                                "description": "Imperative commit message.",
                            },
                            "files": {
                                "type": "object",
                                "description": "File claims for this task.",
                                "properties": {
                                    "create": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "default": [],
                                    },
                                    "edit": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "default": [],
                                    },
                                    "touch": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "default": [],
                                    },
                                    "read": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "default": [],
                                    },
                                },
                            },
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Slugs of tasks this depends on.",
                                "default": [],
                            },
                            "role": {
                                "type": "string",
                                "enum": ["worker", "researcher", "reviewer"],
                                "default": "worker",
                            },
                        },
                        "required": ["slug", "summary", "prompt", "commit_message"],
                    },
                },
                "config_overrides": {
                    "type": "object",
                    "description": (
                        "Optional project config overrides discovered during analysis. "
                        "Supported keys: src_dir, test_dir, lint_cmd, format_cmd, "
                        "lint_fix_cmd, test_cmd, language."
                    ),
                },
            },
            "required": ["name", "summary", "tasks"],
        },
    },
}

_ASK_USER_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Ask the user a question to resolve ambiguity. One question at a time. "
            "Include your recommended answer with each question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, with your recommended answer.",
                },
            },
            "required": ["question"],
        },
    },
}


def _tool_name(spec: dict[str, Any]) -> str:
    function = spec.get("function")
    if not isinstance(function, dict):
        raise ValueError("Malformed tool spec: missing function metadata")
    name = function.get("name")
    if not isinstance(name, str):
        raise ValueError("Malformed tool spec: missing function name")
    return name


def get_tool_spec(
    role: Literal["worker", "researcher", "planner"] = "worker",
    interactive: bool = False,
) -> list[Any]:
    specs = [
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
                    "Find lexical occurrences of a symbol across the codebase. "
                    "Use this for quick name hits. Prefer ast_grep for structural search."
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
                "name": "ast_grep",
                "description": (
                    "Structural code search via ast-grep. Use this for syntax-aware matches "
                    "like function defs, imports, calls, or class declarations."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "ast-grep pattern such as 'def $A(): $$$'",
                        },
                        "path": {
                            "type": "string",
                            "description": "File or dir to search (default: '.')",
                            "default": ".",
                        },
                        "lang": {
                            "type": "string",
                            "description": "Optional ast-grep language override, e.g. 'python'",
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
                    "Heuristic import neighborhood for a Python file: what it imports "
                    "from and what imports it. Use as a fallback, not as a semantic truth source."
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
        {
            "type": "function",
            "function": {
                "name": "type_check",
                "description": (
                    "Run the project's type checker (e.g. ty check). "
                    "Returns checker output. Use after edits to verify type correctness."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
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

    if role == "worker":
        return specs
    if role == "researcher":
        return [spec for spec in specs if _tool_name(spec) not in _RESEARCHER_EXCLUDED_TOOLS]
    if role == "planner":
        base = [
            spec
            for spec in specs
            if _tool_name(spec) not in _PLANNER_EXCLUDED_TOOLS and _tool_name(spec) != "done"
        ]
        base.append(_EMIT_PLAN_SPEC)
        if interactive:
            base.append(_ASK_USER_SPEC)
        return base
    raise ValueError(f"Unknown tool role: {role}")


def get_allowed_tool_names(
    role: Literal["worker", "researcher", "planner"] = "worker",
    interactive: bool = False,
) -> frozenset[str]:
    return frozenset(_tool_name(spec) for spec in get_tool_spec(role, interactive=interactive))
