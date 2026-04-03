#!/usr/bin/env python3
import subprocess
import sys

# Test the import
try:
    from dgov.lifecycle import create_worker_pane
    print("OK - Import test passed")
except Exception as e:
    print(f"FAIL - Import error: {e}")
    sys.exit(1)

# Run ruff check
result = subprocess.run(
    ["uv", "run", "ruff", "check", "src/dgov/lifecycle/create.py"],
    cwd="/Users/jakegearon/projects/dgov",
    capture_output=True,
    text=True
)
print(f"Ruff check stdout: {result.stdout}")
print(f"Ruff check stderr: {result.stderr}")
print(f"Ruff check return code: {result.returncode}")

# Run ruff format
result = subprocess.run(
    ["uv", "run", "ruff", "format", "src/dgov/lifecycle/create.py"],
    cwd="/Users/jakegearon/projects/dgov",
    capture_output=True,
    text=True
)
print(f"Ruff format stdout: {result.stdout}")
print(f"Ruff format stderr: {result.stderr}")
print(f"Ruff format return code: {result.returncode}")
