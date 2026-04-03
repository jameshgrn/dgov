"""Test runner debug — print what's happening."""

import asyncio
import sys
sys.path.insert(0, "src")

from dgov.plan import compile_plan, parse_plan_file
from dgov.runner import EventDagRunner

async def main():
    plan = parse_plan_file(".dgov/test-mock.toml")
    dag = compile_plan(plan)
    
    print(f"DAG: {dag.name}")
    print(f"Tasks: {list(dag.tasks.keys())}")
    
    runner = EventDagRunner(dag, session_root=".")
    print("Runner created")
    
    # Run with timeout
    try:
        results = await asyncio.wait_for(runner.run(), timeout=10)
        print(f"Results: {results}")
    except asyncio.TimeoutError:
        print("TIMEOUT")
        print(f"Pending: {runner._pending_dispatches}")
        print(f"Kernel done: {runner.kernel.done}")
        print(f"Kernel states: {runner.kernel.task_states}")

if __name__ == "__main__":
    asyncio.run(main())
