# Audit Report: Formal Resource Management for Agent Groups and Concurrency

**Date:** 2026-03-23  
**Auditor:** Governor (Jake Gearon)  
**Scope:** Agent groups, group-level concurrency enforcement, performance optimization

---

## Executive Summary

The formal resource management system for agent groups is **partially implemented** with critical gaps in testing coverage. While the core infrastructure exists in `agents.py` and `router.py`, there are significant concerns around:

1. **Missing tests for group concurrency enforcement** - No unit tests verify that `resolve_agent` properly enforces `max_concurrent` at the group level
2. **Potential race condition in group counting** - Group counts are computed from `all_panes()` snapshot but may miss panes created between the snapshot and the actual resolution
3. **Incomplete documentation of group semantics** - The TOML parsing supports groups but the operational behavior isn't well-specified

**Overall Status:** ⚠️ **PARTIAL** - Core logic exists but untested and potentially fragile under concurrent load

---

## 1. Agent Groups: 'groups' Field in AgentDef and TOML Parsing

### Implementation Review

#### Data Structure (`src/dgov/agents.py`)

```python
@dataclass(frozen=True)
class AgentDef:
    ...
    groups: tuple[str, ...] = ()
```

✅ **Correctly defined** as a tuple of strings with empty tuple default.

#### TOML Parsing (`_agent_def_from_toml`)

```python
def _agent_def_from_toml(agent_id: str, table: dict, source: str) -> AgentDef:
    ...
    return AgentDef(
        ...
        groups=tuple(table.get("groups", ())),
        ...
    )
```

✅ **Correctly parsed** from TOML list into tuple.

#### Merge Logic (`_merge_agent_def`)

```python
# Handle tuple fields
for tf in ("send_keys_pre_prompt", "send_keys_submit", "groups"):
    if tf in kwargs and isinstance(kwargs[tf], list):
        kwargs[tf] = tuple(kwargs[tf])
```

✅ **Correctly converted** to tuple during merge operations.

#### Group Loading (`load_groups`)

```python
def load_groups(project_root: str | None = None) -> dict[str, dict]:
    """Load agent group definitions from TOML config files.

    Returns {group_id: {max_concurrent: int, ...}}.
    """
    groups: dict[str, dict] = {}

    # User global: ~/.dgov/agents.toml
    user_config = Path.home() / ".dgov" / "agents.toml"
    if user_config.is_file():
        try:
            with open(user_config, "rb") as f:
                data = tomllib.load(f)
                groups.update(data.get("groups", {}))
        except (tomllib.TOMLDecodeError, OSError):
            pass

    # Project-local: <project_root>/.dgov/agents.toml
    if project_root:
        project_config = Path(project_root) / ".dgov" / "agents.toml"
        if project_config.is_file():
            try:
                with open(project_config, "rb") as f:
                    data = tomllib.load(f)
                    groups.update(data.get("groups", {}))
            except (tomllib.TOMLDecodeError, OSError):
                pass

    return groups
```

⚠️ **Issue identified**: The function loads groups from both user and project configs, but **does not handle conflicts**. If the same group is defined in both locations, the project config silently overwrites the user config. This may be intentional, but it's not documented.

**Recommendation:** Add logging when project config overrides user config for the same group.

---

## 2. Group Concurrency: `resolve_agent` Enforcing Group-Level `max_concurrent`

### Implementation Review (`src/dgov/router.py`)

```python
def resolve_agent(
    name: str,
    session_root: str,
    project_root: str,
) -> tuple[str, str | None]:
    ...
    tables = _load_routing_tables()
    if name not in tables:
        return name, None

    backends = tables[name]
    registry = load_registry(project_root)
    groups = load_groups(project_root)

    # Optimization: fetch all active panes and tmux info once for group checks
    _TERMINAL_STATES = {
        "done", "failed", "superseded", "merged", "closed", "escalated", "timed_out",
    }
    panes = all_panes(session_root)
    all_tmux = get_backend().bulk_info()

    group_counts: dict[str, int] = {}
    for p in panes:
        agent_id = p.get("agent", "")
        if agent_id and p.get("state") not in _TERMINAL_STATES:
            pane_id = p.get("pane_id", "")
            if pane_id and pane_id in all_tmux:
                # Track group counts
                agent_def = registry.get(agent_id)
                if agent_def:
                    agent_groups = getattr(agent_def, "groups", ())
                    for g in agent_groups:
                        group_counts[g] = group_counts.get(g, 0) + 1

    tried: list[str] = []
    for backend_id in backends:
        agent_def = registry.get(backend_id)
        if agent_def is None:
            tried.append(f"{backend_id} (not registered)")
            continue

        # Group Concurrency Check
        agent_groups = getattr(agent_def, "groups", ())
        if agent_groups:
            group_blocked = False
            for g in agent_groups:
                g_def = groups.get(g)
                if g_def and "max_concurrent" in g_def:
                    active = group_counts.get(g, 0)
                    limit = g_def["max_concurrent"]
                    if active >= limit:
                        tried.append(f"{backend_id} (group '{g}' full: {active}/{limit})")
                        group_blocked = True
                        break
            if group_blocked:
                continue
```

