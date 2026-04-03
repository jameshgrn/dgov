#!/usr/bin/env python3
"""Fix old _poll_once call signatures in test_dgov_panes.py"""
import re

with open('/Users/jakegearon/projects/dgov/tests/test_dgov_panes.py', 'r') as f:
    content = f.read()

# Pattern 1: Tests with old signature that unpack to (done, _, method) etc.
# Look for _poll_once calls with None,None,20 or None,None,15 patterns

old_patterns = [
    # Pattern: done, _, _, _, blocked = _poll_once(..., None, None, 15, ...)
    (r'done, _, _, _, blocked = _poll_once\(\s*(str\(tmp_path\),\s*){3}pane_record,\s*None,\s*None,\s*15,', 
     'stable_state: dict = {}\n            done, method = _poll_once(\n                str(tmp_path),\n                str(tmp_path),\n                "s1",\n                pane_record,\n                stable_state,\n                15,'),
    
    # Pattern: done, _, method = _poll_once(..., None, None, 20, ...)  
    (r'done, _, method = _poll_once\(\s*(str\(tmp_path\),\s*){3}pane_record,\s*None,\s*None,\s*20,',
     'stable_state: dict = {}\n            done, method = _poll_once(\n                str(tmp_path),\n                str(tmp_path),\n                "s1",\n                pane_record,\n                stable_state,\n                20,'),
]

# More specific approach - find lines with _poll_once and fix them
lines = content.split('\n')
new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    
    # Check if this is a _poll_once call (not def)
    if '_poll_once(' in line and 'def _poll_once' not in line:
        # Look for patterns with None, None followed by numeric value
        # This is likely old signature with last_output, stable_since, etc.
        
        # Check next few lines to see the pattern
        context = '\n'.join(lines[i:min(i+20, len(lines))])
        
        if ', None,\s*None,\s*15' in re.sub(r'\s+', ' ', context) or \
           ', None,\s*None,\s*20' in re.sub(r'\s+', ' ', context):
            # This is old signature - we need to add stable_state declaration before
            if 'stable_state: dict = {}' not in context[:50]:
                new_lines.append('            stable_state: dict = {}')
    
    new_lines.append(line)
    i += 1

with open('/Users/jakegearon/projects/dgov/tests/test_dgov_panes.py', 'w') as f:
    f.write('\n'.join(new_lines))

print("Done adding stable_state declarations")