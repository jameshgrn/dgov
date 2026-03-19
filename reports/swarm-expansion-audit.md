# Swarm Expansion Audit

## Executive Summary

This audit maps the current state schema and CLI architecture to identify integration points for three new swarm coordination commands: `think`, `convo`, and `watch`. The analysis reveals that dgov's event-driven persistence layer is well-positioned for swarm expansion, with clear hooks in both the event log and CLI command registry.

---

## 1. State Schema Analysis

### 1.1 Pane States (persistence.py)

**Current canonical states:**
```python
PANE_STATES = frozenset({
    "active", "done", "failed", "reviewed_pass", "reviewed_fail",
    "merged", "merge_conflict", "timed_out", "escalated",
    "superseded", "closed", "abandoned"
})
```

**Integration opportunities:**
- **`think`**: Could introduce `"thinking"` or `"reasoning"` intermediate state
- **`convo`**: No new state needed — uses existing `"active"` + parent-child hierarchy
- **`watch`**: Could introduce `"monitoring"` or `"observing"` state for passive observation panes

### 1.2 Event Log (VALID_EVENTS)

**Current events (56 total):**
- Pane lifecycle: `pane_created`, `pane_done`, `pane_resumed`, `pane_timed_out`, `pane_merged`, etc.
- Review/experiment: `review_pass`, `review_fail`, `experiment_started`, `experiment_accepted`, `experiment_rejected`
- DAG execution: `dag_started`, `dag_tier_started`, `dag_task_dispatched`, `dag_task_completed`, `dag_task_failed`, `dag_task_escalated`, `dag_tier_completed`, `dag_completed`, `dag_failed`
- Monitoring: `monitor_nudge`, `monitor_auto_complete`, `monitor_idle_timeout`, `monitor_blocked`, `monitor_auto_merge`, `monitor_auto_retry`, `monitor_tick`
- Mission: `mission_pending`, `mission_running`, `mission_waiting`, `mission_reviewing`, `mission_merging`, `mission_completed`, `mission_failed`

**Integration opportunities:**

#### For `think` (reasoning/analysis phase):
```python
NEW_EVENTS = {
    "think_started",        # Reasoning phase begins
    "think_step",           # Intermediate reasoning step
    "think_concluded",      # Reasoning complete, ready for action
    "think_aborted",        # Reasoning interrupted
}
```

#### For `convo` (multi-agent dialogue):
```python
NEW_EVENTS = {
    "convo_started",        # Dialogue session initiated
    "convo_message_sent",   # Message sent to participant
    "convo_message_received", # Response received
    "convo_turn",           # Turn completed (bidirectional exchange)
    "convo_ended",          # Dialogue concluded
    "convo_escalated",      # Escalated to stronger agent mid-dialogue
}
```

#### For `watch` (passive monitoring):
```python
NEW_EVENTS = {
    "watch_started",        # Observation begins
    "watch_event",          # Monitored event detected
    "watch_alert",          # Threshold breach / anomaly
    "watch_summary",        # Periodic summary report
    "watch_stopped",        # Observation ends
}
```

### 1.3 Pane Hierarchy

**Current fields:**
```python
@dataclass
class WorkerPane:
    slug: str
    prompt: str
    pane_id: str
    agent: str
    project_root: str
    worktree_path: str
    branch_name: str
    created_at: float
    owns_worktree: bool
    base_sha: str
    parent_slug: str          # ← Key for swarm relationships
    tier_id: str              # ← Key for tier-based coordination
    role: str = "worker"      # Current: "worker" or "lt-gov"
    state: str = "active"
```

**Integration opportunities:**

| Command | Use of `parent_slug` | Use of `tier_id` | New role value? |
|---------|---------------------|------------------|-----------------|
| `think` | Parent = orchestrator | Tier = reasoning tier | `"reasoner"` optional |
| `convo` | Parent = conversation host | Tier = dialogue round | `"dialogue_participant"` |
| `watch` | Parent = watcher supervisor | Tier = monitoring tier | `"observer"` |

**Helper function already exists:**
```python
def get_child_panes(session_root: str, parent_slug: str) -> list[dict]:
    """Return all panes whose parent_slug matches *parent_slug*."""
```

---

## 2. CLI Architecture Analysis

### 2.1 Command Registry Structure

**Current top-level commands (cli/__init__.py):**
```python
cli.add_command(pane)      # pane create, merge, wait, close, list, etc.
cli.add_command(plan)
cli.add_command(preflight_cmd)
cli.add_command(status)
cli.add_command(rebase)
cli.add_command(blame)
cli.add_command(list_agents)
cli.add_command(version_cmd)
cli.add_command(stats)
cli.add_command(dashboard)
cli.add_command(template)
cli.add_command(checkpoint)
cli.add_command(batch)
cli.add_command(experiment)
cli.add_command(review_fix)
cli.add_command(openrouter)
cli.add_command(init_cmd)
cli.add_command(doctor_cmd)
cli.add_command(gc_cmd)
cli.add_command(mission_cmd)
cli.add_command(dag)
cli.add_command(merge_queue)
cli.add_command(briefing_cmd)
cli.add_command(terrain_cmd)
cli.add_command(tunnel_cmd)
cli.add_command(worker)
cli.add_command(monitor_cmd)
```

