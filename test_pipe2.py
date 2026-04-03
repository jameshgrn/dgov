"""Test pipe directly with debug."""
import asyncio
import os
import sys
from pathlib import Path
import subprocess

async def test():
    pipe_path = Path(".") / ".dgov" / "debug.pipe"
    pipe_path.parent.mkdir(parents=True, exist_ok=True)
    if pipe_path.exists():
        pipe_path.unlink()
    
    os.mkfifo(str(pipe_path))
    print(f"Pipe created: {pipe_path}")
    
    def read_pipe():
        print("Reader: opening pipe (blocking)...")
        with open(str(pipe_path), "r") as f:
            print("Reader: opened, reading...")
            line = f.readline().strip()
            print(f"Reader: read: '{line}'")
            return line
    
    import concurrent.futures
    loop = asyncio.get_event_loop()
    
    with concurrent.futures.ThreadPoolExecutor() as executor:
        print("Submitting reader to executor...")
        future = loop.run_in_executor(executor, read_pipe)
        
        print("Waiting 0.5s for reader to start blocking...")
        await asyncio.sleep(0.5)
        
        print("Writing to pipe...")
        event_data = '{"task_slug": "test", "pane_slug": "p1", "exit_code": 0}'
        cmd = f"echo '{event_data}' > '{pipe_path}'"
        subprocess.run(["bash", "-c", cmd], check=True)
        print("Write complete")
        
        print("Waiting for reader result...")
        try:
            result = await asyncio.wait_for(future, timeout=3)
            print(f"SUCCESS: {result}")
        except asyncio.TimeoutError:
            print("TIMEOUT")

if __name__ == "__main__":
    asyncio.run(test())
