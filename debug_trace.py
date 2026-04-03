"""Debug run with tracing."""
import sys
sys.path.insert(0, "src")

import asyncio
import logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s %(name)s: %(message)s')

from dgov.plan import parse_plan_file, compile_plan
from dgov.runner import EventDagRunner

async def main():
    print("=== Starting ===")
    plan = parse_plan_file(".dgov/test-mock.toml")
    dag = compile_plan(plan)
    
    runner = EventDagRunner(dag, session_root=".")
    
    print("=== Running ===")
    try:
        results = await asyncio.wait_for(runner.run(), timeout=5)
        print(f"Results: {results}")
    except asyncio.TimeoutError:
        print("TIMEOUT")
        print(f"Pending: {runner._pending_dispatches}")
        print(f"Kernel states: {runner.kernel.task_states}")
        print(f"Kernel done: {runner.kernel.done}")
        import os
        print(f"Pipe exists: {os.path.exists('.dgov/event.pipe')}")

if __name__ == "__main__":
    asyncio.run(main())
