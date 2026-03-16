# Codex Adversarial Review of Dashboard DAG

## Findings

### 1. High
- Task: `T1a`, `T2a`, `T4a`
- Description: The Rich migration spec does not require escaping or stripping untrusted pane/event text before rendering. `read_events()` returns flattened arbitrary event payload fields, and current dashboard activity strings are populated from `progress/<slug>.json` without sanitization. In a Rich UI, raw markup or control sequences can spoof status, corrupt layout, or inject clickable links. Sources: [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L83), [dashboard.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/dashboard.py#L189), [persistence.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/persistence.py#L82).
- Fix: Add an explicit sanitization requirement to the DAG: strip ANSI/control sequences, escape Rich markup, and render user-controlled strings via `Text` objects instead of markup-parsed strings. Add tests with malicious `progress.json` messages and event payloads.

### 2. High
- Task: `T1a`
- Description: The card spec is not implementable against the APIs it cites. The DAG requires line 3 to show `commit count + files changed (from pane record, NOT live git calls)`, but `list_worker_panes()` returns neither commit counts nor changed-file stats, and `get_pane()` / `all_panes()` only expose persisted pane metadata. The only current source for commit/file stats is the inspection path used in detail view, which does live git work. Sources: [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L88), [status.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/status.py#L168), [persistence.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/persistence.py#L350), [dashboard.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/dashboard.py#L255).
- Fix: Change the DAG spec to either remove that card line for MVP or add a preceding task that persists cached diff stats into pane metadata with a defined producer API.

### 3. High
- Task: `T3b`
- Description: The erosion-model extension is specified in a way that does not match the vendored code. `ErosionModel.step()` uses scalar `self.K` and scalar `self.uplift` across the whole grid. A subclass cannot implement per-region parameters simply by “apply per-region parameters before calling `super().step()`”; that still runs one global erosion/uplift pass. Making this work requires rewriting the erosion loop, not a small subclass. Assigning this to `pi` is a skill mismatch. Sources: [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L160), [erosion.py](/Users/jakegearon/projects/writing/src/scilint/tui/erosion.py#L96).
- Fix: Rewrite the DAG so `T3b` is either dropped from this phase or recast as a substantial numerical-model change owned by a stronger agent, with permission to modify the erosion algorithm and with dedicated tests.

### 4. High
- Task: `T0a`, `T0b`, `T2a`, `T3b`, `T4a`
- Description: The dependency graph is wrong in multiple places. `T0b` is not independent of `T0a` because the vendored model imports `rich.text.Text`. `T2a` and `T3b` both edit `src/dgov/terrain.py` from the same base but are placed in separate branches with no dependency edge, so the DAG is manufacturing merge risk. `T4a` tests `DgovErosionModel` but does not depend on `T3b`, the task that creates it. The merge section also claims “no conflicts possible” where conflicts are plausible by construction. Sources: [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L8), [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L114), [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L160), [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L181), [erosion.py](/Users/jakegearon/projects/writing/src/scilint/tui/erosion.py#L8).
- Fix: Add `T0b -> T0a`, add `T4a -> T3b`, and either make `T3b -> T2a` or merge `T2a` and `T3b` into one terrain task.

### 5. High
- Task: `T1a`, `T3a`
- Description: The rollout and rollback story is not safe. The v2 spec removes the current y/n confirmation barrier for `m=merge` and `x=close`, so destructive actions become single-keystroke operations. The CLI fallback only catches `ImportError`; if `dashboard_v2` imports successfully and then fails at startup or during the first render, the default command path still crashes. The rollback section says fallback is immediate, but that is only true if the operator already knows to set `DGOV_DASHBOARD=v1`. Sources: [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L95), [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L141), [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L224), [dashboard.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/dashboard.py#L741), [admin.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/cli/admin.py#L216).
- Fix: Keep v1 as the default until v2 soaks, preserve confirmation for merge/close in v2, and change the DAG to require guarded startup with a clear fallback path for non-import startup failures.

### 6. Medium
- Task: `T0c`
- Description: The task spec does not match the current dashboard structure. There is no `_draw_panes` function, and the table already renders `activity`, not raw `last_output`. The only log-tail rendering path is `_draw_prompt_preview()`. As written, this task tells the worker to edit a nonexistent function and solve the wrong display path. Sources: [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L54), [dashboard.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/dashboard.py#L399), [dashboard.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/dashboard.py#L443).
- Fix: Rewrite `T0c` against the real functions: `_draw_prompt_preview()` for log filtering and `format_row()` / `_draw_pane_row()` if phase dots are required.

### 7. Medium
- Task: `T1a`
- Description: The new dashboard’s data thread is specified as a read loop, but `list_worker_panes()` is not a pure read. It calls `_is_done()` for active panes, which can update persistent state. That matters for race analysis and for test design: a polling UI is also a writer. Sources: [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L87), [status.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/status.py#L198).
- Fix: Amend the DAG to either accept that side effect explicitly and test it, or add a pure snapshot API before building a higher-frequency Rich dashboard on top of it.

### 8. Medium
- Task: `T4a`
- Description: The test spec is too shallow for the failure modes introduced by the new design. It does not cover CLI fallback routing, non-TTY stdin, `select.select()` / raw-mode failures, `Rich Live` loop teardown, terrain hide-on-narrow-width behavior, terrain-step exceptions, or preservation of merge/close safeguards. Those are the parts most likely to fail in practice, and they are also the least testable if `dashboard_v2.py` stays monolithic. Sources: [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L181), [dashboard.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/dashboard.py#L622), [admin.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/cli/admin.py#L216).
- Fix: Expand `T4a` to require tests for fallback and teardown paths, and require `T1a` to split pure helpers from terminal I/O so those paths are unit-testable.

### 9. Medium
- Task: `T1a`, `T2a`, `T3a`
- Description: Error handling is underspecified where the new implementation is most brittle. The DAG says nothing about what to do if Rich import succeeds but terminal startup fails, if `stdin` is not a TTY, if `select.select()` raises, if `termios` restoration fails, or if `terrain_model.step()` / `render_terrain()` raises during the live loop. Current curses code contains localized exception handling around detail fetches and action failures; the new spec drops that discipline. Sources: [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L83), [DAG-DASHBOARD.md](/Users/jakegearon/projects/dgov/DAG-DASHBOARD.md#L119), [admin.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/cli/admin.py#L216), [dashboard.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/dashboard.py#L176), [dashboard.py](/Users/jakegearon/projects/dgov/.dgov/worktrees/review-dag-codex/src/dgov/dashboard.py#L963).
- Fix: Add explicit DAG requirements for degraded modes: v1 fallback on startup failure, no-input mode when stdin is unusable, try/except around terrain updates with last-good-frame retention, and visible error banners instead of hard exits.

## Verdict

`NO-GO`

The DAG is not ready to execute as written. The main blockers are not polish issues; they are spec/code mismatches, a broken dependency graph, unsafe rollout assumptions, and underdefined failure handling in the new Rich path. Rewrite the DAG first, then execute.
