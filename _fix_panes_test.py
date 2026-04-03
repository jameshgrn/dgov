#!/usr/bin/env python3
"""Fix patches in test_dgov_panes.py for removed re-exports."""

import re
from pathlib import Path

# Symbols that are still defined in panes.py (patch target stays the same)
PANES_DEFINED = {
    "_remove_worktree",
    "_full_cleanup",
    "_trigger_hook",
    "capture_worker_output",
    "close_worker_pane",
    "create_worker_pane",
    "retry_worker_pane",
    "escalate_worker_pane",
    "list_worker_panes",
    "prune_stale_panes",
    "_project_root",
    "_repo",
}

# Symbols moved to other modules
MOVED = {
    "_update_pane_state": "dgov.persistence",
    "_is_done": "dgov.waiter",
    "_get_pane": "dgov.persistence",
    "_emit_event": "dgov.persistence",
    "_structure_pi_prompt": "dgov.strategy",
    "_generate_slug": "dgov.strategy",
    "_validate_slug": "dgov.strategy",
    "classify_task": "dgov.strategy",
    "load_registry": "dgov.agents",
    "build_launch_command": "dgov.agents",
}


def process_file(path: Path) -> int:
    content = path.read_text()
    count = 0
    lines = content.split("\n")
    new_lines = []

    for line in lines:
        new_line = line
        for sym, target_mod in MOVED.items():
            # Only redirect if it's not a panes-defined symbol
            if sym in PANES_DEFINED:
                continue
            old = f"dgov.panes.{sym}"
            if old in new_line and "patch(" in new_line:
                new_line = new_line.replace(old, f"{target_mod}.{sym}")
                count += 1
        new_lines.append(new_line)

    if count > 0:
        path.write_text("\n".join(new_lines))
    return count


def main():
    root = Path("/Users/jakegearon/projects/dgov/.dgov/worktrees/split-barrel")
    path = root / "tests" / "test_dgov_panes.py"
    c = process_file(path)
    print(f"test_dgov_panes.py: {c} patches updated")


if __name__ == "__main__":
    main()
