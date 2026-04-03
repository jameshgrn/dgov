```bash
dgov plan run .dgov/plans/my-task.toml
```

Or use a specific agent:

```toml
name = "complex-task"
agent = "claude"
```

Use `dgov pane classify` to see what the model recommends without launching:

```bash
dgov pane classify "debug the flaky scheduler test"
```

## Concurrency limits

You can set a `max_concurrent` limit per agent in your `agents.toml`. dgov will refuse to dispatch new workers for that agent if the limit is reached.
