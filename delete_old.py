import os
import subprocess

# Delete the old merger.py
old_file = "/Users/jakegearon/projects/dgov/src/dgov/merger.py"
if os.path.exists(old_file):
    os.remove(old_file)
    print(f"Deleted: {old_file}")

# Verify
result = subprocess.run(
    ["ls", "-la", "/Users/jakegearon/projects/dgov/src/dgov/merger/"],
    capture_output=True,
    text=True
)
print(result.stdout)
