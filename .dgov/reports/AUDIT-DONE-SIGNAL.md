## P0 (will crash or hang forever)

- **Interactive agents + `_wrap_done_signal` never firing**
  - **What happens**: `_setup_and_launch_agent` always wraps the launch command (including interactive TUIs like `claude`, `cursor-auto`) via `_wrap_done_signal`, which emits a shell snippet: `if <agent_cmd>; then touch done; else echo $? > done.exit; fi`. For interactive agents, `<agent_cmd>` stays running as long as the TUI is open. The `then/else` branch only executes after the agent process exits and returns control to the shell. While the user (or cursor) keeps the TUI open, the wrapper does *nothing* and no done/exit file is written.
  - **Crash/hang condition**: With `DoneStrategy.type == "api"`, `_is_done` explicitly skips commit and stabilization signals and only checks:
    1. done file
    2. exit file
    3. pane liveness
    4. (then returns False immediately for `stype == "api"`)
    If cursor-auto never runs `dgov worker complete` *and* keeps the agent process alive (TUI stays open), then:
    - done file does not exist (wrapper has not executed).
    - exit file does not exist.
    - pane is alive, so liveness branch doesn't mark it abandoned.
    - `stype == "api"` forces an early `return False`.
    Result: the pane remains forever "active" and `wait_worker_pane` never finishes (modulo its external timeout). From the waiter’s perspective this is a genuine hang until the user (or some out-of-band force) kills the pane or times out the wait.
  - **Why this is P0**: For any workflow that expects the agent to run "under its own steam" (governor waiting on `wait_worker_pane`), a missing `dgov worker complete` in combination with a still-alive interactive agent means the wait loop never observes completion via `_is_done`. In practice, users see a "stuck" pane and a blocked governor until the timeout/error path fires or they manually intervene.

- **API strategy + no external timeout on pane itself**
  - `wait_worker_pane` enforces a wall-clock timeout (default 600s) and raises `PaneTimeoutError` when exceeded, but the interactive pane itself has no intrinsic timeout. If callers wrap `wait_worker_pane` in a longer-running higher-level orchestrator or forget to use a finite timeout, the session can hang effectively forever because `_is_done` never returns True in the api+alive+no-signal case.


## P1 (incorrect detection)

- **Done-signal vs. live interactive pane**
  - `_is_done` treats the done file as the highest-priority signal:
    - It unconditionally sets the pane state to `"done"` (possibly with `force=True` if previously `"abandoned"`), writes `_done_reason="done_signal"`, and returns `True` without checking pane liveness.
  - This means a scenario is possible where:
    - The interactive agent is still running in the tmux pane (e.g., user manually touched the done file, or a hook wrote it early).
    - `_is_done` sees the done file and declares the worker "done", leaving a live agent process in the background.
  - For interactive agents, this is semantically incorrect: "done" is being inferred from a file that can be written by *any* process (hooks, nudges, manual `signal_pane`), even while the agent TUI is still active. The state machine does not enforce that "done" implies "no running agent".

- **Commit-based completion with still-running agent (non-api strategies)**
  - For non-`api` strategies that allow commit-based completion, `_is_done` will:
    - Detect new commits.
    - If the agent is still running, start a 30s grace period and then:
      - Mark the pane `"done"`, emit `pane_done`, and touch the done file.
    - This can race with any interactive session where the agent is still active and still doing work / further commits:
      - If `_stable_state` is missing (e.g., some code paths), it logs a warning and *immediately* declares done even though the agent is still running, to avoid "blocking forever".
  - Result: pane state can flip to `"done"` while the agent continues to operate, which is logically inconsistent and can cause incorrect higher-level decisions (e.g., governor thinking the work is finished while more commits are being made).


## P2 (missing fallback)

- **API strategy has no non-signal fallback**
  - `_resolve_strategy` defaults to `"api"` when no `DoneStrategy` is given. For `stype == "api"`, `_is_done`:
    - Always checks for done/exit signal files.
    - Always updates abandonment for dead panes (10s grace).
    - Then *short-circuits* with `return False`, explicitly skipping:
      - Commit detection.
      - Output stabilization.
      - Circuit breaker.
  - For interactive agents whose lifecycle is controlled by an external TUI (like Cursor) and which are supposed to report completion via an API call (`dgov worker complete`), this means:
    - There is no built-in fallback if the API call never happens and the pane remains alive.
    - There is no "stabilization" heuristic that says "output hasn't changed for N seconds, agent is probably idle, mark done".
    - There is no commit-based heuristic that says "commits landed, treat as done even if TUI is still attached".
  - Combined with `_wrap_done_signal` only firing *after* the agent exits, api+interactive agents have a single critical dependency: a cooperating agent that correctly writes the done/exit signal. When that cooperation fails, the system intentionally ignores all other potential evidence of "done-ness" and has no built-in recovery path beyond:
    - eventual pane death (abandonment path), or
    - external timeout in `wait_worker_pane`, or
    - manual `signal_pane` / `nudge_pane`.

- **No race-handling between done signal and liveness**
  - There is no explicit race-resolution logic when:
    - The done/exit file appears *while* the agent is still alive.
  - `_is_done` always prefers the signal file and never re-checks liveness before declaring `"done"`/`"failed"`. In practice, this can produce transiently inconsistent states (live agent, "done" worker) that the rest of the system likely tolerates but doesn't explicitly recognize as a race that should be resolved (e.g., by killing the agent or deferring "done" until liveness flips).

