"""Test with debug."""
import asyncio
import sys
sys.path.insert(0, "src")

from dgov.plan import parse_plan_file, compile_plan
from dgov.runner import EventDagRunner
from pathlib import Path
import subprocess

async def main():
    plan = parse_plan_file(".dgov/test-mock.toml")
    dag = compile_plan(plan)
    
    print(f"DAG: {dag.name}, tasks: {list(dag.tasks.keys())}")
    
    runner = EventDagRunner(dag, session_root=".")
    
    # Manually test pipe
    print("\n=== Manual pipe test ===")
    pipe_path = Path(".") / ".dgov" / "event.pipe"
    pipe_path.parent.mkdir(parents=True, exist_ok=True)
    if pipe_path.exists():
        pipe_path.unlink()
    
    import os
    os.mkfifo(str(pipe_path))
    print(f"Pipe created: {pipe_path}")
    
    # Start reader
    queue = asyncio.Queue()
    
    async def reader():
        print("Reader: opening pipe...")
        with open(str(pipe_path), "r") as f:
            print("Reader: pipe opened, waiting for line...")
            line = f.readline().strip()
            print(f"Reader: got line: {line}")
            await queue.put(line)
    
    reader_task = asyncio.create_task(reader())
    
    # Wait a bit then write
    await asyncio.sleep(0.5)
    print("Writer: writing to pipe...")
    signal_cmd = f'echo \'{{"task_slug": "mock-test", "pane_slug": "test", "exit_code": 0}}\' > "{pipe_path}"'
    proc = subprocess.Popen(["bash", "-c", signal_cmd])
    
    # Wait for reader
    try:
        result = await asyncio.wait_for(queue.get(), timeout=3)
        print(f"Got result: {result}")
    except asyncio.TimeoutError:
        print("TIMEOUT")
    
    reader_task.cancel()
    try:
        await reader_task
    except asyncio.CancelledError:
        pass
    
    print("\n=== Now test full runner ===")
    runner2 = EventDagRunner(dag, session_root=".")
    try:
        results = await asyncio.wait_for(runner2.run(), timeout=5)
        print(f"Results: {results}")
    except asyncio.TimeoutError:
        print("RUNNER TIMEOUT")
        print(f"Pending: {runner2._pending_dispatches}")

if __name__ == "__main__":
    asyncio.run(main())
