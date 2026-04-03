import os
import sys
import subprocess

# Change to the correct directory
os.chdir('/Users/jakegearon/projects/dgov')

# Test the imports
print("Testing imports...")

try:
    from dgov.merger.validate import _check_merge_preconditions
    print("✓ validate.py import works")
except Exception as e:
    print(f"✗ validate.py import failed: {e}")
    sys.exit(1)

try:
    from dgov.merger import (
        merge_worker_pane,
        MergeSuccess,
        MergeError,
        MergeConflict,
        ConflictResolveStrategy,
    )
    print("✓ merger public API works")
except Exception as e:
    print(f"✗ merger public API failed: {e}")
    sys.exit(1)

print("\nAll import tests passed!")
