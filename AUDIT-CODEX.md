# dgov Efficiency Audit

Reviewed every file under `src/dgov/` plus the packaged `pi-extensions`.

Quick counts:

- `subprocess.run`: 116 call sites
- `subprocess.Popen`: 0 call sites
- Warm import cost: `dgov.cli` ~32 ms, `dgov.lifecycle` ~26 ms, `dgov.agents` ~22 ms, `dgov.persistence` ~18 ms, `dgov.dashboard` ~17 ms
- Typical `pane list` DB behavior: 1 `SELECT * FROM panes` when nothing changes; each pane that flips state during the list adds an `UPDATE`, a title-refresh `SELECT`, and a reconciliation `SELECT`

## Prompt Lifecycle Trace

For non-`send-keys` agents, the prompt path is:

1. Click parses `--prompt` and passes it into `create_worker_pane()`.
   - Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/cli/pane.py:38`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/cli/pane.py:179`
2. `create_worker_pane()` keeps the original prompt for slugging, hook input, state persistence, and event emission.
   - Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:329`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:403`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:424`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:437`
3. `_setup_and_launch_agent()` copies it again into hook env, optionally restructures it, then rewrites absolute paths for the worktree.
   - Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:218`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:232`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:236`
4. `build_launch_command()` writes the rewritten prompt to `.dgov/prompts/<slug>--<ts>-<rand>.txt`.
   - Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/agents.py:539`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/agents.py:595`
5. The generated shell snippet reads that file back into `$DGOV_PROMPT_CONTENT`, deletes the file, and passes the shell variable into the agent CLI as positional arg, option arg, or stdin.
   - Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/agents.py:551`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/agents.py:598`
6. In parallel, the original prompt is persisted in SQLite `panes.prompt`, and a 200-char preview is copied into the event log.
   - Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:424`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:202`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:442`

For `send-keys` agents, step 4-5 is replaced by tmux buffer transport:

- `send_prompt_via_buffer()` copies the prompt into the tmux server buffer, pastes it into the pane, sends Enter, then deletes the buffer.
- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:257`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/tmux.py:295`

## Findings

### 1. Full prompts are stored inline in `panes` and reloaded on every list-style read

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:424`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:202`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:357`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/status.py:164`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/dashboard.py:181`
- Impact: hot path
- Evidence: `WorkerPane.prompt` is stored in the `panes` row, `all_panes()` does `SELECT *`, and `list_worker_panes()` uses that full row even when it only needs an 80-char preview. The dashboard refresh thread hits this every second by default.
- Fix: split prompt storage from pane listing. Keep `prompt_preview` on the pane row and move the full prompt into a separate `prompts` table or prompt-blob file keyed by slug. Add a list query that selects only pane metadata.

### 2. Hook execution inflates the environment with the full prompt

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:218`
- Impact: hot path
- Evidence: `DGOV_PROMPT` is exported with the entire prompt before the hook runs. Large prompts get copied into process env, then copied again by the child hook process.
- Fix: stop passing full prompt text in env. Pass `DGOV_SLUG`, `DGOV_SESSION_ROOT`, and either a prompt file path or a DB key so hooks can fetch the prompt only if they need it.

### 3. Non-`send-keys` prompt transport writes the prompt to disk, reads it back into a shell variable, then hands it to the agent

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/agents.py:539`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/agents.py:551`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/agents.py:595`
- Impact: hot path
- Evidence: every positional/option/stdin launch pays for disk write, disk read, shell-variable expansion, and file deletion. This is the densest copy chain in the whole prompt lifecycle.
- Fix: add a real `file` transport and prefer `stdin` for CLIs that support it. Use the current temp-file fallback only for CLIs that truly require prompt text as an argv string.

### 4. `send_prompt_via_buffer()` uses four tmux subprocesses and passes the whole prompt as argv

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/tmux.py:295`
- Impact: hot path for `cline`/`crush`
- Evidence: `set-buffer`, `paste-buffer`, `send-keys`, and `delete-buffer` are four `_run()` calls. `set-buffer -- <prompt>` also puts the entire prompt on the subprocess argument vector.
- Fix: change `_run()` to accept stdin and use `tmux load-buffer -b <name> -`, then `tmux paste-buffer -d -b <name> -t <pane>`, then `send-keys Enter`. That cuts the tmux calls and removes argv-size pressure.

