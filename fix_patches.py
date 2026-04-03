#!/usr/bin/env python3
"""Fix test file patch paths."""

import re

path = "/Users/jakegearon/projects/dgov/tests/test_lifecycle.py"
with open(path) as f:
    content = f.read()

# Replace patch targets
replacements = [
    ('"dgov.lifecycle.subprocess.run"', '"dgov.lifecycle._lifecycle.subprocess.run"'),
    ('"dgov.lifecycle.os.killpg"', '"dgov.lifecycle._lifecycle.os.killpg"'),
    ('"dgov.lifecycle.os.getpgid"', '"dgov.lifecycle._lifecycle.os.getpgid"'),
    ('"dgov.lifecycle.os.kill"', '"dgov.lifecycle._lifecycle.os.kill"'),
    ('"dgov.lifecycle._terminate_pane_process_tree"', '"dgov.lifecycle._lifecycle._terminate_pane_process_tree"'),
    ('"dgov.lifecycle.load_registry"', '"dgov.lifecycle._lifecycle.load_registry"'),
    ('"dgov.lifecycle.get_backend"', '"dgov.lifecycle._lifecycle.get_backend"'),
    ('"dgov.lifecycle._setup_and_launch_agent"', '"dgov.lifecycle._lifecycle._setup_and_launch_agent"'),
    ('"dgov.lifecycle._full_cleanup"', '"dgov.lifecycle._lifecycle._full_cleanup"'),
]

for old, new in replacements:
    content = content.replace(old, new)

with open(path, "w") as f:
    f.write(content)

print("Fixed test file patches")
