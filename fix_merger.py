#!/usr/bin/env python3
"""Script to remove duplicate functions from merger.py"""

import re

filepath = "/Users/jakegearon/projects/dgov/src/dgov/merger.py"

with open(filepath, "r") as f:
    content = f.read()

# Pattern to match _stash_and_rebase function
pattern = r'\ndef _stash_and_rebase\([^)]+\)[^:]+:\s*"""[^"]*"""[^}]*logger\.info\([^)]+\)\s+return \(MergeResult\(success=True\), current_branch\)\n\n\n'

# Actually let me try a simpler pattern - match from def to the next def
def find_and_remove_function(content, func_name, next_func_name):
    """Remove a function definition from content"""
    start_marker = f"\ndef {func_name}("
    end_marker = f"\ndef {next_func_name}("

    start_idx = content.find(start_marker)
    if start_idx == -1:
        print(f"Could not find {func_name}")
        return content

    end_idx = content.find(end_marker, start_idx + len(start_marker))
    if end_idx == -1:
        print(f"Could not find {next_func_name}")
        return content

    # Remove the function, but keep one newline before the next function
    return content[:start_idx] + "\n" + content[end_idx:]

# Remove _stash_and_rebase
content = find_and_remove_function(content, "_stash_and_rebase", "_rebase_onto_head")

with open(filepath, "w") as f:
    f.write(content)

print("Done!")
