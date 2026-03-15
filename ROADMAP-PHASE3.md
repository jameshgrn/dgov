# ROADMAP — Phase 3: Missions & Scale

dgov is a mature, stable orchestration layer for AI coding agents. Phase 1 (reliability) and Phase 2 (API stabilization) successfully moved dgov from a collection of scripts to a robust CLI. Phase 3 focuses on **Missions** (end-to-end automation), **Agent Specialized Detection** (fixing the "done" problem), and **Architectural Modularization**.

## 1. Architecture Assessment

### What's Clean
- **Backend Abstraction:** `WorkerBackend` Protocol in `backend.py` is perfectly decoupled. Moving to Docker or SSH will be trivial.
- **State Persistence:** SQLite + WAL in `persistence.py` is robust and handles concurrent access/events well.
- **Batch Execution:** The tiered DAG logic in `batch.py` is a highlight—smartly handling both explicit dependencies and implicit file-overlap serialization.
- **Hook System:** PRIORITY order for hooks (.dgov/hooks vs .dgov-hooks) is idiomatic and flexible.

### What's Tangled
- **CLI Bloat:** `src/dgov/cli.py` (1.3k lines) is a "God Module". It mixes UI logic, workflow orchestration (templates/experiments), and parameter validation.
- **Status Side-Effects:** `list_worker_panes` in `status.py` calls `_is_done`, which in turn updates persistent state. This makes "listing" a state-modifying operation, which is risky for read-heavy UI components like the dashboard.
- **Wait Heuristics:** `waiter.py` uses a hardcoded list of `_AGENT_COMMANDS` and a "one size fits all" output stabilization check. This is the primary point of failure for new agents.

## 2. Top 5 High-Value Changes

1.  **Mission Primitive:** Introduce a `dgov mission` command that orchestrates the full `create -> wait -> review -> merge` lifecycle. Users shouldn't have to poll manually for single-shot tasks.
2.  **Strategy-Based Done Detection:** Move "done" logic into `AgentDef`. Let agents specify if they signal via exit code, a `.done` file, a specific terminal string, or output stabilization.
3.  **Modular CLI:** Split `cli.py` into `dgov.cli` package. Separate `pane`, `mission`, `batch`, `experiment`, and `config` into distinct files.
4.  **Actionable Dashboard:** Turn the dashboard from a read-only view into a control center. Allow `Enter` to open a response prompt, `m` to merge, and `x` to close directly from the TUI.
5.  **Sandboxed Backend:** Implement `DockerBackend`. Agents are untrusted; running them in worktrees on the host is a security risk.

## 3. Module Split Plan

Justified by the need to isolate UI from orchestration and make the codebase navigable for contributors.

- `src/dgov/cli/` (New Package)
    - `__init__.py`: Group setup and entry point.
    - `pane.py`: Lifecycle commands (create, close, list).
    - `mission.py`: (New) High-level automation.
    - `batch.py`: DAG execution.
    - `inspect.py`: Review, diff, blame, logs.
    - `recovery.py`: Retry, escalate, resume.
    - `meta.py`: Templates, checkpoints, config, stats.
- `src/dgov/git/` (New Package)
    - Extract git plumbing from `merger.py`, `gitops.py`, and `blame.py` into a shared utility layer.

## 4. Per-Agent Done Detection Strategy

Stop using one heuristic for all. Modify `AgentDef` to include a `DoneStrategy`:

```python
@dataclass
class DoneStrategy:
    type: str  # "exit", "file", "pattern", "stable"
    value: str | int | None = None
    timeout_s: int = 600

# Example in AgentDef:
# claude: type="stable", value=15
# pi: type="exit", value=0
# custom: type="pattern", value="[DONE]"
```

`waiter.py` will then dispatch to the specific strategy defined for that agent, reducing false positives/negatives.

## 5. Mission Primitive Design

A Mission is a declarative state-machine.

```bash
dgov mission run "Add docstrings to src/dgov/waiter.py" \
    --agent pi \
    --auto-merge \
    --review severity=medium
```

**Lifecycle:**
1. `PENDING`: Preflight checks run.
2. `RUNNING`: Worker dispatched.
3. `WAITING`: Agent-specific `DoneStrategy` polls.
4. `REVIEWING`: (Optional) `review-fix` logic runs.
5. `MERGING`: `plumbing_merge` with auto-lint.
6. `COMPLETED`: Cleanup worktree/pane.

## 6. Testing Strategy

- **Consolidate Integration:** `tests/test_integration.py` is the most valuable test file. Expand it to cover the `Mission` machine.
- **Mock Backend Rigor:** Ensure `test_dgov_panes.py` exercises the `WorkerBackend` protocol completely so that adding `DockerBackend` doesn't require new pane tests.
- **Wasted Effort:** Stop testing `tmux` output parsing directly. If the `WorkerBackend` is mocked, we are testing `dgov` logic, not `tmux` behavior.

## 7. Anti-patterns to Avoid

- **No "Just-in-case" Features:** Avoid adding "agent-to-agent chat" or "multi-governor sync" until single-governor missions are perfect.
- **Avoid Asyncio:** The current synchronous, subprocess-heavy model is easy to debug and fits the "no daemon" philosophy. Keep it that way.
- **Side-effect Getters:** Remove state updates from `list_worker_panes`. Status should be read-only; state transitions should be explicit.