**Pattern for adding commands:**
1. Create command group/function in dedicated module (`dgov/cli/<name>.py`)
2. Add import in `cli/__init__.py` after imports section
3. Register with `cli.add_command(<name>)`

### 2.2 Integration Points

#### Option A: Top-level swarm commands (recommended)

Add three new top-level commands alongside `dag`, `mission`, `monitor`:

```python
# In cli/__init__.py imports
from dgov.cli.swarm import think, convo, watch  # NEW

# In cli.add_command() calls
cli.add_command(think)
cli.add_command(convo)
cli.add_command(watch)
```

**Rationale:** Matches existing pattern (`dag`, `mission`, `monitor` are all orchestration-level commands).

#### Option B: Subcommands under `dgov swarm`

Create a new parent group:

```python
# In cli/__init__.py
from dgov.cli.swarm import swarm  # NEW (group containing think/convo/watch)

cli.add_command(swarm)
```

Then users run:
```bash
dgov swarm think -p "Analyze the parser bug..."
dgov swarm convo --with worker-abc123
dgov swarm watch --on pane_xyz789
```

**Rationale:** Groups related functionality; scales better if more swarm commands added later.

#### Option C: Subcommands under `dgov pane`

Extend existing `pane` group:

```python
# In cli/cli/pane.py
@pane.command("think")
def pane_think(...): ...

@pane.command("convo")
def pane_convo(...): ...

@pane.command("watch")
def pane_watch(...): ...
```

**Rationale:** Keeps everything pane-centric; but `think/convo/watch` are higher-level than individual pane management.

### 2.3 Recommended Approach

**Go with Option A (top-level commands)** because:
1. Consistent with `dag`, `mission`, `monitor` which are also orchestration-level
2. Simpler mental model: `dgov think`, `dgov convo`, `dgov watch`
3. Easier to discover via `dgov --help`
4. Avoids nesting depth creep

---

## 3. Implementation Blueprint

### 3.1 File Structure

```
src/dgov/cli/
├── __init__.py          # Register swarm commands
├── swarm.py             # NEW: think/convo/watch implementations
│   ├── think.py         # Optional: separate file for think logic
│   ├── convo.py         # Optional: separate file for convo logic
│   └── watch.py         # Optional: separate file for watch logic
```

### 3.2 Minimal `swarm.py` Skeleton

```python
"""Swarm coordination commands: think, convo, watch."""

from __future__ import annotations

import click

from dgov.cli import SESSION_ROOT_OPTION


@click.group()
def swarm():
    """Swarm coordination: think, convo, watch."""
    pass


@swarm.command("think")
@click.option("--agent", "-a", default=None, help="Reasoning agent")
@click.option("--prompt", "-p", required=True, help="Task for reasoning")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root",
)
@SESSION_ROOT_OPTION
def think(agent, prompt, project_root, session_root):
    """Launch a reasoning/thinking pane."""
    # Implementation: create_worker_pane with role="reasoner"
    # Emit think_started, think_concluded events
    pass


@swarm.command("convo")
@click.argument("participant_slugs", nargs=-1)
@click.option("--agent", "-a", default=None, help="Conversation host agent")
@click.option("--prompt", "-p", help="Conversation topic/initiator prompt")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root",
)
@SESSION_ROOT_OPTION
def convo(participant_slugs, agent, prompt, project_root, session_root):
    """Start a multi-agent dialogue."""
    # Implementation: create conversation host pane
    # Link participants via parent_slug
    # Emit convo_* events
    pass


@swarm.command("watch")
@click.argument("target_slug")
@click.option("--agent", "-a", default=None, help="Watcher agent")
@click.option("--events", "-e", multiple=True, help="Events to monitor")
@click.option(
    "--project-root",
    "-r",
    default=".",
    envvar="DGOV_PROJECT_ROOT",
    help="Project root",
)
@SESSION_ROOT_OPTION
def watch(target_slug, agent, events, project_root, session_root):
    """Monitor a pane for specific events."""
    # Implementation: create observer pane
    # Subscribe to target's event stream
    # Emit watch_* events
    pass
```

### 3.3 Event Registration Locations

**File: `src/dgov/persistence.py`**

Add to `VALID_EVENTS` frozenset (around line 30):

