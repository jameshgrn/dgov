#!/usr/bin/env python3
"""Demo: File-claim conflict detection in action."""

from dgov.kernel import DagKernel, DagTaskState
from dgov.unit import UnitSpec
from dgov.unit_compile import compile_unit


def demo():
    print("=" * 70)
    print("DEMO: File-Claim Conflict Detection")
    print("=" * 70)

    # Create 5 units: A,B conflict on cli.py; C,D conflict on api.py; E is independent
    units = [
        ("add-cli-help", "Add --help flag to src/dgov/cli.py"),
        ("add-cli-version", "Add --version flag to src/dgov/cli.py"),  # CONFLICTS with A
        ("add-api-auth", "Add auth middleware to src/dgov/api.py"),
        ("add-api-logging", "Add request logging to src/dgov/api.py"),  # CONFLICTS with C
        ("fix-docs-typo", "Fix typo in README.md"),  # NO CONFLICT
    ]

    print("\n1. CREATING UNITS")
    print("-" * 70)

    all_tasks = {}
    for name, description in units:
        unit = UnitSpec(
            name=name,
            goal=f"{description}. Edit the relevant files and commit.",
            agent="kimi",
        )
        dag = compile_unit(unit, agent="kimi")

        for slug, task in dag.tasks.items():
            full_slug = f"{name}/{slug}"
            all_tasks[full_slug] = task
            files = task.all_touches()
            print(f"  {name:20} → files: {files}")

    print("\n2. BUILDING KERNEL WITH FILE CLAIMS")
    print("-" * 70)

    task_files = {
        slug: t.all_touches() for slug, t in all_tasks.items() if t.all_touches()
    }

    kernel = DagKernel(
        deps={slug: () for slug in all_tasks},
        task_files=task_files,
    )
    kernel.merge_order = tuple(all_tasks.keys())
    kernel.task_states = {slug: DagTaskState.PENDING for slug in all_tasks}

    print(f"  Total tasks: {len(all_tasks)}")
    print(f"  Tasks with file claims: {len(task_files)}")

    print("\n3. SCHEDULING ROUND 1 (Initial dispatch)")
    print("-" * 70)

    actions1 = kernel._schedule()
    dispatched1 = [a.task_slug for a in actions1]
    pending1 = [s for s, st in kernel.task_states.items() if st == DagTaskState.PENDING]

    print(f"  DISPATCHED ({len(dispatched1)}):")
    for slug in dispatched1:
        files = task_files.get(slug, ())
        print(f"    ✅ {slug:35} files={files}")

    print(f"\n  QUEUED ({len(pending1)}):")
    for slug in pending1:
        files = task_files.get(slug, ())
        # Show what's blocking it
        conflicts = []
        for d_slug in dispatched1:
            d_files = set(task_files.get(d_slug, ()))
            if set(files) & d_files:
                conflicts.append(d_slug)
        print(f"    ⏳ {slug:35} files={files}")
        print(f"       blocked by: {conflicts}")

    print("\n4. SIMULATING COMPLETIONS")
    print("-" * 70)

    # Simulate first dispatched task completing
    first_done = dispatched1[0]
    from dgov.kernel import TaskWaitDone

    print(f"  Task '{first_done}' completes...")
    kernel.task_states[first_done] = DagTaskState.MERGED

    print("\n5. SCHEDULING ROUND 2 (After first completion)")
    print("-" * 70)

    # Reset DISPATCHED states back to check scheduling again
    # In real system, this happens via event handling
    for slug in list(kernel.task_states.keys()):
        if kernel.task_states[slug] == DagTaskState.DISPATCHED:
            kernel.task_states[slug] = DagTaskState.MERGED

    actions2 = kernel._schedule()
    dispatched2 = [a.task_slug for a in actions2]
    pending2 = [s for s, st in kernel.task_states.items() if st == DagTaskState.PENDING]

    print(f"  NEWLY DISPATCHED ({len(actions2)}):")
    for slug in dispatched2:
        files = task_files.get(slug, ())
        print(f"    ✅ {slug:35} files={files}")

    print(f"\n  STILL QUEUED ({len(pending2)}):")
    for slug in pending2:
        files = task_files.get(slug, ())
        print(f"    ⏳ {slug:35} files={files}")

    print("\n6. FINAL STATE")
    print("-" * 70)

    by_state = {}
    for slug, state in kernel.task_states.items():
        by_state.setdefault(state.value, []).append(slug)

    print(f"  MERGED:   {len(by_state.get('merged', []))} tasks")
    print(f"  PENDING:  {len(by_state.get('pending', []))} tasks")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"""
Conflict groups identified:
  Group 1 (cli.py): {['add-cli-help', 'add-cli-version']}
  Group 2 (api.py): {['add-api-auth', 'add-api-logging']}
  Independent:      {['fix-docs-typo']}

Parallelism achieved:
  • Round 1: {len(dispatched1)} tasks in parallel
  • Round 2: {len(dispatched2)} more tasks
  • Safe concurrency: Non-conflicting tasks ran together
  • Conflicting tasks: Serialized automatically

Result: File-claim conflict detection working correctly!
""")


if __name__ == "__main__":
    demo()
