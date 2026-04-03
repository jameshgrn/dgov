"""Debug the pipe issue."""
import sys
sys.path.insert(0, "src")

import os
import json
import threading
import time
from pathlib import Path

# Create pipe
pipe_path = Path(".") / ".dgov" / "debug.pipe"
pipe_path.parent.mkdir(parents=True, exist_ok=True)
if pipe_path.exists():
    pipe_path.unlink()
os.mkfifo(str(pipe_path))
print(f"Pipe created: {pipe_path}")

# Track received events
events = []
stop_event = threading.Event()

def reader():
    print("Reader: opening pipe...")
    with open(str(pipe_path), "r") as f:
        print("Reader: pipe opened")
        import select
        while not stop_event.is_set():
            ready, _, _ = select.select([f], [], [], 0.5)
            if not ready:
                print("Reader: timeout, looping...")
                continue
            line = f.readline().strip()
            if line:
                print(f"Reader: got: {line}")
                events.append(line)
            else:
                print("Reader: empty line")

# Start reader
thread = threading.Thread(target=reader)
thread.start()
time.sleep(0.5)  # Let reader start

# Write event
print("Writer: writing...")
event = json.dumps({"task_slug": "test", "pane_slug": "p1", "exit_code": 0})
with open(str(pipe_path), "w") as f:
    f.write(event + "\n")
print("Writer: done")

# Wait for event
time.sleep(1)
print(f"Events received: {events}")

# Cleanup
stop_event.set()
thread.join(timeout=2)
pipe_path.unlink()
print("Done")
