#!/usr/bin/env python3
"""Find all _poll_once calls with old signature."""
lines = open('/Users/jakegearon/projects/dgov/tests/test_dgov_panes.py').readlines()
for i, line in enumerate(lines):
    if '_poll_once' in line and 'def _poll_once' not in line:
        print(f"\n=== Line {i+1} ===")
        for j in range(max(0, i-2), min(len(lines), i+10)):
            prefix = ">>>" if j == i else "   "
            print(f"{prefix} {j+1}: {lines[j].rstrip()}")