#!/usr/bin/env python3
import subprocess
import os

os.chdir('/Users/jakegearon/projects/dgov')

# Add the files
subprocess.run(['git', 'add', 'src/dgov/cli/diagnostics.py', 'tests/test_init_doctor.py'], check=True)

# Check status
result = subprocess.run(['git', 'status'], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)

# Commit
result = subprocess.run(['git', 'commit', '-m', 'refactor(cli): extract diagnostics.py from admin.py'], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)
