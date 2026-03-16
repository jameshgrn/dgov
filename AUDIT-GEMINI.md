# dgov Codebase Audit Report — Phase 3

**Auditor:** Gemini CLI
**Date:** March 15, 2026
**Scope:** `src/dgov/` (all modules)

## Executive Summary
The `dgov` codebase is a mature, robust CLI tool for orchestrating AI agents. It leverages git worktrees and tmux panes to provide isolated, observable environments for workers. The state management is cleanly handled via SQLite with explicit transition validation. The most significant findings involve redundant abstractions between the backend and tmux layers, minor code duplication, and opportunities to refine prompt context delivery.

---

## 1. Plumbing Issues & Dead Code

### Redundant Backend Abstraction (Medium)
The `WorkerBackend` protocol in `src/dgov/backend.py` and its `TmuxBackend` implementation are almost strictly 1:1 wrappers around `src/dgov/tmux.py`.
- **Finding:** Every method in `TmuxBackend` simply imports and calls a function from `dgov.tmux`.
- **File:** `src/dgov/backend.py`
- **Recommendation:** Merge `TmuxBackend` into `src/dgov/tmux.py` or fold the logic into `backend.py`. The abstraction exists for "future proofing" (Docker/SSH) but currently adds unnecessary indirection.

### Duplicated ANSI Stripping (Low)
- **Finding:** The `_ANSI_RE` regex and `_strip_ansi` function are duplicated identically in two places.
- **Files:** `src/dgov/status.py:L21` and `src/dgov/dashboard.py:L26`
- **Recommendation:** Move to a shared utility module (e.g., `src/dgov/art.py` or a new `utils.py`).

### Overlapping CLI Commands (Low)
- **Finding:** `pane message` and `pane respond` perform identical tasks.
- **File:** `src/dgov/cli/pane.py:L487, L503`
- **Recommendation:** Consolidate into a single `pane send` or `pane input` command.

### Hardcoded Lists (Low)
- **Finding:** Several modules use hardcoded lists for agent-specific logic that could be moved to the registry.
    - `_AGENT_COMMANDS` in `src/dgov/waiter.py:L226`
    - `_AGENT_COLORS` in `src/dgov/tmux.py:L142`
    - `agent_cmds` in `src/dgov/status.py:L166`
- **Recommendation:** Move these properties into the `AgentDef` dataclass in `src/dgov/agents.py`.

---

## 2. Loose Ends

### Heuristic Stabilization (Medium)
- **Finding:** The `stable` DoneStrategy (used by `cline` and `crush`) relies on output remaining unchanged for a set duration.
- **File:** `src/dgov/waiter.py:L186`
- **Risk:** This is inherently flaky if an agent pauses to "think" or waits for a slow network response.
- **Recommendation:** Encourage the use of `signal` (done-signal file) for all agents where possible.

### Missing TODOs (Low)
- **Finding:** There are zero `TODO`, `FIXME`, or `XXX` markers in the entire `src/dgov/` directory.
- **Observation:** While this suggests a high degree of completion, it may also mean that known technical debt is not being tracked in-code.

---

## 3. Context Management

### Prompt Delivery via Files (Medium)
- **Finding:** `build_launch_command` writes prompts to `.dgov/prompts/*.txt` and uses a shell snippet (`cat ... && rm ...`) to pass them to the agent.
- **File:** `src/dgov/agents.py:L333`
- **Impact:** This avoids shell escaping and ARG_MAX issues, but leaves prompt files on disk if the shell snippet fails before reaching the `rm` command.
- **Recommendation:** Ensure the `prune` command also cleans up the `.dgov/prompts/` directory.

### Environment Variable Bloat (Low)
- **Finding:** The full prompt is passed as `DGOV_PROMPT` in the environment when triggering hooks.
- **File:** `src/dgov/lifecycle.py:L164`
- **Impact:** Very large prompts may hit environment size limits or be truncated.
- **Recommendation:** Pass the path to the prompt file instead of the content for hooks.

### Path Rewriting (High Signal)
- **Observation:** `dgov` correctly rewrites absolute paths in prompts so agents target the worktree instead of the main repository.
- **File:** `src/dgov/lifecycle.py:L182`
- **Note:** This is a critical safety and correctness feature that is well-implemented.

---

## 4. Condensation Opportunities

### Module Consolidation (Medium)
- **`src/dgov/backend.py` + `src/dgov/tmux.py`**: As noted above, these are redundant.
- **`src/dgov/strategy.py`**: Could potentially be merged into `src/dgov/agents.py` as it primarily handles agent selection and prompt formatting.
- **`src/dgov/lifecycle.py`**: The `_setup_and_launch_agent` function is quite large; part of the environment setup could be moved to `agents.py` or a dedicated `env.py`.

---

## 5. State Management

### State Update Side-Effects (Medium)
- **Finding:** `list_worker_panes` calls `_is_done`, which can update the persistent SQLite state (e.g., transition from `active` to `done`).
- **File:** `src/dgov/status.py:L146`
- **Impact:** A "read" operation causing a "write" is generally discouraged, though here it ensures the dashboard remains fresh.
- **Recommendation:** Separate the "check for completion" logic from the "list panes" logic to avoid unexpected DB writes during monitoring.

### Robust Transitions (High Signal)
- **Observation:** The use of a transition table (`VALID_TRANSITIONS`) and atomic `UPDATE` statements in `persistence.py` is excellent and prevents race conditions between the dashboard and CLI.
- **File:** `src/dgov/persistence.py:L371`

---

## Prioritized Action List

### High Priority
1. **Consolidate Backend/Tmux:** Merge `backend.py` wrappers into `tmux.py` to reduce indirection.
2. **Hook Prompt Safety:** Stop passing the full prompt text in `DGOV_PROMPT` env var; pass a file path instead.

### Medium Priority
1. **Deduplicate Utilities:** Move `_strip_ansi` and other shared helpers to a utility module.
2. **Clean up Prompts:** Update `prune_stale_panes` to clean the `.dgov/prompts/` directory.
3. **Registry Migration:** Move hardcoded agent lists (commands, colors) into the agent registry TOML/dataclass.

### Low Priority
1. **CLI Cleanup:** Consolidate `pane message` and `pane respond`.
2. **Dashboard Refactor:** Split `dashboard.py` into `data_fetcher.py` and `ui_render.py` for better maintainability.
