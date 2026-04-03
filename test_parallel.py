"""Test parallel execution using the DAG runner directly."""

import asyncio
import time
from pathlib import Path

from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.runner import EventDagRunner


def create_parallel_dag(work_time_s: float = 2.0, num_tasks: int = 3) -> DagDefinition:
    """Create a DAG with N parallel tasks, each sleeping for work_time_s."""
    tasks = {}
    for i in range(num_tasks):
        slug = f"task-{chr(ord('a') + i)}"
        tasks[slug] = DagTaskSpec(
            slug=slug,
            summary=f"Parallel task {slug}",
            prompt=f"Wait {work_time_s} seconds",
            commit_message=f"Task {slug} completed",
            agent="mock",
            escalation=(),
            depends_on=(),  # No dependencies = parallel
            files=DagFileSpec(create=[f"{slug}.txt"]),
            timeout_s=60,
        )

    return DagDefinition(
        name="parallel-dag",
        dag_file="<test:parallel>",
        project_root=".",
        session_root=".",
        max_concurrent=3,  # Allow all 3 to run concurrently
        tasks=tasks,
    )


async def main():
    """Run parallel test and measure timing."""
    work_time = 2.0  # Each task waits 2 seconds
    num_tasks = 3    # 3 tasks

    dag = create_parallel_dag(work_time, num_tasks)
    runner = EventDagRunner(dag=dag, session_root=".")

    print(f"Running {num_tasks} parallel tasks, each ~{work_time}s of work")
    print(f"Sequential would take: ~{num_tasks * work_time:.1f}s")
    print(f"Parallel should take: ~{work_time + 0.5:.1f}s (work + harness overhead)")
    print()

    start = time.time()
    states = await runner.run()
    elapsed = time.time() - start

    print(f"\nCompleted in {elapsed:.2f}s")
    print(f"Speedup vs sequential: {num_tasks * work_time / elapsed:.1f}x")
    print()
    print("Task states:")
    for slug, state in states.items():
        print(f"  {slug}: {state}")


if __name__ == "__main__":
    asyncio.run(main())