### 5. Worker-pane bootstrap fans out into roughly 21 tmux subprocesses before custom env vars or prompt paste

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:183`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:191`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:201`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:205`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:268`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/tmux.py:133`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/tmux.py:189`
- Impact: hot path
- Evidence: `setup_pane_borders()` is 2 tmux calls, pane creation is 1, title/style/rename lock is 7 more, auth scrubbing is 3 `send-keys`, logging is 1, `DGOV_*` exports are 7 more, and the wrapped launch command is another `send-keys`. Each extra env var adds another subprocess.
- Fix: batch tmux configuration with a single `tmux ... \; ...` command or `source-file`. Fold `unset`, `export`, and the launch command into one shell script sent once to the pane instead of `send-keys` per variable.

### 6. `wait_for_slugs()` and `wait_all_worker_panes()` do one `get_pane()` per slug per poll cycle

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/waiter.py:413`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/waiter.py:522`
- Impact: hot path
- Evidence: the batch waiters poll each pending slug independently. With 20 panes and a 3s poll interval, this becomes 20 individual `SELECT * FROM panes WHERE slug = ?` queries every cycle.
- Fix: load pane state once per cycle with `all_panes()` or a `SELECT ... WHERE slug IN (...)`, build a slug-index dict in memory, and reuse it for done checks and timeout reporting.

### 7. Review and retry logic still read the full event log when a slug filter or aggregate query would do

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/inspection.py:106`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/retry.py:45`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/retry.py:83`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/blame.py:30`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/blame.py:141`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:82`
- Impact: medium path
- Evidence: `read_events()` already supports `slug`, but review/retry/blame often load the whole table and filter in Python.
- Fix: add targeted SQL helpers such as `read_events(session_root, slug=...)`, `count_events(session_root, slug, event)`, and use them instead of full-table scans.

### 8. `git worktree prune` is run repeatedly on create and cleanup

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:53`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:525`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/gitops.py:19`
- Impact: hot path
- Evidence: every pane create does `git worktree prune` before it even checks whether the worktree already exists, and cleanup paths prune again.
- Fix: prune once per command/session, or only after a failed `worktree add/remove` when Git actually reports stale metadata.

### 9. `check_git_clean()` shells out twice where one git status call would answer the question

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/preflight.py:72`
- Impact: medium path
- Evidence: `git diff --quiet HEAD` and `git diff --quiet --cached` are run back-to-back on every default preflight. `pane create` calls preflight by default.
- Fix: replace both with one `git status --porcelain --untracked-files=no` and inspect whether any tracked-file entries are present.

### 10. The import graph has a real cycle cluster around pane state and completion logic

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:324`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:421`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/status.py:21`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/waiter.py:240`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/waiter.py:291`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/recovery.py:8`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/retry.py:10`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/responder.py:124`
- Impact: mixed, with hot-path consequences
- Evidence: the `lifecycle -> persistence -> status -> waiter -> retry/recovery/responder` cluster is only held together by local imports in active code paths like done detection and title updates.
- Fix: extract shared concerns into acyclic modules:
  - pane-title formatting
  - done-signal and output-capture helpers
  - retry metadata helpers
  - response-rule execution

### 11. The CLI package has a second cycle and eagerly imports every subcommand module on startup

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/cli/__init__.py:12`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/cli/__init__.py:203`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/cli/admin.py:14`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/cli/pane.py:11`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/cli/templates.py:12`
- Impact: cold path
- Evidence: subcommand modules import `SESSION_ROOT_OPTION` from `dgov.cli`, while `dgov.cli` imports the subcommands back for registration. Also, `detect_installed_agents` is imported at module import time even though it is only needed for bare `dgov`.
- Fix: move `SESSION_ROOT_OPTION` into `dgov.cli.common`, lazy-register subcommands, and move the top-level `detect_installed_agents` import into `_resolve_governor()`.

