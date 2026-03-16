# Review: dgov Phase 3 (v2) Design

**Reviewer:** Gemini CLI
**Date:** March 16, 2026
**Subject:** Mission Primitives, Strategy-Based Done Detection, and Modularization

## 1. Well-Designed / Ship As-Is

- **Strategy-Based Done Detection (`DoneStrategy`):** Moving away from a "one-size-fits-all" stabilization heuristic is the single most important reliability improvement in this design. Explicitly allowing agents to signal completion via exit codes, `.done` files, or specific patterns correctly maps to how different CLI agents actually behave.
- **Mission Primitive (`run_mission`):** The transition from manual polling to a declarative state machine is the right move for UX. The `PENDING -> RUNNING -> WAITING -> REVIEWING -> MERGING -> COMPLETED` flow is logical and matches the "governor" philosophy.
- **Slim DB Queries:** Implementing `list_panes_slim` and separating prompt storage from metadata prevents the dashboard from choking on large prompts. This fixes a known performance bottleneck.
- **Hook Priority:** The `.dgov/hooks` vs `.dgov-hooks` vs `~/.dgov/hooks` hierarchy is idiomatic and provides a clear path for both repo-specific and user-specific automation.

## 2. Gaps & Missing Edge Cases

- **Mission Interruption:** The `run_mission` loop is synchronous and blocking. If a user `Ctrl-C`s a mission, there is no documented "recovery" or "resume" state for the mission itself. The worker pane persists, but the mission state-machine is lost.
- **Merge Atomicity Failure:** `merger.py` uses a `stash push -> update-ref -> reset --hard -> stash pop` sequence. If `reset --hard` fails (e.g., due to file locks), the branch pointer has already advanced, leaving the working tree in an inconsistent state. This is not a true transactional merge.
- **Multi-File Mission Collisions:** Missions are designed for single-shot tasks. If two missions are run in parallel and touch the same files, `run_preflight` only checks for *existing* panes, not *queued* missions. There is a race condition between preflight and worktree creation.
- **Done Strategy False Positives:** The `stable` strategy remains fundamentally risky. If an agent pauses to "think" for 16 seconds (threshold is 15s), dgov will declare it done. The design lacks a "heartbeat" or "active thinking" signal from the agent.

## 3. Overengineered Components

- **Agent Specialized Detection in `DoneStrategy`:** While the concept is good, the implementation in `done.py` still falls back to stabilization and commit-counting far too easily. We should force agents to declare a *primary* strategy and only use others as explicit fallbacks.
- **Mission Policy Complexity:** Options like `max_retries` and `escalate_to` inside `MissionPolicy` add branching logic to an already complex state machine. This should be handled by an external `Orchestrator` or `Batch` runner, keeping the `Mission` primitive focused strictly on the lifecycle of one task.

## 4. Concrete Suggestions for Simplification

- **Stateless Missions:** Instead of a blocking `run_mission` function, implement a `MissionManager` that advances the state of all active missions in a single non-blocking tick. This allows the CLI to remain responsive and simplifies the recovery of interrupted missions.
- **Merge via `git worktree` instead of `stash`:** To make merges truly transactional, perform the merge in a temporary "merger" worktree. If it succeeds, advance the main branch. If it fails, delete the temp worktree. This avoids touching the governor's working tree (and avoids the stash/pop risk) until the merge is guaranteed.
- **Collapse `backend.py`:** As noted in the audit, `TmuxBackend` is a 1:1 wrapper. Remove the protocol until a second backend (Docker/SSH) is actually being implemented.

## 5. Risk Assessment

| Phase | Risk | Impact | Mitigation |
|-------|------|--------|------------|
| **Missions** | Interrupted missions leave orphaned worktrees and "ghost" events. | High | Implement a `mission resume` or `mission repair` command. |
| **Done Detection**| Stabilization false-positives clobbering long-running tasks. | Medium | Increase default stabilization to 60s; encourage `.done` files. |
| **Modularization**| Regression in `lifecycle.py` during the split. | High | Comprehensive integration tests for every split module before merging. |
| **Merge** | `reset --hard` failure during plumbing merge leaving main broken. | Critical | Use the temporary worktree merge strategy suggested above. |

## 6. Priority Ordering

1. **Transactional Merge (P0):** Fix the `reset --hard` risk before adding any more automation (Missions).
2. **Done Strategy (P1):** Deploy the new strategies to stop the stabilization flakiness.
3. **Mission Primitive (P1):** Add the `mission` command but keep it simple.
4. **Modularization (P2):** Split the files only after the core logic is stable.
5. **Dashboard Actions (P3):** Interactive dashboard is nice-to-have, not a core reliability requirement.

## Final Verdict
The design is a strong evolution of the Phase 2 baseline, but it prioritizes "Missions" (features) over "Transactional Safety" (correctness). **The merge path is the most dangerous part of dgov v2.** Until the stash/reset sequence is replaced with a zero-side-effect temporary worktree merge, the tool is not safe for high-concurrency automated missions.
