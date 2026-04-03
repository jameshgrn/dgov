#!/usr/bin/env python3
"""One-shot migration script: rewrite imports from dgov.panes barrel to source modules."""

import re
from pathlib import Path

# Symbol → actual source module (only re-exported symbols, NOT panes-defined)
SYMBOL_MAP = {
    # dgov.batch
    "_compute_tiers": "dgov.batch",
    "create_checkpoint": "dgov.batch",
    "list_checkpoints": "dgov.batch",
    "run_batch": "dgov.batch",
    # dgov.experiment
    "ExperimentLog": "dgov.experiment",
    "run_experiment": "dgov.experiment",
    "run_experiment_loop": "dgov.experiment",
    # dgov.merger
    "_commit_worktree": "dgov.merger",
    "_detect_conflicts": "dgov.merger",
    "_lint_fix_merged_files": "dgov.merger",
    "_pick_resolver_agent": "dgov.merger",
    "_plumbing_merge": "dgov.merger",
    "_resolve_conflicts_with_agent": "dgov.merger",
    "_restore_protected_files": "dgov.merger",
    "merge_worker_pane": "dgov.merger",
    "merge_worker_pane_with_close": "dgov.merger",
    # dgov.openrouter
    "_QWEN_4B_TIMEOUT": "dgov.openrouter",
    "_QWEN_4B_URL": "dgov.openrouter",
    "_qwen_4b_request": "dgov.openrouter",
    "chat_completion": "dgov.openrouter",
    # dgov.persistence
    "_PROTECTED_FILES": "dgov.persistence",
    "_STATE_DIR": "dgov.persistence",
    "PANE_STATES": "dgov.persistence",
    "VALID_EVENTS": "dgov.persistence",
    "VALID_TRANSITIONS": "dgov.persistence",
    "IllegalTransitionError": "dgov.persistence",
    "WorkerPane": "dgov.persistence",
    "_add_pane": "dgov.persistence",
    "_all_panes": "dgov.persistence",
    "_emit_event": "dgov.persistence",
    "_get_pane": "dgov.persistence",
    "_read_state": "dgov.persistence",
    "_remove_pane": "dgov.persistence",
    "_state_path": "dgov.persistence",
    "_update_pane_state": "dgov.persistence",
    "_validate_state": "dgov.persistence",
    "_write_state": "dgov.persistence",
    # dgov.responder
    "BUILT_IN_RULES": "dgov.responder",
    "COOLDOWN_SECONDS": "dgov.responder",
    "ResponseRule": "dgov.responder",
    "auto_respond": "dgov.responder",
    "check_cooldown": "dgov.responder",
    "load_response_rules": "dgov.responder",
    "match_response": "dgov.responder",
    "record_cooldown": "dgov.responder",
    "reset_cooldowns": "dgov.responder",
    # dgov.retry
    "RetryPolicy": "dgov.retry",
    "get_retry_policy": "dgov.retry",
    "maybe_auto_retry": "dgov.retry",
    "retry_context": "dgov.retry",
    # dgov.review_fix
    "ReviewFinding": "dgov.review_fix",
    "parse_review_findings": "dgov.review_fix",
    "run_review_fix_pipeline": "dgov.review_fix",
    # dgov.strategy
    "_SLUG_RE": "dgov.strategy",
    "_generate_slug": "dgov.strategy",
    "_structure_pi_prompt": "dgov.strategy",
    "_validate_slug": "dgov.strategy",
    "classify_task": "dgov.strategy",
    # dgov.templates
    "BUILT_IN_TEMPLATES": "dgov.templates",
    "PromptTemplate": "dgov.templates",
    "list_templates": "dgov.templates",
    "load_templates": "dgov.templates",
    "render_template": "dgov.templates",
    # dgov.waiter
    "_AGENT_COMMANDS": "dgov.waiter",
    "PaneTimeoutError": "dgov.waiter",
    "_agent_still_running": "dgov.waiter",
    "_detect_blocked": "dgov.waiter",
    "_has_new_commits": "dgov.waiter",
    "_is_done": "dgov.waiter",
    "_poll_once": "dgov.waiter",
    "_wrap_done_signal": "dgov.waiter",
    "interact_with_pane": "dgov.waiter",
    "nudge_pane": "dgov.waiter",
    "signal_pane": "dgov.waiter",
    "wait_all_worker_panes": "dgov.waiter",
    "wait_worker_pane": "dgov.waiter",
}


def parse_symbols(raw: str) -> list[str]:
    """Parse comma-separated symbol list, handling parens and newlines."""
    raw = raw.strip().strip("()")
    symbols = []
    for part in raw.replace("\n", ",").split(","):
        s = part.strip()
        if s:
            # Remove any trailing comment
            s = s.split("#")[0].strip()
            if s:
                symbols.append(s)
    return symbols


