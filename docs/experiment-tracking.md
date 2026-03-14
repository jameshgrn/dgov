# Experiment tracking

dgov supports sequential hypothesis testing and metric optimization through sequential experiment loops. This allows an agent to iteratively try different approaches, measure their impact, and decide whether to accept (merge) or reject (discard) the change.

## What experiments are

An experiment is a loop that dispatches a worker pane, waits for results, evaluates a metric, and updates a baseline. Each iteration builds on the previous **accepted** baseline.

## Writing a program file

An experiment starts with a markdown file (the **program**) describing the optimization goal and context.

```markdown
# Optimize latency in parser.py

Target: reduce execution time of `parse_file()` by at least 15%.
Context: current implementation uses a slow regex engine. Try a state machine.
```

## Running experiments

Dispatch the experiment loop using the `dgov experiment start` command.

```bash
# Start the loop
dgov experiment start \
  -p experiments/reduce-latency.md \
  -m latency_ms \
  -b 5 \
  -a claude \
  -d minimize
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--program` | `-p` | string | `None` | Program file (markdown) |
| `--metric` | `-m` | string | `None` | Metric name to optimize |
| `--budget` | `-b` | int | `5` | Max number of experiments |
| `--agent` | `-a` | string | `claude` | Agent to use for optimization |
| `--direction`| `-d` | string | `minimize`| `minimize` or `maximize` |
| `--timeout` | `-t` | int | `600` | Timeout per experiment |

## Result file format

The agent must output its results to a JSON file at `.dgov/experiments/results/<exp-id>.json`. dgov automatically provides this path to the agent in its prompt.

```json
{
  "metric_name": "latency_ms",
  "metric_value": 142.5,
  "hypothesis": "Replaced regex with a hand-written state machine",
  "follow_ups": [
    "Pre-allocate the buffer to save another 5ms",
    "Use a zero-copy parser for large files"
  ]
}
```

## How follow-ups work

At the end of each experiment, the agent can suggest **follow-ups**. dgov picks the first follow-up from the last accepted result and uses it as the prompt for the next iteration. If no follow-up exists, it re-uses the original program text.

## Viewing logs

See every iteration of an experiment program:

```bash
# List all results for a program
dgov experiment log -p reduce-latency
```

## Summary

Get a high-level summary of a program's performance:

```bash
# Get best result and total duration
dgov experiment summary -p reduce-latency -d minimize
```

## Baseline tracking

The **best accepted** result becomes the next baseline. If an iteration regresses or fails, dgov discards the changes (the worktree is closed without merging) and re-starts from the last accepted commit.
