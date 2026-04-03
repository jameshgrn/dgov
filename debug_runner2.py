"""Debug runner step by step."""
import sys
sys.path.insert(0, "src")

import asyncio
import os
import json
from pathlib import Path

from dgov.plan import parse_plan_file, compile_plan
from dgov.runner import EventDagRunner

async def main():
    print("=== Parse plan ===")
    plan = parse_plan_file(".dgov/test-mock.toml")
    dag = compile_plan(plan)
    print(f"DAG: {dag.name}, tasks: {list(dag.tasks.keys())}")
    
    print("\n=== Create runner ===")
    runner = EventDagRunner(dag, session_root=".")
    print(f"Kernel states: {runner.kernel.task_states}")
    print(f"Kernel done: {runner.kernel.done}")
    
    print("\n=== Setup pipe ===")
    pipe_path = await runner._setup_event_pipe()
    print(f"Pipe: {pipe_path}, exists: {pipe_path.exists()}")
    
    print("\n=== Start kernel ===")
    actions = runner.kernel.start()
    print(f"Initial actions: {actions}")
    print(f"Kernel states after start: {runner.kernel.task_states}")
    print(f"Kernel done: {runner.kernel.done}")
    
    if not actions:
        print("No actions - checking if done")
        return
    
    print("\n=== Dispatch first action ===")
    from dgov.kernel import DispatchTask
    for action in actions:
        if isinstance(action, DispatchTask):
            print(f"Dispatching: {action.task_slug}")
            await runner._dispatch(action, pipe_path)
            print(f"Pending: {runner._pending_dispatches}")
    
    print("\n=== Wait for event ===")
    try:
        event = await asyncio.wait_for(runner._event_queue.get(), timeout=3)
        print(f"Got event: {event}")
    except asyncio.TimeoutError:
        print("TIMEOUT - checking pipe state")
        # Check if event was written
        import glob
        events = glob.glob(".dgov/out/mock-test/*")
        print(f"Output files: {events}")
        
        # Check if pipe has data
        print(f"Pipe exists: {pipe_path.exists()}")
        if pipe_path.exists():
            import stat
            mode = os.stat(pipe_path).st_mode
            print(f"Pipe mode: {oct(mode)}")
    
    print("\n=== Cleanup ===")
    runner._cleanup_event_pipe(pipe_path)
    print("Done")

if __name__ == "__main__":
    asyncio.run(main())
