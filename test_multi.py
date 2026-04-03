"""Test pipe with multiple writers."""
import os
import json
import threading
import time
from pathlib import Path

pipe_path = Path(".") / ".dgov" / "test_multi.pipe"
pipe_path.parent.mkdir(parents=True, exist_ok=True)
if pipe_path.exists():
    pipe_path.unlink()
os.mkfifo(str(pipe_path))

received = []
stop = threading.Event()

def reader():
    print("Reader: opening...")
    fd = os.open(str(pipe_path), os.O_RDONLY)
    print(f"Reader: opened fd {fd}")
    
    while not stop.is_set():
        import select
        ready, _, _ = select.select([fd], [], [], 0.5)
        if not ready:
            continue
        
        try:
            data = os.read(fd, 4096).decode()
            print(f"Reader: read {len(data)} bytes: {data!r}")
            if data:
                received.append(data.strip())
        except:
            break
    
    os.close(fd)
    print("Reader: closed")

# Start reader
thread = threading.Thread(target=reader)
thread.start()
time.sleep(0.3)

# Writer 1
print("Writer 1: writing...")
with open(str(pipe_path), "w") as f:
    f.write(json.dumps({"msg": 1}) + "\n")
print("Writer 1: done")

time.sleep(0.5)
print(f"Received after w1: {received}")

# Writer 2 (new connection)
print("Writer 2: writing...")
with open(str(pipe_path), "w") as f:
    f.write(json.dumps({"msg": 2}) + "\n")
print("Writer 2: done")

time.sleep(0.5)
print(f"Received after w2: {received}")

# Cleanup
stop.set()
thread.join(timeout=2)
pipe_path.unlink()
print("Done")
