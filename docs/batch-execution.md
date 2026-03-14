# Batch execution

dgov can execute multiple tasks simultaneously using a JSON specification. It automatically handles parallelization using a Directed Acyclic Graph (DAG) to ensure overlapping files are not modified at the same time.

## Spec format

A batch spec is a JSON file that defines a project root and a list of tasks.

```json
{
  "project_root": "/path/to/repo",
  "tasks": [
    {
      "id": "lint-fix",
      "prompt": "Fix all ruff warnings",
      "agent": "pi",
      "touches": ["src/"]
    },
    {
      "id": "add-tests",
      "prompt": "Add unit tests for parser.py",
      "agent": "claude",
      "touches": ["tests/test_parser.py"]
    },
    {
      "id": "update-docs",
      "prompt": "Update docstrings in parser.py",
      "agent": "pi",
      "touches": ["src/parser.py"]
    }
  ]
}
```

## DAG scheduling

dgov calculates **tiers** of parallel tasks:
1. Tasks with disjoint `touches` (no overlapping files or directories) are grouped into the same tier and run in parallel.
2. Tasks that overlap are serialized into subsequent tiers.

In the example above:
- `lint-fix` (src/) and `update-docs` (src/parser.py) **overlap**.
- `add-tests` (tests/) does **not** overlap with either.

**Computed Tiers:**
- **Tier 0**: `lint-fix`, `add-tests`
- **Tier 1**: `update-docs`

## Dry run

Before executing a batch, you can see the computed tiers:

```bash
dgov batch spec.json --dry-run
```

## Execution flow

1. **Create Panes**: All tasks in the current tier are created simultaneously.
2. **Wait**: dgov waits for all panes in the tier to reach the `done` state.
3. **Merge**: Each task is merged into `main`.
4. **Next Tier**: If all merges succeed, dgov moves to the next tier.

**Note**: A failure in any tier (timeout or merge conflict) will abort all remaining tiers to prevent compounding errors.

## Execution

To run the batch:

```bash
dgov batch spec.json
```

## Output format

dgov outputs a JSON summary of the execution:

```json
{
  "tiers": [
    {
      "tier": 0,
      "tasks": [
        {"id": "lint-fix", "slug": "lint-fix", "status": "merged"},
        {"id": "add-tests", "slug": "add-tests", "status": "merged"}
      ]
    },
    {
      "tier": 1,
      "tasks": [
        {"id": "update-docs", "slug": "update-docs", "status": "merged"}
      ]
    }
  ],
  "merged": ["lint-fix", "add-tests", "update-docs"],
  "failed": []
}
```