```python
VALID_EVENTS = frozenset(
    {
        # ... existing events ...
        # Swarm expansion events
        "think_started",
        "think_step",
        "think_concluded",
        "think_aborted",
        "convo_started",
        "convo_message_sent",
        "convo_message_received",
        "convo_turn",
        "convo_ended",
        "convo_escalated",
        "watch_started",
        "watch_event",
        "watch_alert",
        "watch_summary",
        "watch_stopped",
    }
)
```

### 3.4 Role Extensions (Optional)

**File: `src/dgov/persistence.py`**

Extend `WorkerPane.role` defaults or add constants:

```python
# Near PANE_TIER class definition
class PANE_ROLE:
    WORKER = "worker"
    LT_GOV = "lt-gov"
    REASONER = "reasoner"       # For think
    DIALOGUE_PARTICIPANT = "dialogue_participant"  # For convo
    OBSERVER = "observer"        # For watch
```

Update `_validate_state()` if adding new roles (currently only validates states, not roles).

---

## 4. Testing Strategy

### 4.1 Test Files to Extend

Based on test mapping in AGENTS.md:
- `tests/test_dgov_state.py` → Add tests for new events in VALID_EVENTS
- `tests/test_persistence_pane.py` → Test parent_slug/tier_id relationships
- `tests/test_cli_admin.py` → Add CLI integration tests for think/convo/watch

### 4.2 Test Cases

**For `think`:**
```python
def test_think_emits_events():
    """Verify think command emits think_started/think_concluded."""
    # Create think pane, verify events logged to state.db
    pass

def test_think_role_assignment():
    """Verify reasoner role set correctly."""
    pass
```

**For `convo`:**
```python
def test_convo_creates_host_and_participants():
    """Verify conversation creates host pane + linked participants."""
    pass

def test_convo_hierarchy():
    """Verify parent_slug relationships established."""
    pass
```

**For `watch`:**
```python
def test_watch_subscribes_to_events():
    """Verify watcher subscribes to target's event stream."""
    pass

def test_watch_alert_on_threshold():
    """Verify watch_alert emitted when threshold breached."""
    pass
```

---

## 5. Dependencies and Cross-Cuts

### 5.1 Modules to Import

From `swarm.py`:
```python
from dgov.lifecycle import create_worker_pane  # Reuse existing pane creation
from dgov.persistence import emit_event       # Emit swarm events
from dgov.agents import get_default_agent     # Resolve agent IDs
from dgov.tmux import create_utility_pane     # Utility panes for watchers
```

### 5.2 Existing Patterns to Leverage

1. **`monitor_cmd`** (`src/dgov/cli/monitor_cmd.py`): Similar "watch over panes" semantics
2. **`dag` command** (`src/dgov/cli/dag_cmd.py`): Multi-step orchestration pattern
3. **`mission_cmd`** (`src/dgov/cli/mission_cmd.py`): Higher-level task coordination
4. **`get_child_panes()`** (`src/dgov/persistence.py`): Already handles parent-child queries

### 5.3 Potential Conflicts

- **None identified**: Swarm commands operate at orchestration layer, distinct from pane lifecycle management
- **Event naming**: Ensure no collision with existing events (checked against 56 current events)

---

## 6. Migration Path

### Phase 1: Infrastructure (Week 1)
- [ ] Add new events to `VALID_EVENTS` in `persistence.py`
- [ ] Create `src/dgov/cli/swarm.py` skeleton
- [ ] Register commands in `cli/__init__.py`
- [ ] Add basic unit tests

### Phase 2: Implementation (Week 2-3)
- [ ] Implement `think` command with reasoning lifecycle
- [ ] Implement `convo` command with dialogue orchestration
- [ ] Implement `watch` command with event subscription
- [ ] Add integration tests

### Phase 3: Polish (Week 4)
- [ ] CLI help text and documentation
- [ ] Error handling and edge cases
- [ ] Performance optimization for high-frequency watch events
- [ ] User-facing examples in docs

---

## 7. Open Questions

1. **Should `think` have its own state?**
   - Recommendation: No, use `active` with metadata field `phase: "thinking"`

2. **Should `convo` support async vs sync modes?**
   - Recommendation: Start with sync (blocking until dialogue ends), add async later

3. **Should `watch` be persistent across sessions?**
   - Recommendation: Yes, store watcher pane as regular pane with `role="observer"`

4. **Need for swarm-specific configuration?**
   - Recommendation: Store in pane metadata, no separate config file needed

---

## 8. Conclusion

The dgov architecture is well-suited for swarm expansion:
- ✅ Event log is extensible via `VALID_EVENTS` frozenset
- ✅ CLI registration pattern is consistent and documented
- ✅ Pane hierarchy (`parent_slug`, `tier_id`) supports swarm relationships
- ✅ Existing modules (`lifecycle`, `persistence`, `agents`) provide reusable primitives

**Recommended next step:** Implement Phase 1 infrastructure changes, then validate with minimal working prototypes of each command before full implementation.
