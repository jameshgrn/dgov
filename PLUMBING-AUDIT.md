# Plumbing Audit

Scope: `src/dgov/backend.py`, `src/dgov/done.py`, `src/dgov/responder.py`, `src/dgov/waiter.py`, `src/dgov/lifecycle.py`, `src/dgov/tmux.py`

## Current message flow

```text
Governor command / DAG / mission
  |
  | dispatch
  |   lifecycle.create_worker_pane()
  |   -> backend.create_worker_pane()
  |   -> lifecycle._setup_and_launch_agent()
  |   -> backend.configure_worker_pane()
  |   -> backend.send_shell_command(export env)
  |   -> backend.send_shell_command(wrapped launch cmd)
  |   -> persistence.add_pane()
  |   -> emit_event("pane_created")
  v
tmux worker pane / agent process
  |
  | runtime interaction from governor
  |   waiter.interact_with_pane() -> backend.send_input()
  |   waiter.nudge_pane() -> backend.send_input("Are you done?") -> capture_output()
  |   responder.auto_respond() -> backend.send_input() or touch done/exit files
  |   lifecycle.close_worker_pane() -> _full_cleanup() -> backend.destroy()
  |   lifecycle.resume_worker_pane() -> create new pane -> relaunch same slug/worktree
  v
worker side signals
  |
  | clean exit
  |   done._wrap_done_signal() -> touch .dgov/done/<slug>
  |
  | nonzero exit
  |   done._wrap_done_signal() -> write .dgov/done/<slug>.exit
  |
  | commit-based completion
  |   done._is_done() -> git log/rev-list -> update_pane_state("done")
  |   -> emit_event("pane_done") -> touch done file
  |
  | pane death
  |   done._is_done() -> is_alive(False for >=10s)
  |   -> update_pane_state("abandoned") -> emit_event("pane_done")
  |
  | stable output
  |   done._is_done() -> compare captured output over time
  |   -> touch done file -> update_pane_state("done")
  |
  | repeated failure loop
  |   done._is_done() -> circuit-breaker fingerprint
  |   -> update_pane_state("failed")
  |   -> set_pane_metadata(circuit_breaker=true)
  |   -> emit_event("pane_circuit_breaker")
  v
Governor poll loop
  |
  | waiter._poll_once() / wait_for_slugs() / wait_all_worker_panes()
  |   -> capture_output()
  |   -> done._is_done()
  |   -> blocked detection / auto-response
  |   -> return method + state transitions
```

## Governor -> worker paths

- Dispatch: `src/dgov/lifecycle.py:330-475` creates worktree state, background pane, logging, env bootstrap, and launch command. The transport split is enforced in `src/dgov/lifecycle.py:217-239` and `src/dgov/lifecycle.py:282-311`.
- Backend transport boundary: `src/dgov/backend.py:48-61` defines `send_input()` for interactive text and `send_shell_command()` for shell bootstrap. `src/dgov/backend.py:161-169` maps those to tmux.
- tmux transport details: `src/dgov/tmux.py:69-101` sends shell commands via `send_command()` and interactive text via `send_text_input()`.
- Manual respond: `src/dgov/waiter.py:381-397` uses `send_input()`.
- Manual nudge: `src/dgov/waiter.py:400-444` uses `send_input()` then `capture_output()`.
- Auto-respond: `src/dgov/responder.py:118-181` sends `send_input()`, or writes done/exit files for synthetic completion/failure.
- Kill / close: `src/dgov/lifecycle.py:485-604` deletes done/log files, destroys the pane, removes the worktree, emits `pane_closed`, and removes the pane record.
- Resume: `src/dgov/lifecycle.py:607-743` destroys any stale pane, creates a replacement pane, relaunches the agent, and emits `pane_resumed`.

## Worker -> governor paths

- Done wrapper: `src/dgov/done.py:30-34` appends the success/failure side-channel to the launch command.
- Poll loop: `src/dgov/waiter.py:105-179` now captures live output once per tick, feeds it into `_is_done()`, then runs blocked detection and auto-response.
- Completion signals and priority: `src/dgov/done.py:141-159` defines the fixed order, and `src/dgov/done.py:168-321` applies it.
- Commit completion: `src/dgov/done.py:191-240` checks git history and emits `pane_done`.
- Liveness / abandonment: `src/dgov/done.py:242-266` marks dead panes abandoned after the grace period.
- Stable-output completion: `src/dgov/done.py:268-299`.
- Circuit breaker: `src/dgov/done.py:300-321`.
- Blocked prompt escalation: `src/dgov/waiter.py:158-171` and `src/dgov/responder.py:148-181`.