### 12. Dashboard refresh spends a subprocess every second just to learn the current branch

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/dashboard.py:162`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/dashboard.py:176`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/dashboard.py:302`
- Impact: hot path
- Evidence: `fetch_panes()` calls `_get_branch()` on every refresh cycle. Default refresh is 1 second, so an idle dashboard still runs `git rev-parse --abbrev-ref HEAD` once per second forever.
- Fix: cache the branch and only refresh it when `.git/HEAD` changes, or refresh branch name on a slower cadence than pane status.

### 13. `TmuxBackend` is mostly a one-line delegation layer with repeated lazy imports

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/backend.py:101`
- Impact: low path
- Evidence: almost every method does `from dgov import tmux` and forwards one call. The abstraction seam is valid, but the implementation adds indirection without consolidating behavior.
- Fix: if the backend abstraction stays, import `dgov.tmux` once and make `TmuxBackend` the actual implementation boundary. If alternate backends are not planned soon, collapsing the wrapper would simplify the call graph.

### 14. `WorkerPane` is typed on write, but the rest of the system immediately degrades it to raw dicts

- Files: `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:161`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:332`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:350`, `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/lifecycle.py:424`
- Impact: low to medium
- Evidence: `WorkerPane` is only strongly typed at creation time, then `asdict()` is stored and every downstream caller treats pane records as plain dicts.
- Fix: either keep a typed pane model end-to-end, or remove the dataclass and make the persisted record schema explicit. The current hybrid form adds ceremony without reducing branching or copy cost.

## Database Access Summary

- Connection lifecycle: efficient. `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:231` caches one SQLite connection per `(db_path, thread)` and does not reopen per call.
- WAL mode: mildly helpful, not decisive. `/Users/jakegearon/projects/dgov/.dgov/worktrees/audit-codex/src/dgov/persistence.py:249` helps the dashboard reader thread coexist with writers, but the bigger cost is row size and poll frequency, not lock contention.
- Typical list path:
  - `all_panes()` once
  - zero extra DB work if no pane changes state
  - plus `UPDATE` + `SELECT` title refresh + `SELECT` reconciliation for each pane that flips state during the list
- Main DB inefficiencies:
  - full prompt blobs on every list read
  - per-slug polling in wait loops
  - whole-event-log scans in review/retry/blame

## Top 10 Highest-Impact Improvements

1. Batch tmux worker bootstrap into one command/script instead of ~21 `_run()` calls plus one `send-keys` per env var.
2. Remove full prompt blobs from `panes` list reads; store a preview on the pane row and move the full prompt elsewhere.
3. Stop exporting `DGOV_PROMPT` as full text to hooks; pass a prompt reference instead.
4. Replace generic prompt temp-file -> shell-variable transport with per-agent `stdin` or `file` transports wherever supported.
5. Make `wait_for_slugs()` and `wait_all_worker_panes()` batch pane reads per poll cycle.
6. Break the `lifecycle/persistence/status/waiter/retry/recovery/responder` cycle by extracting shared helpers into acyclic modules.
7. Rework `send_prompt_via_buffer()` to use `load-buffer` from stdin and `paste-buffer -d`.
8. Stop calling `git worktree prune` on every create and cleanup.
9. Lazy-register CLI subcommands and move `SESSION_ROOT_OPTION` out of `dgov.cli` to break the CLI cycle and cut startup work.
10. Replace repeated full-event-log scans with slug-filtered SQL helpers, starting with review and retry paths.
