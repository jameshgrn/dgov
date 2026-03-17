#!/usr/bin/env python3
"""Find all _poll_once calls and their context in the test file."""

lines = []
with open('/Users/jakegearon/projects/dgov/tests/test_dgov_panes.py') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if '_poll_once' in line and 'def _poll_once' not in line:
        print(f"\n=== Line {i+1}: {line.rstrip()} ===")
        # Show surrounding context
        for j in range(max(0, i-3), min(len(lines), i+8)):
            prefix = ">>> " if j == i else "    "
            print(f"{prefix}{j+1:4d}: {lines[j].rstrip()}")