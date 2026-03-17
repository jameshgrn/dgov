#!/usr/bin/env python3
"""Fix all _poll_once calls in test_dgov_panes.py to use new signature."""

with open('/Users/jakegearon/projects/dgov/tests/test_dgov_panes.py', 'r') as f:
    lines = f.readlines()

new_lines = []
i = 0
while i < len(lines):
    line = lines[i]
    
    # Check if this is the start of a _poll_once call (not def)
    if '_poll_once(' in line and 'def _poll_once' not in line:
        # This looks like an old-style call with None, None pattern
        # Add stable_state declaration before it if not already present
        
        # Look back up to 3 lines to see if we already have stable_state
        has_stable = False
        for j in range(max(0, i-4), i):
            if 'stable_state: dict = {}' in lines[j]:
                has_stable = True
                break
        
        # Look ahead 15 lines to see the signature pattern  
        context_end = min(i + 15, len(lines))
        context = ''.join(lines[i:context_end])
        
        if ', None,' in context and ', alive=' in context:
            # Old signature detected - add stable_state before this line if not present
            if not has_stable:
                indent = '            '
                new_lines.append(f'{indent}stable_state: dict = {}\n')
            
            # Also need to change the return unpacking pattern
            # Change things like "done, _, method" or "done, _, _, _, blocked"
            # to just "done, method"
            
            # Check if current line has complex unpacking
            if ', _' in line or '_, _, _' in line:
                # Extract the variable names
                var_part = line.split('=', 1)[0].strip()
                # Replace with simple unpacking
                new_line = line.replace(var_part, 'done, method')
                new_lines.append(new_line)
                
                # Also need to remove references to old vars in following lines
                # e.g., "assert blocked == ..." should become "assert method == ..."
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    else:
        # Check if this line tries to use an old var that no longer exists
        if ' assert blocked ==' in line:
            # Change to assert method == ""
            line = line.replace('assert blocked == "Enter password"', 
                               'assert method == ""')
        elif 'mock_emit.assert_called_once_with' in line and 'blocked' not in lines[i-2:i][0] if i >= 2 else True:
            # Don't remove this emit event check since we still want to test blocked detection
            pass
        
        new_lines.append(line)
    
    i += 1

with open('/Users/jakegearon/projects/dgov/tests/test_dgov_panes.py', 'w') as f:
    f.writelines(new_lines)

print("Fixed test file")