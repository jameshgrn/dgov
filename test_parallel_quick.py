"""Quick parallel test with 3 independent tasks."""

import asyncio
import time

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.runner import EventDagRunner


def create_dag() -> DagDefinition:
    """3 parallel tasks, no dependencies."""
    return DagDefinition(
        name="parallel-quick",
        dag_file="<test:parallel>",
        project_root=".",
        session_root=".",
        max_concurrent=3,
        tasks={
            f"task-{c}": DagTaskSpec(
                slug=f"task-{c}",
                summary=f"Task {c}",
                prompt="Wait 1 second",
                commit_message=f"Task {c} done",
                agent="mock",
                escalation=(),
                depends_on=(),
                files=DagFileSpec(),
                timeout_s=30,
            )
            for c in "abc"
        },
    )


async def main():
    dag = create_dag()
    runner = EventDagRunner(dag=dag, session_root=".")

    print("Starting 3 parallel tasks (1s each)...")
    start = time.time()

    try:
        states = await asyncio.wait_for(runner.run(), timeout=15.0)
        elapsed = time.time() - start

        print(f"\nCompleted in {elapsed:.2f}s")
        print(f"Expected: ~1.5s (parallel)")
        print(f"If sequential would be: ~3.5s")

        for slug, state in states.items():
            print(f"  {slug}: {state}")

    except asyncio.TimeoutError:
        print("TIMEOUT - parallel execution is not working correctly")
        return 1

    return 0


if __name__ == "__main__":
    exit(asyncio.run(main()))
