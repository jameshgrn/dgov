"""Simple pipe test - no asyncio."""
import os
import sys
from pathlib import Path
import threading
import time

pipe_path = Path(".") / ".dgov" / "simple.pipe"
pipe_path.parent.mkdir(parents=True, exist_ok=True)
if pipe_path.exists():
    pipe_path.unlink()

os.mkfifo(str(pipe_path))
print(f"Pipe: {pipe_path}")

result = [None]

def reader():
    print("Reader: opening...")
    with open(str(pipe_path), "r") as f:
        print("Reader: reading...")
        line = f.readline().strip()
        print(f"Reader: got: {line}")
        result[0] = line

# Start reader in thread
print("Starting reader thread...")
thread = threading.Thread(target=reader)
thread.start()

# Give reader time to start blocking
time.sleep(0.5)

# Write
print("Writing...")
os.system(f'echo simple-test > "{pipe_path}"')
print("Write done")

# Wait for reader
thread.join(timeout=3)
if thread.is_alive():
    print("READER STILL ALIVE - timeout")
else:
    print(f"SUCCESS: {result[0]}")