## Ranked issues

### 1. Critical: exit/commit wait paths were not refreshing output, so blocked detection and the circuit breaker were effectively disabled

- Before this change, `_poll_once()` only looked at `last_output`, but `last_output` was only refreshed inside the stabilization branch in `_is_done()`. That meant `exit` and `commit` strategies did not see fresh output at all.
- Affected code: old waiter flow in `src/dgov/waiter.py:125-171`; stable-only capture in `src/dgov/done.py:268-299`.
- Result: workers could block on prompts without emitting `pane_blocked`, auto-response would not fire, and the circuit breaker would miss loops on the default strategies.
- Fix implemented: `src/dgov/waiter.py:125-171` and `src/dgov/waiter.py:185-223` now capture live output once per poll and pass it into `_is_done()`.

### 2. High: done detection had real priority, but the API collapsed it to `bool`, forcing the waiter to guess

- `_is_done()` had six effective decision paths, but `_poll_once()` returned `signal_or_commit` for nearly everything and inferred `stable` from filesystem side effects.
- Affected code: `src/dgov/done.py:141-159`, `src/dgov/done.py:168-321`, `src/dgov/waiter.py:140-156`.
- Result: callers could not tell `exit_signal` from `commit`, `abandoned`, or `circuit_breaker`, which makes retries, observability, and operator debugging worse.
- Fix implemented: `_is_done()` now records `_done_reason`, and `_poll_once()` returns that exact method from `src/dgov/waiter.py:149-156`.

### 3. High: responder cooldown leaked across sessions because the key was only `(slug, pattern)`

- Affected code: old cooldown state in `src/dgov/responder.py:42-45` and old keying in `src/dgov/responder.py:94-110`.
- Result: two different repositories or session roots reusing the same slug could suppress each other's auto-response or escalation for 30 seconds.
- Fix implemented: `src/dgov/responder.py:42-45` and `src/dgov/responder.py:95-110` now scope cooldowns by `(session_root, slug, pattern)`.

### 4. Medium: the circuit breaker hashed only the last 5 lines, which aliased distinct failure states

- Affected code: old hashing in `src/dgov/done.py` before the current `src/dgov/done.py:110-128` and `src/dgov/done.py:300-321`.
- Result: different failures with the same trailing tail collapsed into one fingerprint, so repeated loops could be missed.
- Fix implemented: `src/dgov/done.py:110-128` now hashes a normalized 20-line window, and `src/dgov/done.py:300-321` uses it consistently.

### 5. Medium: blocked-prompt detection is duplicated in two modules

- `src/dgov/waiter.py:31-61` hard-codes `_BLOCKED_PATTERNS`.
- `src/dgov/responder.py:29-40` hard-codes `BUILT_IN_RULES`, and `src/dgov/responder.py:83-91` rematches the output separately.
- Result: prompt coverage can drift between "emit blocked event" and "auto-respond/escalate" behavior.
- Recommended fix: make `waiter` consume responder rules directly and stop maintaining a second prompt taxonomy. A small shared matcher is enough; no new abstraction layer is needed.

### 6. Medium: `send_input` vs `send_shell_command` still leaks transport details into higher-level code

- The split is documented in `src/dgov/backend.py:48-61` and implemented in `src/dgov/tmux.py:69-101`, but lifecycle and waiter both need to know which side of the boundary they are on.
- The launch pipeline uses `send_shell_command()` in `src/dgov/lifecycle.py:217-239` and `src/dgov/lifecycle.py:282-311`.
- Runtime interaction uses `send_input()` in `src/dgov/waiter.py:381-419` and `src/dgov/responder.py:153-163`.
- Result: the API is correct but still easy to misuse because it encodes tmux transport semantics rather than intent.
- Recommended fix: rename the protocol surface around intent, for example `send_runtime_input()` and `run_shell_command()`, then update tmux to remain the transport-specific backend.

## Top fixes implemented in this pass

- Live output is refreshed on every waiter poll for `wait_worker_pane()`, `wait_all_worker_panes()`, and `wait_for_slugs()`.
- `_is_done()` now records explicit completion reasons: `done_signal`, `exit_signal`, `commit`, `abandoned`, `stable`, and `circuit_breaker`.
- Responder cooldowns are session-scoped instead of global-by-slug.
- Circuit-breaker fingerprints now use a normalized 20-line window instead of the last 5 raw lines.