### ✅ Correct Aspects

1. **Single snapshot optimization**: Fetches all panes once via `all_panes()` rather than querying per-backend
2. **Terminal state filtering**: Excludes terminal states from count (correct)
3. **Group iteration**: Checks ALL groups an agent belongs to (an agent can belong to multiple groups)
4. **Fail-safe blocking**: If any group is at capacity, the entire backend is skipped

### ⚠️ Critical Issues

#### Issue 1: Race Condition Between Snapshot and Resolution

The group count is computed from a **snapshot** of panes, then used to make routing decisions. However:

```python
panes = all_panes(session_root)  # Snapshot A
...
group_counts[g] = group_counts.get(g, 0) + 1  # Computed from snapshot
...
if active >= limit:  # Decision made using snapshot
    continue
```

**Problem:** A new pane could be dispatched between the snapshot and the actual dispatch, causing the group to exceed its limit.

**Impact:** Under high concurrency, the actual number of active agents in a group could exceed `max_concurrent`.

**Severity:** Medium - The window is small (milliseconds), but non-zero.

#### Issue 2: Missing Test Coverage

There are **NO unit tests** in `test_router.py` that verify:
- Group concurrency is enforced
- Agents in different groups don't share limits
- Multiple groups on the same agent are all checked

This is a **critical gap** because the feature was added without automated verification.

#### Issue 3: Inconsistent Counting Logic

The code checks if `pane_id in all_tmux` before counting:

```python
if pane_id and pane_id in all_tmux:
    group_counts[g] = group_counts.get(g, 0) + 1
```

**Question:** Why check against `all_tmux`? Is this to filter out zombie panes? This logic is not explained and may exclude valid panes.

**Risk:** If `bulk_info()` doesn't include all active panes (e.g., very recently created ones), they won't be counted, leading to incorrect concurrency tracking.

#### Issue 4: No Validation That Groups Are Defined

If an agent has `groups=("river-gpu",)` but no group named `"river-gpu"` is defined in the TOML, the router silently ignores it:

```python
g_def = groups.get(g)
if g_def and "max_concurrent" in g_def:
    # Only applies if max_concurrent is set
```

**Behavior:** An agent can have groups assigned, but if those groups don't have `max_concurrent` defined, they impose no limit.

**Recommendation:** Add validation during registry load to warn about undefined groups.

---

## 3. Performance: Optimization of State Fetching in Routing Layer

### Current Implementation

The router optimizes state fetching by:

```python
# Optimization: fetch all active panes and tmux info once for group checks
panes = all_panes(session_root)
all_tmux = get_backend().bulk_info()

group_counts: dict[str, int] = {}
for p in panes:
    ...
```

### ✅ Good Optimizations

1. **Single `all_panes()` call**: Avoids repeated database queries
2. **Single `bulk_info()` call**: Fetches all tmux state at once
3. **In-memory aggregation**: Builds `group_counts` dict in a single pass

### ⚠️ Potential Performance Issues

#### Issue 1: Unnecessary `all_tmux` Lookup

```python
pane_id = p.get("pane_id", "")
if pane_id and pane_id in all_tmux:
```

Every pane lookup requires a dictionary membership check against `all_tmux`. For large numbers of panes, this adds overhead.

**Optimization opportunity:** Filter panes by state BEFORE checking tmux presence, or use a set for O(1) lookups (currently dict, so still O(1)).

#### Issue 2: Redundant Registry Lookups

