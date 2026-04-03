"""Test pipe threading."""
import asyncio
import sys
sys.path.insert(0, "src")
from pathlib import Path
import os

async def main():
    pipe_path = Path(".") / ".dgov" / "test.pipe"
    pipe_path.parent.mkdir(parents=True, exist_ok=True)
    if pipe_path.exists():
        pipe_path.unlink()
    
    os.mkfifo(str(pipe_path))
    print(f"Pipe: {pipe_path}")
    
    # Use asyncio.to_thread to open in background
    print("Starting reader in thread...")
    
    def read_from_pipe():
        print("Thread: opening pipe for reading...")
        with open(str(pipe_path), "r") as f:
            print("Thread: opened, reading line...")
            line = f.readline().strip()
            print(f"Thread: read: {line}")
            return line
    
    # Start reader in thread
    reader_future = asyncio.to_thread(read_from_pipe)
    print("Reader started in thread, waiting...")
    
    # Give it time to start blocking
    await asyncio.sleep(0.5)
    
    # Now write
    print("Writing to pipe...")
    import subprocess
    signal_cmd = f'echo test-message > "{pipe_path}"'
    subprocess.run(["bash", "-c", signal_cmd], check=True)
    print("Write complete")
    
    # Wait for reader
    print("Waiting for reader result...")
    try:
        result = await asyncio.wait_for(reader_future, timeout=3)
        print(f"Success: {result}")
    except asyncio.TimeoutError:
        print("TIMEOUT")

if __name__ == "__main__":
    asyncio.run(main())
