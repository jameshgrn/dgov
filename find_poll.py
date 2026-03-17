import sys
with open('/Users/jakegearon/projects/dgov/tests/test_dgov_panes.py') as f:
    lines = f.readlines()
    for i, line in enumerate(lines):
        if '_poll_once' in line and 'def _poll_once' not in line:
            start = max(0, i-5)
            end = min(len(lines), i+15)
            print(f"\n=== Line {i+1} ===")
            for j in range(start, end):
                marker = ">>> " if j == i else "    "
                print(f"{marker}{j+1}: {lines[j].rstrip()}")