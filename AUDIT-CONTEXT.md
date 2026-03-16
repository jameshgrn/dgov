# AUDIT: Context Management & Communication Architecture of dgov

## Executive Summary
dgov implements a **loose-coupling, side-channel dominant** architecture for context management. The Governor (CLI/Dashboard) orchestrates Workers (Agent CLIs in tmux panes) primarily through:
1. **Initial Injection:** Environment variables and rewritten prompts.
2. **Side-Channel Signaling:** Filesystem-based "done" signals and a SQLite-backed event journal.
3. **Unstructured Capture:** Log tailing and tmux pane capture for status and "nudge" interactions.

While effective for decoupling, the architecture suffers from high latency in state reconciliation, "blind spots" in worker progress (before commits/completion), and significant context waste during escalation due to full-prompt reconstruction.

---

## Current Architecture Flow (ASCII)

```text
       [ GOVERNOR (main) ]
               |
               +--- (1) Dispatch: create_worker_pane()
               |       |-> git worktree add (slug branch)
               |       |-> tmux new-window -d (slug pane)
               |       |-> env export (DGOV_SLUG, etc.)
               |       |-> prompt rewrite (path mapping)
               |       |-> build_launch_command() (sh wrapper)
               |       L-> AGENT CLI (claude/pi/etc.)
               |
               +--- (2) Monitor: waiter.py / status.py
               |       |-> SQLite state.db (polling)
               |       |-> .dgov/done/<slug> (signal file)
               |       |-> git branch (commit detection)
               |       |-> .dgov/logs/<slug>.log (log tail)
               |       L-> tmux capture-pane (ANSI strip)
               |
               +--- (3) Interact: responder.py / waiter.py
               |       |-> nudge_pane() (send keys "Are you done?")
               |       L-> auto_respond() (regex match -> send keys)
               |
               +--- (4) Integrate: merger.py / inspection.py
               |       |-> review_worker_pane() (git diff stat)
               |       |-> _commit_worktree() (auto-commit)
               |       L-> _plumbing_merge() (in-memory tree)
               |
               L--- (5) Teardown: lifecycle.py
                       |-> tmux kill-pane
                       L-> git worktree remove
```

---

## Detailed Findings

### 1. GOVERNOR → WORKER CONTEXT FLOW
- **Prompt Transport:** Uses a robust "temp file -> read -> delete" shell snippet to bypass CLI argument limits and shell escaping issues (`agents.py:build_launch_command`).
- **Context Injection:** Injects foundational context via `DGOV_*` environment variables and absolute path rewriting in prompts.
- **Lost Context:** Environment variables are stripped for `claude` (Claude Code) but kept for others. Worktree creation happens before the agent starts, so the agent "sees" the worktree as a fresh start, losing transient IDE state (unsaved files, terminal history).
- **Boilerplate:** `pi` prompts are heavily structured into numbered steps (`strategy.py:_structure_pi_prompt`), adding significant token overhead but improving mechanical reliability.

### 2. WORKER → GOVERNOR STATE FLOW
- **Done Detection:** Multi-modal (done-signal file, exit code file, new commits, output stabilization). Done-signal files are the most authoritative but rely on shell `&&` wrappers which can fail if the agent process is killed or panics internally.
- **Latency:** `wait_worker_pane` polls every 3 seconds by default. Stabilization requires 15s of no output. There is a "dead time" between a worker finishing and the governor noticing.
- **Information Gap:** Governor captures the *last* output line but lacks a "high-water mark" of progress. `pi-extensions` attempt to bridge this but currently appear as skeletons.

### 3. ESCALATION CONTEXT TRANSFER
- **Reconstruction:** `recovery.py:retry_context` reconstructs failure context by reading log tails and exit codes.
- **Waste:** The original prompt is concatenated with the failure context. This often forces the second agent to re-read files and re-run analysis that the first agent already performed.
- **Partial Progress:** No mechanism exists to transfer the *mental state* or *checkpoint* of the previous agent beyond the git history.

### 4. REVIEW PROTOCOL
- **Protocol:** `inspection.py:review_worker_pane` is purely based on `git diff --stat` and `git diff --name-only`. It lacks semantic analysis or test-result integration.
- **Protected Files:** `PROTECTED_FILES` check is robust but happens *after* the work is done. `merger.py:_restore_protected_files` is a safety net that amends the last commit to undo damage to protected files.

### 5. INTER-WORKER AWARENESS
- **Isolation:** Workers are strictly isolated in their own worktrees.
- **Freshness:** `status.py:_compute_freshness` provides a "stale" warning if workers touch the same files, but this is a "pull" check by the governor, not a "push" conflict detection between workers.

### 6. CONTEXT WINDOW BUDGET
- **Overhead:** `DGOV_PROMPT` in `worktree_created` hook and the structured prompt wrapper for `pi` consume ~200-500 tokens of the context window.
- **Redundancy:** Agents often re-read `CLAUDE.md` and `pyproject.toml` on every run because they lack a "warmed" context.

### 7. STRUCTURED vs UNSTRUCTURED COMMUNICATION
- **Unstructured Heavy:** Much of the "liveness" detection relies on scraping `tmux` output or tailing text logs. Regex-based question detection (`waiter.py:_BLOCKED_PATTERNS`) is fragile and subject to ANSI noise.
- **Structured Gaps:** No standard protocol for "Phase" reporting (e.g., "Researching", "Editing", "Testing").

### 8. STATE CONSISTENCY
- **Divergence:** `tmux` panes can die without the SQLite state updating. `status.py:list_worker_panes` reconciles this by checking `alive` status during listing.
- **Atomic Transitions:** `persistence.py:update_pane_state` uses an atomic `UPDATE` with state transition validation, preventing many classes of race conditions.

---

## Communication Primitives Roadmap

### 1. Typed Task Packets
- **Location:** `src/dgov/models.py` & `src/dgov/lifecycle.py`
- **Proposal:** Replace raw prompt strings with a `TaskPacket` object containing `intent`, `scope` (file list), `constraints`, and `checkpoints`.
- **Benefit:** Allows `pi` and `claude` to receive pre-filtered context without full codebase scans.

### 2. Progress Packets
- **Location:** `src/dgov/pi-extensions/` & `src/dgov/status.py`
- **Proposal:** Implement a standardized `DGOV_PROGRESS` pipe/file format where workers can emit `{"phase": "test", "step": "3/10", "confidence": 0.8}`.
- **Benefit:** Dashboard can show a progress bar instead of just "working...".

### 3. Escalation Packets
- **Location:** `src/dgov/recovery.py`
- **Proposal:** Instead of concatenating strings, pass a structured `Handoff` object that includes the *summary of findings* and *failed hypotheses* from the previous agent.
- **Benefit:** Prevents agents from repeating known-bad paths.

### 4. Advisory Channel
- **Location:** `src/dgov/persistence.py` (New table `advisory`)
- **Proposal:** A shared "Shared Memory" for parallel workers. If Worker A finds a bug in a shared utility, it posts an advisory. Worker B checks advisories before editing the same area.
- **Benefit:** Reduces merge conflicts and architectural drift between parallel tasks.

### 5. Attention Routing (Salience Maps)
- **Location:** `src/dgov/strategy.py`
- **Proposal:** When dispatching, the Governor computes a "salience map" (list of relevant files + their current SHA). The worker is instructed to *only* pay attention to these files unless the map expands.
- **Benefit:** Drastically reduces token usage for large-repo agents.
