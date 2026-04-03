"""Verify pane_executor.py has correct exports."""

import ast
import sys

def check_exports(filepath):
    """Check that all required exports are present."""
    with open(filepath) as f:
        tree = ast.parse(f.read())

    required = {
        'run_complete_pane': False,
        'run_fail_pane': False,
        'run_mark_reviewed': False,
        'run_cleanup_only': False,
        'run_close_only': False,
        'run_worker_checkpoint': False,
        'CleanupAction': False,
        'CleanupOnlyResult': False,
        'StateTransitionResult': False,
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            if node.name in required:
                required[node.name] = True
        elif isinstance(node, ast.ClassDef):
            if node.name in required:
                required[node.name] = True

    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"Missing exports in {filepath}: {missing}")
        return False

    print(f"All required exports found in {filepath}")
    return True

if __name__ == "__main__":
    import os
    path = "/Users/jakegearon/projects/dgov/src/dgov/pane_executor.py"
    if not os.path.exists(path):
        print(f"File not found: {path}")
        sys.exit(1)

    success = check_exports(path)
    sys.exit(0 if success else 1)
