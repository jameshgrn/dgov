"""Debug runner."""
import asyncio
import sys
sys.path.insert(0, "src")

from dgov.plan import parse_plan_file, compile_plan
from dgov.runner import EventDagRunner

async def main():
    plan = parse_plan_file(".dgov/test-mock.toml")
    dag = compile_plan(plan)
    
    print(f"DAG: {dag.name}")
    print(f"Tasks: {list(dag.tasks.keys())}")
    print(f"Task agents: {[(s, t.agent) for s, t in dag.tasks.items()]}")
    
    runner = EventDagRunner(dag, session_root=".")
    
    print(f"\nKernel deps: {runner.deps}")
    print(f"Kernel states before start: {runner.kernel.task_states}")
    
    actions = runner.kernel.start()
    print(f"\nInitial actions: {actions}")
    print(f"Kernel states after start: {runner.kernel.task_states}")
    print(f"Kernel done: {runner.kernel.done}")
    
    # Check if there's work to do
    if not actions:
        print("No actions - checking if done or stuck")
        return
    
    # Try dispatching first action
    from dgov.kernel import DispatchTask
    for action in actions:
        print(f"\nAction: {type(action).__name__} - {action}")
        if isinstance(action, DispatchTask):
            print(f"Dispatching {action.task_slug}...")
            await runner._dispatch(action)
            print(f"Dispatched. Pending: {runner._pending_dispatches}")
    
    # Now wait for event
    print("\nWaiting for event (3s timeout)...")
    try:
        event = await asyncio.wait_for(runner._event_queue.get(), timeout=3)
        print(f"Got event: {event}")
    except asyncio.TimeoutError:
        print("TIMEOUT - no event received")
        print(f"Check if pipe exists: ls -la .dgov/event.pipe")
        import os
        pipe_path = ".dgov/event.pipe"
        if os.path.exists(pipe_path):
            print(f"Pipe exists: {pipe_path}")
        else:
            print(f"Pipe does NOT exist")

if __name__ == "__main__":
    asyncio.run(main())