```python
agent_def = registry.get(agent_id)
if agent_def:
    agent_groups = getattr(agent_def, "groups", ())
```

This happens inside the loop over all panes. For projects with many panes, this could be optimized by:

1. Pre-computing a mapping: `{agent_id: groups_tuple}` once
2. Or caching the result of `registry.get()` for frequently accessed agents

**Current impact:** Low for typical workloads (<100 panes), but could matter at scale.

#### Issue 3: No Caching of Group Counts

Each call to `resolve_agent` recomputes `group_counts` from scratch. For rapid-fire dispatches (e.g., DAG execution with many parallel tasks), this could be expensive.

**Recommendation:** Consider caching `group_counts` with a short TTL (e.g., 100ms) if multiple resolutions happen in quick succession.

---

## 4. Verification: Shared Resource Limits Across Disjoint Agent IDs

### Scenario: River GPU Cluster

Assume we have:

```toml
# ~/.dgov/agents.toml
[routing.qwen-35b]
backends = ["river-35b", "qwen35-35b"]

[agents.river-35b]
command = "river"
groups = ["river-gpu"]
max_concurrent = 2

[agents.qwen35-35b]
command = "qwen35"
groups = ["river-gpu"]
max_concurrent = 5

[groups.river-gpu]
max_concurrent = 3
```

**Expected behavior:**
- Both `river-35b` and `qwen35-35b` belong to the `river-gpu` group
- The group has `max_concurrent = 3`
- Total active workers across BOTH backends should not exceed 3

### Code Walkthrough

```python
# Both backends are in the same group
agent_groups = getattr(agent_def, "groups", ())  # ("river-gpu",) for both

for g in agent_groups:  # g = "river-gpu"
    g_def = groups.get(g)  # {"max_concurrent": 3}
    if g_def and "max_concurrent" in g_def:
        active = group_counts.get(g, 0)  # COUNTS ACROSS ALL AGENTS IN GROUP
        limit = g_def["max_concurrent"]  # 3
        if active >= limit:
            # Block this backend
```

### ✅ Correct Behavior

The implementation **correctly enforces shared limits** across disjoint agent IDs:

1. `group_counts` is a **single dictionary** keyed by group name
2. All agents in the same group increment the **same counter**
3. When checking availability, the **total active count** is compared to the group limit

**Example trace:**
```
1. river-35b dispatched → group_counts["river-gpu"] = 1
2. qwen35-35b dispatched → group_counts["river-gpu"] = 2
3. river-35b dispatched again → group_counts["river-gpu"] = 3
4. qwen35-35b dispatched → BLOCKED (3 >= 3)
5. river-35b dispatched again → BLOCKED (3 >= 3)
```

### ⚠️ Edge Case: Agents Without Explicit Groups

If an agent has NO groups defined:

```python
agent_groups = getattr(agent_def, "groups", ())  # ()
if agent_groups:  # False, skip group check
    ...
```

**Result:** The agent bypasses group concurrency entirely and only respects individual `max_concurrent`.

**Is this correct?** Yes - if you don't assign an agent to a group, it shouldn't be subject to group limits. But this should be documented.

### ⚠️ Edge Case: Agent in Multiple Groups

If an agent belongs to multiple groups:

```python
groups = ["high-priority", "gpu-access"]
```

Both groups are checked:

```python
for g in agent_groups:
    if active >= limit:
        group_blocked = True
        break
```

**Result:** The agent is blocked if **ANY** of its groups is at capacity.

**Is this correct?** Yes - this is conservative and prevents over-subscription of any resource the agent might need.

---

## 5. Test Coverage Analysis

### Existing Tests in `test_router.py`

| Test | Covers Groups? |
|------|----------------|
| `test_passthrough_for_physical_agent` | ❌ |
| `test_passthrough_for_unknown_name` | ❌ |
| `test_resolve_returns_physical_backend` | ❌ |
| `test_skips_unhealthy_backend` | ❌ |
| `test_skips_busy_backend` | ❌ (only tests individual `max_concurrent`) |
| `test_raises_when_all_unavailable` | ❌ |

### Missing Tests

**Critical missing tests:**

1. **Test group concurrency enforcement**
   ```python
   def test_group_max_concurrent_enforced(self, tmp_path, monkeypatch):
       # Two backends in same group
       # Dispatch one to each
       # Third dispatch should fail due to group limit
   ```

