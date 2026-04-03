"""Test runner in isolation."""
import asyncio
import sys
sys.path.insert(0, "src")

print("Step 1: imports...")
from dgov.plan import parse_plan_file, compile_plan
from dgov.runner import EventDagRunner

print("Step 2: parse plan...")
plan = parse_plan_file(".dgov/test-mock.toml")
dag = compile_plan(plan)

print(f"Step 3: DAG has {len(dag.tasks)} tasks")

print("Step 4: create runner...")
runner = EventDagRunner(dag, session_root=".")
print(f"Runner deps: {runner.deps}")
print(f"Kernel tasks: {list(runner.kernel.task_states.keys())}")

print("Step 5: start kernel...")
actions = runner.kernel.start()
print(f"Initial actions: {actions}")
print(f"Kernel done: {runner.kernel.done}")
print(f"States: {runner.kernel.task_states}")
