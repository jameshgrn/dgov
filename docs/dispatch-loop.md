# Canonical Dispatch Loop

The dgov governor-worker dispatch path is optimized for **<50ms latency** on the hot path while maintaining full observability through structured spans.

## Architecture

```
┌─────────────┐     ┌─────────────────┐     ┌──────────────┐     ┌─────────────┐
│   Entry     │────▶│    Preflight    │────▶│    Spawn     │────▶│   Track     │
│  (plan/     │     │  (Parallel)     │     │   (tmux)     │     │  (events)   │
│  pane)      │     │                 │     │              │     │             │
└─────────────┘     └─────────────────┘     └──────────────┘     └─────────────┘
                           │
                           ▼
                    ┌─────────────────┐
                    │  Cached Registry│  <1ms
                    │  get_registry() │
                    └─────────────────┘
```

## Performance Characteristics

| Component | Cold | Hot | Target |
|-----------|------|-----|--------|
| Registry load | 0.5ms | **0.04ms** | <5ms ✓ |
| Preflight checks | 91ms | **49ms** | <50ms ✓ |
| Command build | 1ms | **0.5ms** | <1ms |
| Pane spawn | 800ms | **200ms** | N/A (tmux overhead) |
| **Total hot path** | - | **<50ms** | **<50ms ✓** |

## The Four Stages

### 1. Fast Preflight

```python
async def run_preflight_async(...) -> PreflightReport:
    with span("preflight"):
        # Batch 1: All independent checks run concurrently
        batch1 = await asyncio.gather(
            check_agent_cli(),      # Verify agent binary exists
            check_git_clean(),      # Verify working tree is clean
            check_git_branch(),     # Verify on correct branch
            check_agent_concurrency(),  # Check agent capacity
            check_stale_worktrees(),    # Clean up old worktrees
        )

        # Batch 2: Conditional health check
        if agent_cli_passed and agent_def.health.check:
            health = await check_agent_health()

        # Batch 3: File locks (always last)
        locks = await check_file_locks()
```

**Key optimizations:**
- Parallel execution: 5 checks in ~45ms vs 5× sequential (~150ms)
- Cached registry: `get_registry()` instead of `load_registry()`
- Spans for every check with timing breakdown

### 2. Command Building

Commands are cached and built without subprocess:

```python
def build_launch_command_cached(agent: str, prompt: str) -> str:
    registry = get_registry(project_root)  # <1ms
    agent_def = registry[agent]
    return agent_def.command.format(prompt=shlex.quote(prompt))
```

### 3. Pane Spawn

```python
async def spawn_pane(cmd: str, ctx: DispatchContext) -> str:
    with span("pane_spawn"):
        pane_id = create_pane(title=ctx.task_slug)
        send_command(pane_id, cmd)  # 40ms total (was 300ms)
        return pane_id
```

**Removed bottlenecks:**
- `time.sleep(0.5)` → `time.sleep(0.05)` (10× faster)
- `time.sleep(0.3)` → `time.sleep(0.04)` (7.5× faster)
- `asyncio.sleep(0.2)` → signal-based wait (unbounded → responsive)

### 4. Event Tracking

The kernel tracks workers via events, not polling:

```python
# Worker signals completion
echo "pane_done:{slug}" > .dgov/event.pipe

# Kernel receives and transitions state
class DagKernel:
    def handle_pane_done(self, slug: str):
        task = self.tasks[slug]
        task.state = TaskState.COMPLETED
        self._trigger_merge_if_ready(task)
```

**No polling loops.** Zero `time.sleep()` in event handling.

## Observability

Every dispatch creates a trace tree:

```
plan-run-abc123: 245.60ms
├── compile_dag:   1.20ms
├── dispatch_task: 49.40ms
│   ├── preflight: 48.89ms
│   │   ├── batch1_independent: 45.15ms
│   │   │   ├── check_agent_cli:  5.23ms
│   │   │   ├── check_git_clean: 44.07ms
│   │   │   ├── check_git_branch: 16.58ms
│   │   │   ├── check_agent_concurrency: 2.49ms
│   │   │   └── check_stale_worktrees: 13.86ms
│   │   └── batch3_file_locks: 3.53ms
│   └── pane_spawn: 194.20ms
└── merge_task:    0.80ms
```

### Querying Traces

```python
from dgov.spans import get_trace, get_slow_spans, print_trace

# Get full trace
trace = get_trace("abc123", ".")

# Find bottlenecks
slow = get_slow_spans(threshold_ms=50)

# Visual tree
print_trace("abc123", ".")
```

Spans are written to `.dgov/spans/YYYYMMDD_HHMMSS.jsonl` as JSONL for easy processing.

## Extension Points

### Adding a Prelight Check

```python
def check_my_new_thing(project_root: str) -> CheckResult:
    """Custom preflight check."""
    with SpanContext("check_my_new_thing"):
        # Check logic here
        passed = verify_something()
        return CheckResult(
            name="my_new_thing",
            passed=passed,
            critical=True,  # Block dispatch if failed
            message="...",
        )

# Add to batch1 (independent) or batch2 (conditional)
batch1_tasks.append(
    _run_check_async("my_new_thing", check_my_new_thing, project_root)
)
```

### Custom Span Annotations

```python
from dgov.spans import span, annotate

@span("my_operation")
def my_operation(data: dict):
    annotate("input_size", len(data))
    result = process(data)
    annotate("output_size", len(result))
    annotate("cache_hit", result.from_cache)
    return result
```

## Recovery from Slow Paths

If a check fails and requires remediation:

```python
if not check.passed and check.fixable:
    with SpanContext("auto_fix"):
        fix_result = attempt_fix(check)
        annotate("fix_applied", fix_result.success)
        if fix_result.success:
            # Retry the check
            check = re_run_check(check)
```

Auto-fixes are themselves traced, so slow remediation paths are visible in the trace tree.

## Design Principles

1. **Canonical path**: All dispatch goes through the same 4 stages
2. **Zero polling**: Event-driven, no `sleep()` loops
3. **Detect, don't assume**: Use blocking operations as sync points
4. **Trace everything**: Every stage, every check, every decision
5. **Fail fast**: Abort at preflight, don't spawn then fail
6. **Cache aggressively**: Registry, commands, results — never repeat work
