#!/usr/bin/env python3
"""Fix merger.py: replace _p.X with direct imports."""

import re
from pathlib import Path

# Functions defined in panes.py (stays as dgov.panes)
PANES_FUNCS = {
    "create_worker_pane",
    "capture_worker_output",
    "close_worker_pane",
    "_trigger_hook",
    "_full_cleanup",
    "list_worker_panes",
}

# Functions from persistence
PERSISTENCE_FUNCS = {
    "_get_pane",
    "_update_pane_state",
    "IllegalTransitionError",
    "_emit_event",
    "_state_path",
}

# Functions from waiter
WAITER_FUNCS = {
    "_is_done",
}

# Same module (merger) — call directly
MERGER_FUNCS = {
    "_pick_resolver_agent",
    "_commit_worktree",
    "_restore_protected_files",
    "_plumbing_merge",
    "_lint_fix_merged_files",
    "_detect_conflicts",
    "_resolve_conflicts_with_agent",
}

ALL_REDIRECTS = {}
for f in PANES_FUNCS:
    ALL_REDIRECTS[f] = "dgov.panes"
for f in PERSISTENCE_FUNCS:
    ALL_REDIRECTS[f] = "dgov.persistence"
for f in WAITER_FUNCS:
    ALL_REDIRECTS[f] = "dgov.waiter"
for f in MERGER_FUNCS:
    ALL_REDIRECTS[f] = None  # same module, no import needed


def process_file(path: Path):
    content = path.read_text()
    lines = content.split("\n")
    new_lines = []

    for line in lines:
        if "import dgov.panes as _p" in line:
            # Remove this line
            continue
        if "_p." in line:
            # Replace all _p.X references
            for func, mod in ALL_REDIRECTS.items():
                line = line.replace(f"_p.{func}(", f"{func}(")
                line = line.replace(f"_p.{func}.", f"{func}.")  # for _p.IllegalTransitionError
        new_lines.append(line)

    path.write_text("\n".join(new_lines))


def main():
    root = Path("/Users/jakegearon/projects/dgov/.dgov/worktrees/split-barrel")
    merger = root / "src" / "dgov" / "merger.py"
    process_file(merger)
    print(f"Processed {merger}")

    # Add imports at the top of functions that need them
    content = merger.read_text()

    # Find functions that use persistence/waiter/panes functions and add imports
    # Strategy: add imports at each of the 3 original locations where `import dgov.panes as _p` was

    # Actually, let me just do a simpler approach: add imports at the function level
    # For each function, find what it needs and add the import

    print("Done. Run ruff to verify.")


if __name__ == "__main__":
    main()