2. **Test disjoint agents sharing group limit**
   ```python
   def test_disjoint_agents_share_group_limit(self, tmp_path, monkeypatch):
       # river-35b and qwen35-35b both in "river-gpu" group
       # Limit is 2
       # Should block after 2 total across both backends
   ```

3. **Test agent in multiple groups**
   ```python
   def test_agent_multiple_groups_all_checked(self, tmp_path, monkeypatch):
       # Agent in ["group-a", "group-b"]
       # group-a at limit, group-b not
       # Should block
   ```

4. **Test group override precedence**
   ```python
   def test_project_overrides_user_group_limit(self, tmp_path, monkeypatch):
       # User defines group with max_concurrent=5
       # Project overrides with max_concurrent=2
       # Router uses project value (2)
   ```

5. **Test undefined group handling**
   ```python
   def test_agent_with_undefined_group(self, tmp_path, monkeypatch):
       # Agent has groups=["unknown-group"]
       # No group definition exists
       # Should bypass group check (current behavior)
   ```

### Existing Tests in `test_dgov_agents.py`

Groups are mentioned in:
- `test_project_config_overrides_safe_fields` - mentions `max_concurrent` but not groups
- `test_env_and_permissions_from_toml` - mentions env/permissions but not groups

**No tests explicitly validate the `groups` field.**

---

## 6. Recommendations

### High Priority

1. **Add comprehensive group concurrency tests** to `test_router.py`
   - Minimum 5 test cases covering the scenarios above
   - Use `monkeypatch` to isolate from real config files

2. **Document group semantics in CLAUDE.md**
   - How groups work
   - Precedence rules (user vs project config)
   - What happens if a group is undefined

3. **Add validation warnings**
   - Warn if an agent references an undefined group
   - Warn if a group has no `max_concurrent` defined (likely a mistake)

### Medium Priority

4. **Reduce race condition window**
   - Consider adding a brief lock when updating group counts
   - Or accept the small risk and document it

5. **Optimize group count computation**
   - Cache the `agent_id -> groups` mapping
   - Consider memoizing `group_counts` for 100ms

6. **Clarify `pane_id in all_tmux` logic**
   - Add comment explaining why this check exists
   - Consider removing if unnecessary

### Low Priority

7. **Consider group inheritance**
   - Could allow groups to nest: `["parent-group", "child-group"]`
   - Would enforce limits at both levels

8. **Add metrics/logging**
   - Log when a backend is blocked due to group limits
   - Track group utilization over time

---

## 7. Conclusion

The formal resource management system for agent groups is **functionally correct** but **insufficiently tested**. The core logic for enforcing group-level concurrency limits is sound and correctly handles:

- ✅ Shared limits across disjoint agent IDs
- ✅ Multiple groups per agent
- ✅ Individual vs group concurrency (both must pass)

However, the lack of automated tests means this feature has not been verified under realistic conditions. The race condition between snapshot and resolution is acceptable for most workloads but should be documented.

**Next steps:**
1. Write comprehensive tests (priority)
2. Add validation warnings for misconfiguration
3. Document group semantics
4. Consider performance optimizations if needed at scale

---

## Appendix: Example Configuration

```toml
# ~/.dgov/agents.toml

[routing.qwen-35b]
backends = ["river-35b", "qwen35-35b", "local-qwen35"]

[agents.river-35b]
command = "river"
transport = "positional"
groups = ["river-gpu", "high-performance"]
max_concurrent = 2

[agents.qwen35-35b]
command = "qwen35"
transport = "positional"
groups = ["river-gpu"]
max_concurrent = 5

[agents.local-qwen35]
command = "qwen35-local"
transport = "positional"
groups = ["local-dev"]
max_concurrent = 10

[groups.river-gpu]
max_concurrent = 3

[groups.high-performance]
max_concurrent = 5

[groups.local-dev]
max_concurrent = 10
```

**Behavior:**
- `river-35b`: Can run up to 2 concurrently, but also limited by `river-gpu` group (3 total)
- `qwen35-35b`: Can run up to 5 individually, but limited by `river-gpu` group (3 total across river-35b + qwen35-35b)
- `local-qwen35`: Runs independently in `local-dev` group, no conflict with GPU agents

**Total GPU usage:** Max 3 across `river-35b` + `qwen35-35b` combined.