def build_import(indent: str, module: str, symbols: list[str], suffix: str = "") -> str:
    """Build a from-import line."""
    sym_str = ", ".join(sorted(symbols))
    return f"{indent}from {module} import {sym_str}{suffix}"


def rewrite_import(line: str, next_lines: list[str]) -> tuple[list[str], int]:
    """Rewrite a 'from dgov.panes import ...' statement.

    Returns (new_lines, num_consumed_from_next_lines).
    """
    # Match single-line: from dgov.panes import X, Y
    m = re.match(r"^(\s*)from dgov\.panes import (.+)$", line)
    if m:
        indent = m.group(1)
        raw = m.group(2)

        # Check if it ends with ( - multi-line continuation
        if raw.rstrip().endswith("("):
            return [line], 0  # Will be handled by multi-line logic

        symbols = parse_symbols(raw)
        return _rewrite_symbols(indent, symbols)

    return [line], 0


def _rewrite_symbols(indent: str, symbols: list[str]) -> list[str]:
    """Given parsed symbols, produce the right import lines."""
    if not symbols:
        return []

    # Separate into panes-defined vs re-exported
    panes_syms = [s for s in symbols if s not in SYMBOL_MAP]
    reexport_syms = [s for s in symbols if s in SYMBOL_MAP]

    # Group re-exported symbols by target module
    groups: dict[str, list[str]] = {}
    for sym in reexport_syms:
        mod = SYMBOL_MAP[sym]
        groups.setdefault(mod, []).append(sym)

    result = []
    for mod in sorted(groups.keys()):
        result.append(build_import(indent, mod, groups[mod]))

    if panes_syms:
        result.append(build_import(indent, "dgov.panes", panes_syms))

    return result if result else [build_import(indent, "dgov.panes", symbols)]


def process_file(path: Path) -> bool:
    """Process a single file. Returns True if modified."""
    content = path.read_text()
    lines = content.split("\n")
    new_lines = []
    modified = False
    i = 0

    while i < len(lines):
        line = lines[i]

        # Check for multi-line import: from dgov.panes import (
        m = re.match(r"^(\s*)from dgov\.panes import \($", line.strip())
        if m is None:
            m = re.match(r"^(\s*)from dgov\.panes import \(.*$", line)
        if m and "from dgov.panes import" in line and "(" in line:
            indent = m.group(1)
            # Collect lines until closing paren
            raw_parts = []
            # Get symbols on same line after (
            after_paren = line.split("import (")[1] if "import (" in line else ""
            if after_paren.strip().rstrip(")"):
                raw_parts.append(after_paren)

            j = i + 1
            while j < len(lines):
                if ")" in lines[j]:
                    before_close = lines[j].split(")")[0]
                    if before_close.strip():
                        raw_parts.append(before_close)
                    break
                raw_parts.append(lines[j])
                j += 1

            raw = " ".join(raw_parts)
            symbols = parse_symbols(raw)
            new_imports = _rewrite_symbols(indent, symbols)

            if new_imports:
                new_lines.extend(new_imports)
                modified = True
            else:
                # Keep original
                for k in range(i, j + 1):
                    new_lines.append(lines[k])

            i = j + 1
            continue

        # Single-line import
        if re.match(r"^\s*from dgov\.panes import ", line) and not line.strip().startswith("#"):
            m = re.match(r"^(\s*)from dgov\.panes import (.+)$", line)
            if m:
                indent = m.group(1)
                symbols = parse_symbols(m.group(2))
                new_imports = _rewrite_symbols(indent, symbols)
                if new_imports != [line.rstrip()]:
                    new_lines.extend(new_imports)
                    modified = True
                else:
                    new_lines.append(line)
            else:
                new_lines.append(line)
            i += 1
            continue

        new_lines.append(line)
        i += 1

    if modified:
        path.write_text("\n".join(new_lines))
        return True
    return False


def main():
    root = Path("/Users/jakegearon/projects/dgov/.dgov/worktrees/split-barrel")
    files = []

    for p in (root / "src" / "dgov").rglob("*.py"):
        if "from dgov.panes import" in p.read_text():
            files.append(p)
    for p in (root / "tests").rglob("*.py"):
        if "from dgov.panes import" in p.read_text():
            files.append(p)

    print(f"Found {len(files)} files with dgov.panes imports:")
    for f in files:
        print(f"  {f.relative_to(root)}")

    modified_count = 0
    for f in files:
        if process_file(f):
            print(f"  ✓ Modified: {f.relative_to(root)}")
            modified_count += 1
        else:
            print(f"  ○ No change: {f.relative_to(root)}")

    print(f"\nModified {modified_count} files")


if __name__ == "__main__":
    main()
