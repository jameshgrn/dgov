#!/usr/bin/env python3
import subprocess
import sys
import os

os.chdir('/Users/jakegearon/projects/dgov')

# Add src to path for verification
sys.path.insert(0, 'src')

# E1: Verify kernel compiles and runs
print("=== E1: Testing kernel compiles and runs ===")
from dgov.kernel import DagKernel, DagState
k = DagKernel(deps={"a": ()})
actions = k.start()
assert len(actions) == 1, f"Expected 1 action, got {len(actions)}"
print(f"E1 PASS: {len(actions)} actions emitted")

# E3: Verify no RetryTask in kernel.py
print("")
print("=== E3: Checking RetryTask removed ===")
with open('src/dgov/kernel.py', 'r') as f:
    content = f.read()
    if 'RetryTask' in content:
        print("E3 FAIL: RetryTask still found in kernel.py")
        sys.exit(1)
    else:
        print("E3 PASS: No RetryTask in kernel.py")

# E4: Verify max_retries defaults to 0
print("")
print("=== E4: Checking max_retries defaults to 0 ===")
if 'max_retries: int = 0' in content:
    print("E4 PASS: max_retries defaults to 0")
else:
    print("E4 FAIL: max_retries does not default to 0")
    sys.exit(1)

print("")
print("=== All evals passed! ===")

# Stage and commit
print("")
print("=== Staging changes ===")
subprocess.run(['git', 'add', 'src/dgov/kernel.py', 'tests/test_kernel.py'], check=True)
print("Changes staged")

print("")
print("=== Committing ===")
result = subprocess.run(
    ['git', 'commit', '-m', 'Remove auto-retry from kernel - failures go to BLOCKED_ON_GOVERNOR'],
    capture_output=True,
    text=True
)
print(result.stdout)
if result.returncode != 0:
    print(result.stderr)
    sys.exit(1)

print("")
print("=== Commit successful! ===")
