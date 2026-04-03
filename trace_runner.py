"""Trace runner step by step."""
import sys
sys.path.insert(0, "src")

import asyncio
from pathlib import Path

from dgov.plan import parse_plan_file, compile_plan
from dgov.runner import EventDagRunner
from dgov.kernel import DispatchTask

async def main():
    plan = parse_plan_file(".dgov/test-mock.toml")
    dag = compile_plan(plan)
    
    runner = EventDagRunner(dag, session_root=".")
    
    # Step through manually
    print("1. Setup pipe...")
    pipe_path = await runner._setup_event_pipe()
    print(f"   Pipe: {pipe_path}")
    
    print("2. Start kernel...")
    actions = runner.kernel.start()
    print(f"   Actions: {actions}")
    print(f"   States: {runner.kernel.task_states}")
    
    if not actions:
        print("   ERROR: No actions!")
        return
    
    dispatch_actions = [a for a in actions if isinstance(a, DispatchTask)]
    print(f"   Dispatch actions: {len(dispatch_actions)}")
    
    if dispatch_actions:
        print("3. Dispatch first task...")
        await runner._dispatch(dispatch_actions[0], pipe_path)
        print(f"   Pending: {runner._pending_dispatches}")
        
        print("4. Wait for event...")
        try:
            event = await asyncio.wait_for(runner._event_queue.get(), timeout=3)
            print(f"   Got event: {event}")
        except asyncio.TimeoutError:
            print("   TIMEOUT - no event received")
            print(f"   Check if pipe has readers/writers...")
            import subprocess
            result = subprocess.run(
                ["lsof", str(pipe_path)], 
                capture_output=True, 
                text=True
            )
            print(f"   lsof output:\n{result.stdout}")
    
    print("5. Cleanup...")
    runner._cleanup_event_pipe(pipe_path)
    print("   Done")

if __name__ == "__main__":
    asyncio.run(main())
