#!/usr/bin/env python3
"""Debug tmux pane creation and send_keys."""

import subprocess
import time
from dgov.tmux import create_background_pane, ensure_session, kill_pane, pane_exists
from dgov.tmux import send_keys_with_session, capture_pane

# Create test session
ensure_session("test-debug")
print("Session created")

# Create pane
pane_id = create_background_pane(name="test-pane", cwd="/tmp", target_session="test-debug")
print(f"Pane created: {pane_id}")

# Wait for shell
time.sleep(0.5)

# Check pane content
content = capture_pane(pane_id)
print(f"Initial pane content: {content[:100] if content else 'empty'}")

# Send command
result = send_keys_with_session(pane_id, "echo HELLO", "test-debug")
print(f"send_keys result: {result.returncode if result else 'None'}")

result = send_keys_with_session(pane_id, "Enter", "test-debug")
print(f"Enter result: {result.returncode if result else 'None'}")

# Wait
time.sleep(1)

# Check content
content = capture_pane(pane_id)
print(f"After send_keys (last 200 chars): {content[-200:] if content else 'empty'}")

# Cleanup
kill_pane(pane_id)
subprocess.run(["tmux", "kill-session", "-t", "test-debug"])
print("Done")
