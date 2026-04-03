"""Test with concurrent.futures."""
import asyncio
import sys
from pathlib import Path
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

async def main():
    pipe_path = Path(".") / ".dgov" / "executor.pipe"
    pipe_path.parent.mkdir(parents=True, exist_ok=True)
    if pipe_path.exists():
        pipe_path.unlink()
    
    os.mkfifo(str(pipe_path))
    print(f"Pipe: {pipe_path}")
    
    def read_from_pipe():
        print("Thread: opening pipe...")
        with open(str(pipe_path), "r") as f:
            print("Thread: reading...")
            return f.readline().strip()
    
    # Use ThreadPoolExecutor via asyncio
    with ThreadPoolExecutor(max_workers=1) as executor:
        loop = asyncio.get_event_loop()
        print("Submitting reader to executor...")
        future = loop.run_in_executor(executor, read_from_pipe)
        
        # Wait a bit
        await asyncio.sleep(0.5)
        
        # Write
        print("Writing...")
        subprocess.run(["bash", "-c", f'echo executor-test > "{pipe_path}"'])
        
        # Wait for result
        print("Waiting for result...")
        try:
            result = await asyncio.wait_for(future, timeout=3)
            print(f"SUCCESS: {result}")
        except asyncio.TimeoutError:
            print("TIMEOUT")

if __name__ == "__main__":
    asyncio.run(main())
