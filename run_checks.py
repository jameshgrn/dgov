#!/usr/bin/env python3
import subprocess
import sys

worktree = "/Users/jakegearon/.dgov/worktrees/1bac54f15c74/r221-coverage-push-a2"

# Run ruff check
result = subprocess.run(
    ["uv", "run", "ruff", "check", 
     "src/dgov/persistence.py", "src/dgov/spans.py", 
     "tests/test_persistence_pane.py", "tests/test_spans.py"],
    cwd=worktree,
    capture_output=True,
    text=True
)
print("Ruff check output:")
print(result.stdout)
print(result.stderr)
if result.returncode != 0:
    print(f"Ruff check failed with code {result.returncode}")
    sys.exit(1)

# Run ruff format
result = subprocess.run(
    ["uv", "run", "ruff", "format",
     "src/dgov/persistence.py", "src/dgov/spans.py",
     "tests/test_persistence_pane.py", "tests/test_spans.py"],
    cwd=worktree,
    capture_output=True,
    text=True
)
print("\nRuff format output:")
print(result.stdout)
print(result.stderr)

# Run pytest
result = subprocess.run(
    ["uv", "run", "pytest", "tests/test_persistence_pane.py", "tests/test_spans.py", "-q", "-m", "unit"],
    cwd=worktree,
    capture_output=True,
    text=True
)
print("\nPytest output:")
print(result.stdout)
print(result.stderr)
if result.returncode != 0:
    print(f"Pytest failed with code {result.returncode}")
    sys.exit(1)

print("\nAll checks passed!")
