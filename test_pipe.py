"""Test event pipe."""
import asyncio
import sys
sys.path.insert(0, "src")

print("Step 1: imports...")
from pathlib import Path

print("Step 2: setup pipe...")
from dgov.runner import EventDagRunner
from dgov.plan import parse_plan_file, compile_plan

plan = parse_plan_file(".dgov/test-mock.toml")
dag = compile_plan(plan)
runner = EventDagRunner(dag, session_root=".")

pipe_path = runner._setup_event_pipe()
print(f"Pipe created at: {pipe_path}")
print(f"Pipe exists: {pipe_path.exists()}")

print("Step 3: read task started...")
# Check if read task is running
import asyncio
print(f"All tasks: {asyncio.all_tasks()}")

print("Step 4: simulate write...")
import subprocess
signal_cmd = f'echo \'{{"task_slug": "mock-test", "pane_slug": "test", "exit_code": 0}}\' > "{pipe_path}"'
print(f"Running: {signal_cmd}")

# Do the write in background
proc = subprocess.Popen(
    ["bash", "-c", signal_cmd],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
)
print(f"Spawned PID: {proc.pid}")

print("Step 5: wait for queue...")
async def wait_for_event():
    try:
        event = await asyncio.wait_for(runner._event_queue.get(), timeout=3)
        print(f"Got event: {event}")
    except asyncio.TimeoutError:
        print("TIMEOUT waiting for event")
        stderr = proc.stderr.read()
        print(f"Subprocess stderr: {stderr}")

asyncio.run(wait_for_event())
print("Done")
