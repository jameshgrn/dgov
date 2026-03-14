# dgov Documentation Plan

## Conventions

- **Tone**: practical, direct, code-heavy. No marketing language. Write for someone who already knows git, tmux, and CLI tools. Assume they want to get something done, not learn theory.
- **Format**: GitHub-flavored markdown. Fenced code blocks for all CLI examples. Use tables for flags/options. No emojis.
- **Code examples**: every page must include runnable CLI snippets. Show real output where possible (JSON). Use `# comment` annotations inside code blocks, not prose between them.
- **Structure**: each page is self-contained — include enough context to understand it without reading other pages, but link to related pages for depth.
- **Naming**: filenames are kebab-case. Titles are sentence case.

---

## Page inventory

### docs/README.md — Index

The landing page. Links to every doc page in recommended reading order.

```
# dgov documentation

## Getting started
- [Installation](installation.md)
- [Quick start](quick-start.md)
- [Core concepts](core-concepts.md)

## Usage
- [Pane lifecycle](pane-lifecycle.md)
- [Agent registry](agent-registry.md)
- [Prompt templates](prompt-templates.md)
- [Batch execution](batch-execution.md)
- [Experiment tracking](experiment-tracking.md)
- [Review-fix pipeline](review-fix-pipeline.md)
- [Hook system](hook-system.md)
- [Preflight checks](preflight-checks.md)

## Reference
- [CLI reference](cli-reference.md)
- [State and events](state-and-events.md)
- [Configuration files](configuration-files.md)

## Advanced
- [Conflict resolution](conflict-resolution.md)
- [Troubleshooting](troubleshooting.md)
- [Architecture](architecture.md)
```

---

### 1. docs/installation.md — Installation

**Target audience**: new user, first encounter with dgov.

**Outline**:
1. Prerequisites (Python >= 3.12, tmux, git, uv)
2. Install from source with uv
3. Install as global tool (`uv tool install`)
4. Verify install (`dgov version`)
5. tmux configuration requirements (`default-terminal "tmux-256color"`)
6. Optional: install agent CLIs (claude, codex, gemini, pi, etc.)

**CLI snippets**:
```bash
uv pip install -e /path/to/dgov
uv tool install --force --python 3.14 -e /path/to/dgov
dgov version
dgov agents  # shows which agents are installed
```

**Key notes**:
- Mention Python 3.12 minimum (tomllib dependency)
- Mention that dgov requires being inside a tmux session for the default backend
- Mention the Ghostty `xterm-ghostty` issue and the `default-terminal` fix

---

### 2. docs/quick-start.md — Quick start

**Target audience**: new user who just installed dgov.

**Outline**:
1. Start a dgov session (`dgov` bare command, or from inside tmux)
2. Dispatch your first worker
3. Wait for completion
4. Review the diff
5. Merge back to main
6. Close the pane
7. Full single-task workflow in one copy-paste block

**CLI snippets**:
```bash
# Start session
dgov

# Dispatch a worker
dgov pane create -a claude -p "Add a health check endpoint to app.py" -r .

# Wait for it
dgov pane wait add-health-check

# Review
dgov pane review add-health-check

# Merge
dgov pane merge add-health-check

# See what happened
dgov pane list
dgov status
```

**Key concepts introduced**: governor, worker, slug, worktree, pane.

---

### 3. docs/core-concepts.md — Core concepts

**Target audience**: new user who has run the quick start and wants to understand what happened.

**Outline**:
1. **Governor vs workers** — governor stays on main, dispatches, never writes code. Workers run in worktrees, commit, exit.
2. **Git worktrees** — each worker gets `.dgov/worktrees/<slug>/` with its own branch. Isolation by design.
3. **Panes** — a pane = worktree + tmux pane + agent process. The unit of work.
4. **Slugs** — unique identifier for each task. Auto-generated via Qwen 4B or manually specified with `-s`.
5. **Backends** — `WorkerBackend` protocol. Default is `TmuxBackend`. Future: Docker, SSH.
6. **Agents** — any CLI that accepts a prompt and produces code. dgov wraps 11+ agents with a common interface.
7. **State machine** — pane lifecycle states: active → done → reviewed_pass → merged → closed. Show the full transition diagram.
8. **Protected files** — CLAUDE.md, .napkin.md, etc. Never carried from worker branches.
9. **Main-branch enforcement** — dgov refuses to run from worktrees or non-main branches. `DGOV_SKIP_GOVERNOR_CHECK=1` to override.

**Diagrams**: ASCII state machine diagram showing all 12 states and valid transitions (from `VALID_TRANSITIONS` in persistence.py).

**No CLI snippets** — this is conceptual. Link to pane-lifecycle.md for hands-on.

---

### 4. docs/pane-lifecycle.md — Pane lifecycle

**Target audience**: daily user who wants to understand every pane operation.

**Outline**:
1. **Create** — `dgov pane create` options: `-a`, `-p`, `-r`, `-s`, `-m`, `-f`, `-e`, `-T`, `--var`, `--max-retries`, `--preflight/--no-preflight`, `--fix/--no-fix`. Show all flag combinations.
2. **List** — `dgov pane list` table format and `--json` mode. Fields: slug, agent, state, alive, done, freshness, duration, prompt.
3. **Wait** — `dgov pane wait <slug>`. Three detection modes: done-signal, new commits, output stabilization. Options: `--timeout`, `--poll`, `--stable`, `--auto-retry/--no-auto-retry`.
4. **Wait-all** — `dgov pane wait-all`. Prints each as it completes.
5. **Review** — `dgov pane review <slug>`. Diff stat, protected file check, safe-to-merge verdict. `--full` for complete diff.
6. **Diff** — `dgov pane diff <slug>`. `--stat`, `--name-only`.
7. **Capture** — `dgov pane capture <slug> -n 50`. Read live pane output.
8. **Merge** — `dgov pane merge <slug>`. `--no-close`, `--resolve agent|manual`. Plumbing merge via `git merge-tree`.
9. **Merge-all** — `dgov pane merge-all`. Sequential merge of all done panes.
10. **Escalate** — `dgov pane escalate <slug> -a claude`. Re-dispatch to a stronger agent.
11. **Retry** — `dgov pane retry <slug>`. New pane with attempt suffix. Original marked `superseded`.
12. **Resume** — `dgov pane resume <slug>`. Re-launch agent in existing worktree.
13. **Close** — `dgov pane close <slug>`. `--force` for dirty worktrees.
14. **Prune** — `dgov pane prune`. Remove stale entries.
15. **Classify** — `dgov pane classify "<prompt>"`. Recommend agent.
16. **Interact/respond** — `dgov pane interact <slug> "message"`. Send text to a worker via tmux.
17. **Nudge** — `dgov pane nudge <slug>`. Ask if done, parse response.
18. **Signal** — `dgov pane signal <slug> done|failed`. Manual state override.
19. **Logs** — `dgov pane logs <slug>`. Persistent log file.
20. **Utility panes** — `dgov pane util`, `lazygit`, `yazi`, `htop`, `k9s`, `top`.
21. **Freshness** — computed during review and list. fresh/warn/stale. Based on commits since base, file overlap, pane age.

**CLI snippets**: one example per operation. Show JSON output.

---

### 5. docs/agent-registry.md — Agent registry

**Target audience**: user who wants to add custom agents or understand existing ones.

**Outline**:
1. **Built-in agents** — table of all 11 agents with: id, CLI command, transport type, permission flags, resume support, color.
2. **Transport types** — positional, option, send-keys, stdin. How each one delivers the prompt.
3. **Permission modes** — plan, acceptEdits, bypassPermissions. How they map per agent.
4. **Listing agents** — `dgov agents` output, source column (built-in/user/project).
5. **User config** — `~/.dgov/agents.toml`. Full TOML example adding a custom agent.
6. **Project config** — `.dgov/agents.toml`. Security boundary: no health_check/health_fix in project config.
7. **Merging layers** — built-in → user → project. Override individual fields.
8. **AgentDef fields** — complete reference of all fields: id, name, short_label, prompt_command, prompt_transport, prompt_option, no_prompt_command, permission_flags, send_keys_pre_prompt, send_keys_submit, send_keys_post_paste_delay_ms, send_keys_ready_delay_ms, default_flags, resume_template, health_check, health_fix, max_concurrent, max_retries, retry_escalate_to, color, env, source.
9. **Auto-classification** — `dgov pane create -a auto`. Qwen 4B classifies task complexity.
10. **Concurrency limits** — `max_concurrent` in agents.toml.
11. **Auto-retry policy** — `max_retries`, `retry_escalate_to` in agent config.
12. **Resume support** — `resume_template` format. `{permissions}` placeholder.

**CLI snippets**:
```bash
dgov agents
dgov pane create -a auto -p "format all python files"
dgov pane classify "debug the flaky scheduler test"
```

**TOML example**:
```toml
[agents.myagent]
name = "My Custom Agent"
command = "myagent-cli"
transport = "positional"
color = 45
max_concurrent = 2

[agents.myagent.permissions]
acceptEdits = "--auto-accept"
bypassPermissions = "--yolo"

[agents.myagent.resume]
template = "myagent-cli --continue{permissions}"
```

---

### 6. docs/prompt-templates.md — Prompt templates

**Target audience**: user who dispatches repetitive tasks.

**Outline**:
1. **What templates are** — reusable prompts with `{variable}` substitution.
2. **Built-in templates** — bugfix, feature, refactor, test, review. Show each template's text, required vars, default agent.
3. **Using templates** — `dgov pane create -T bugfix --var file=src/foo.py --var description="off-by-one"`.
4. **Listing templates** — `dgov template list`.
5. **Showing template details** — `dgov template show bugfix`.
6. **Creating user templates** — `dgov template create mytemplate` prints TOML skeleton. Save to `.dgov/templates/mytemplate.toml`.
7. **User template format** — TOML spec with all fields.
8. **Override built-ins** — user templates with same name replace built-ins.

**CLI snippets**:
```bash
dgov template list
dgov template show bugfix
dgov pane create -T bugfix --var file=src/parser.py --var description="null pointer" --var test_file=tests/test_parser.py
dgov template create migration
```

---

### 7. docs/batch-execution.md — Batch execution

**Target audience**: user running multiple tasks at once.

**Outline**:
1. **Spec format** — JSON with `project_root` and `tasks[]`. Each task: id, prompt, agent, touches, timeout, permission_mode.
2. **DAG scheduling** — tasks with disjoint `touches` run in parallel tiers. Overlapping files → serialized.
3. **Dry run** — `dgov batch spec.json --dry-run` shows computed tiers.
4. **Execution flow** — create all panes in tier → wait → merge → next tier. Failure aborts remaining tiers.
5. **Example spec** — full JSON example with 4-5 tasks.
6. **Output format** — JSON with tiers, merged, failed arrays.

**CLI snippets**:
```bash
dgov batch tasks.json --dry-run
dgov batch tasks.json
```

---

### 8. docs/experiment-tracking.md — Experiment tracking

**Target audience**: user optimizing a metric iteratively.

**Outline**:
1. **What experiments are** — sequential hypothesis testing. Dispatch worker, evaluate metric, accept (merge) or reject (discard).
2. **Writing a program file** — markdown file describing what to optimize.
3. **Result file format** — JSON at `.dgov/experiments/results/<exp-id>.json` with metric_value, hypothesis, follow_ups.
4. **Running experiments** — `dgov experiment start -p program.md -m latency -b 5 -a claude`.
5. **Direction** — minimize (default) or maximize.
6. **Dry run** — `--dry-run` shows plan.
7. **Viewing logs** — `dgov experiment log -p optimize-latency`.
8. **Summary** — `dgov experiment summary -p optimize-latency`.
9. **How follow_ups work** — worker suggests next hypothesis, loop picks it up.
10. **Baseline tracking** — best accepted result becomes next baseline.

**CLI snippets**:
```bash
dgov experiment start -p experiments/reduce-latency.md -m latency_ms -b 5 -a claude -d minimize
dgov experiment log -p reduce-latency
dgov experiment summary -p reduce-latency -d minimize
```

---

### 9. docs/review-fix-pipeline.md — Review-fix pipeline

**Target audience**: user wanting automated code review + fix.

**Outline**:
1. **Three phases** — review → approve → fix.
2. **Running it** — `dgov review-fix -t src/dgov/ --review-agent claude --fix-agent claude`.
3. **Severity threshold** — `--severity critical|medium|low`. Controls which findings get fixed.
4. **Auto-approve** — `--auto-approve` skips manual approval.
5. **Review prompt format** — structured JSON output from reviewer.
6. **Fix prompt format** — per-file fix dispatch.
7. **Output** — findings, fixes applied, pass/fail counts.

**CLI snippets**:
```bash
dgov review-fix -t src/dgov/panes.py -t src/dgov/merger.py --severity medium
dgov review-fix -t src/ --auto-approve --timeout 900
```

---

### 10. docs/hook-system.md — Hook system

**Target audience**: user customizing dgov behavior.

**Outline**:
1. **Hook search order** — `.dgov-hooks/` (version-controlled) → `.dgov/hooks/` (gitignored) → `~/.dgov/hooks/` (global). First found wins.
2. **Available hooks** — worktree_created, pre_merge, post_merge, before_worktree_remove.
3. **Environment variables** — DGOV_ROOT, DGOV_SLUG, DGOV_PROMPT, DGOV_AGENT, DGOV_WORKTREE_PATH, DGOV_BRANCH.
4. **worktree_created** — runs after worktree + pane created, before agent launch. Use case: write worker-specific CLAUDE.md, install deps.
5. **pre_merge** — runs before merge. Use case: restore protected files. Fallback behavior if no hook.
6. **post_merge** — runs after merge. Use case: lint, verify protected files. Fallback behavior.
7. **before_worktree_remove** — runs before worktree deletion. Use case: archive artifacts.
8. **Fallback behaviors** — what dgov does inline when no hook exists (protected file restoration, lint).
9. **Writing a hook** — example bash script. Must be executable.

**Code examples**: full hook script for worktree_created.

---

### 11. docs/preflight-checks.md — Preflight checks

**Target audience**: user diagnosing dispatch failures.

**Outline**:
1. **When preflight runs** — automatically before `pane create` (disable with `--no-preflight`). Also standalone.
2. **Checks** — agent_cli, git_clean, git_branch, tunnel, kerberos, deps, stale_worktrees, file_locks.
3. **Each check explained** — what it tests, when it's critical vs. warning.
4. **Auto-fix** — `--fix` (default: on during create). What it fixes: tunnel, kerberos, deps, stale worktrees.
5. **Standalone usage** — `dgov preflight -a claude -r . --fix`.
6. **File lock detection** — overlap with active panes, `.lock` files.
7. **Output format** — JSON with checks array.

**CLI snippets**:
```bash
dgov preflight -a pi -r /path/to/repo --fix
dgov preflight -a claude -t src/foo.py -t src/bar.py
dgov pane create -a claude -p "..." --no-preflight
```

---

### 12. docs/cli-reference.md — CLI reference

**Target audience**: anyone looking up exact syntax.

**Outline**: every command and subcommand with full flag reference in table format. Group by subcommand tree.

1. **dgov** (bare) — start/style tmux session
2. **dgov status** — `--project-root`, `--session-root`
3. **dgov agents** — `--project-root`
4. **dgov version** — no args
5. **dgov rebase** — `--project-root`, `--onto`
6. **dgov blame** — `FILE_PATH`, `--project-root`, `--session-root`, `--all`, `--agent`
7. **dgov preflight** — `--project-root`, `--session-root`, `--agent`, `--fix`, `--touches`, `--branch`
8. **dgov batch** — `SPEC_PATH`, `--session-root`, `--dry-run`
9. **dgov review-fix** — `--targets`, `--review-agent`, `--fix-agent`, `--auto-approve`, `--severity`, `--project-root`, `--session-root`, `--timeout`
10. **dgov pane create** — all 12 options
11. **dgov pane list** — `--project-root`, `--session-root`, `--json`
12. **dgov pane wait** — `SLUG`, `--project-root`, `--session-root`, `--timeout`, `--poll`, `--stable`, `--auto-retry/--no-auto-retry`
13. **dgov pane wait-all** — `--project-root`, `--session-root`, `--timeout`, `--poll`, `--stable`
14. **dgov pane review** — `SLUG`, `--project-root`, `--session-root`, `--full`
15. **dgov pane diff** — `SLUG`, `--project-root`, `--session-root`, `--stat`, `--name-only`
16. **dgov pane capture** — `SLUG`, `--project-root`, `--session-root`, `--lines`
17. **dgov pane merge** — `SLUG`, `--project-root`, `--session-root`, `--close/--no-close`, `--resolve`
18. **dgov pane merge-all** — `--project-root`, `--session-root`, `--close/--no-close`, `--resolve`
19. **dgov pane escalate** — `SLUG`, `--project-root`, `--session-root`, `--agent`, `--permission-mode`
20. **dgov pane retry** — `SLUG`, `--project-root`, `--session-root`, `--agent`, `--prompt`, `--permission-mode`
21. **dgov pane resume** — `SLUG`, `--project-root`, `--session-root`, `--agent`, `--prompt`, `--permission-mode`
22. **dgov pane close** — `SLUG`, `--project-root`, `--session-root`, `--force`
23. **dgov pane prune** — `--project-root`, `--session-root`
24. **dgov pane classify** — `PROMPT`
25. **dgov pane interact** — `SLUG`, `MESSAGE`, `--session-root`
26. **dgov pane respond** — `SLUG`, `MESSAGE`, `--session-root` (alias for interact)
27. **dgov pane nudge** — `SLUG`, `--session-root`, `--wait`
28. **dgov pane signal** — `SLUG`, `SIGNAL_TYPE` (done|failed), `--session-root`
29. **dgov pane logs** — `SLUG`, `--project-root`, `--session-root`, `--tail`
30. **dgov pane util** — `COMMAND`, `--title`, `--cwd`
31. **dgov pane lazygit/yazi/htop/k9s/top** — `--cwd`
32. **dgov checkpoint create** — `NAME`, `--project-root`, `--session-root`
33. **dgov checkpoint list** — `--project-root`, `--session-root`
34. **dgov template list** — `--project-root`, `--session-root`
35. **dgov template show** — `NAME`, `--project-root`, `--session-root`
36. **dgov template create** — `NAME`
37. **dgov experiment start** — `--program`, `--metric`, `--budget`, `--agent`, `--direction`, `--project-root`, `--session-root`, `--timeout`, `--dry-run`
38. **dgov experiment log** — `--program`, `--project-root`, `--session-root`
39. **dgov experiment summary** — `--program`, `--project-root`, `--session-root`, `--direction`

**Format per command**: table with columns: Flag, Short, Type, Default, Description.

---

### 13. docs/state-and-events.md — State and events

**Target audience**: user debugging or building integrations.

**Outline**:
1. **State database** — `.dgov/state.db`, SQLite with WAL mode. Pane records: slug, agent, pane_id, worktree_path, branch_name, base_sha, created_at, state, prompt.
2. **Pane states** — all 12 states defined in `PANE_STATES`. Meaning of each.
3. **State transitions** — `VALID_TRANSITIONS` table. Show which states can transition to which.
4. **IllegalTransitionError** — what triggers it, how to recover.
5. **Event journal** — `.dgov/events.jsonl`. Append-only. Event format: ts, event, pane, plus event-specific fields.
6. **Event types** — all 22 event types from `VALID_EVENTS`. What triggers each.
7. **Blame** — `dgov blame <file>`. How it resolves commits to agents via merge SHA + subject line parsing.
8. **Checkpoints** — snapshots of state.db + events.jsonl at `.dgov/checkpoints/<name>/`.

**Example JSON**: event log entries for a full pane lifecycle (created → done → merged → closed).

---

### 14. docs/configuration-files.md — Configuration files

**Target audience**: user setting up dgov for a project or globally.

**Outline**:
1. **Directory layout** — `.dgov/` contents: state.db, events.jsonl, worktrees/, logs/, prompts/, checkpoints/, experiments/, templates/, agents.toml, hooks/.
2. **Global config** — `~/.dgov/` contents: agents.toml, hooks/, responses.toml.
3. **Team config** — `.dgov-hooks/` (version-controlled).
4. **agents.toml** — full spec (see agent-registry.md for details).
5. **responses.toml** — auto-responder rules. Pattern, response, action (send/signal_done/signal_failed/escalate). Cooldown.
6. **templates** — `.dgov/templates/*.toml`.
7. **Protected files** — CLAUDE.md, CLAUDE.md.full, THEORY.md, ARCH-NOTES.md, .napkin.md.
8. **TDD status file** — `$DGOV_TDD_STATUS_FILE` and its JSON format.
9. **Environment variables** — `DGOV_SKIP_GOVERNOR_CHECK`, `DGOV_ROOT`, `DGOV_SLUG`, `DGOV_PROMPT`, `DGOV_AGENT`, `DGOV_WORKTREE_PATH`, `DGOV_BRANCH`, `DGOV_TDD_STATUS_FILE`.

---

### 15. docs/conflict-resolution.md — Conflict resolution

**Target audience**: user dealing with merge conflicts.

**Outline**:
1. **Plumbing merge** — `git merge-tree` + `commit-tree`. In-memory, no side effects on failure.
2. **Agent resolution** — `--resolve agent` (default). Spawns resolver pane (claude or codex). Waits for `<<<<<<<` markers to be removed. Commits if clean, aborts if not.
3. **Manual resolution** — `--resolve manual`. Leaves conflict markers. User fixes manually.
4. **Post-merge lint** — auto-runs `ruff check --fix` + `ruff format` on changed `.py` files. Amends merge commit.
5. **Protected file restoration** — pre_merge hook or inline restoration from base commit.
6. **Freshness and stale merges** — when to rebase first.

**CLI snippets**:
```bash
dgov pane merge fix-parser --resolve agent
dgov pane merge fix-parser --resolve manual
dgov pane merge fix-parser --no-close  # keep worktree for inspection
```

---

### 16. docs/troubleshooting.md — Troubleshooting

**Target audience**: user hitting problems.

**Outline (FAQ format)**:

1. **"dgov is running inside a git worktree"** — you're in a worker directory. `cd` back to main repo root. Or set `DGOV_SKIP_GOVERNOR_CHECK=1`.
2. **"Governor is on branch X, but must stay on main"** — `git checkout main`.
3. **Worker pane doesn't commit** — pi needs explicit commit instructions in prompt. Check prompt structure. Use numbered steps.
4. **Pane stuck as "active" forever** — use `dgov pane nudge <slug>`. Use `dgov pane signal <slug> done`. Check if agent is blocked on a prompt (`dgov pane capture`).
5. **Agent not found** — run `dgov agents` to see installed status. Install the CLI. Check `PATH`.
6. **Preflight fails on tunnel/kerberos** — only relevant for pi (SSH tunnel to GPU). Use `--no-preflight` if not using pi.
7. **Merge conflicts** — use `--resolve agent` or `--resolve manual`. If agent resolution fails, dgov aborts and you can retry.
8. **Protected files clobbered** — expected. pre_merge hook restores them. If missing, dgov does inline restoration.
9. **tmux "not a terminal" errors** — ensure `default-terminal "tmux-256color"` in `.tmux.conf`.
10. **Slug already exists** — slugs must be unique. Close the old pane first, or use a different `-s` slug.
11. **State database locked** — WAL mode should prevent this. If stuck: no running dgov processes → safe to delete `.dgov/state.db-wal` and `.dgov/state.db-shm`.
12. **Stale worktrees** — `dgov pane prune` removes dead entries. `dgov preflight --fix` prunes stale git worktrees.
13. **Auto-retry keeps failing** — set `--max-retries 0` on create. Or check the underlying task is possible for the agent.
14. **VIRTUAL_ENV leaks into worker** — unset VIRTUAL_ENV before launching dgov, or use `--python` flag.

---

### 17. docs/architecture.md — Architecture

**Target audience**: contributor or curious power user.

**Outline**:
1. **Module map** — all 18 Python modules with one-sentence descriptions:
   - `cli.py` — Click command tree, entry point
   - `panes.py` — facade re-exporting from split modules
   - `persistence.py` — SQLite state + event journal
   - `waiter.py` — poll/wait logic, done detection, blocked detection
   - `merger.py` — git plumbing merge, conflict resolution, post-merge lint
   - `batch.py` — DAG tier computation, batch runner, checkpoints
   - `agents.py` — agent registry, launch command builder, TOML config loader
   - `templates.py` — prompt template system
   - `experiment.py` — experiment loops, metric comparison, log
   - `blame.py` — file-to-agent attribution via events + git log
   - `strategy.py` — Qwen 4B integration, task classification, slug generation, prompt structuring
   - `responder.py` — auto-respond to blocked workers
   - `retry.py` — auto-retry engine, retry policy, lineage tracking
   - `review_fix.py` — review-then-fix pipeline
   - `preflight.py` — pre-dispatch validation and auto-fix
   - `backend.py` — `WorkerBackend` protocol (abstract)
   - `tmux.py` — tmux command wrappers (default backend)
   - `state.py` — status aggregation (panes + tunnel + kerberos)
   - `models.py` — shared dataclasses (TaskSpec, MergeResult, ConflictDetails)
2. **Data flow** — governor calls cli.py → panes.py facade → split modules → persistence.py → state.db / events.jsonl.
3. **Backend abstraction** — `WorkerBackend` protocol with 9 methods. `TmuxBackend` is the only implementation. How to add a new backend.
4. **State machine** — `VALID_TRANSITIONS` dict. How `_update_pane_state` enforces transitions.
5. **Merge strategy** — plumbing merge flow: merge-tree → commit-tree → update-ref. Why this is safer than porcelain merge.
6. **Security boundaries** — project-level agents.toml cannot define health_check/health_fix (shell exec risk). Protected files. Governor enforcement.
7. **Concurrency** — max_concurrent in agent config. Bulk tmux queries (`bulk_pane_info`). WAL mode for SQLite.
8. **Test structure** — 571+ tests. markers: `unit`. Key test files and what they cover.

---

## Recommended reading order (learning path)

| Step | Page | Why |
|------|------|-----|
| 1 | installation.md | Get dgov running |
| 2 | quick-start.md | First successful dispatch-wait-review-merge |
| 3 | core-concepts.md | Understand the model |
| 4 | pane-lifecycle.md | All pane operations |
| 5 | agent-registry.md | Configure agents |
| 6 | cli-reference.md | Look up exact flags |
| 7 | prompt-templates.md | Reusable prompts |
| 8 | hook-system.md | Customize behavior |
| 9 | batch-execution.md | Multi-task workflows |
| 10 | conflict-resolution.md | Handle merge conflicts |
| 11 | state-and-events.md | Debug and audit |
| 12 | experiment-tracking.md | Metric optimization |
| 13 | review-fix-pipeline.md | Automated code review |
| 14 | preflight-checks.md | Diagnose failures |
| 15 | configuration-files.md | All config in one place |
| 16 | troubleshooting.md | When things break |
| 17 | architecture.md | Contribute or extend |

---

## Cross-cutting notes for doc writers

1. **All CLI examples must use `dgov`**, not `python -m dgov` or `dmux` (old name).
2. **JSON output**: show actual output from commands where possible. Use `jq` for pretty-printing in examples.
3. **Flag tables**: use this format consistently:
   ```
   | Flag | Short | Type | Default | Description |
   |------|-------|------|---------|-------------|
   ```
4. **Internal links**: use relative markdown links (`[core concepts](core-concepts.md)`).
5. **No screenshots** — terminal output as fenced code blocks only.
6. **Version**: state "v0.5.0" in installation.md. Don't embed version elsewhere.
7. **Auto-structured prompts**: mention in agent-registry.md that pi prompts get auto-structured by `_structure_pi_prompt()`. Users don't need to worry about formatting for pi — dgov handles it.
8. **TDD protocol**: mention in pane-lifecycle.md that every worker prompt gets TDD protocol appended. Show TDD status JSON format and how `dgov pane list` displays progress.
9. **Auto-responder**: mention in pane-lifecycle.md under "wait" that blocked panes may get auto-responded. Full details in configuration-files.md under responses.toml.
10. **Blame**: cover in state-and-events.md, not its own page. It's a query against existing data.

---

## Files to create (summary)

| # | File | Title |
|---|------|-------|
| 0 | `docs/README.md` | dgov documentation |
| 1 | `docs/installation.md` | Installation |
| 2 | `docs/quick-start.md` | Quick start |
| 3 | `docs/core-concepts.md` | Core concepts |
| 4 | `docs/pane-lifecycle.md` | Pane lifecycle |
| 5 | `docs/agent-registry.md` | Agent registry |
| 6 | `docs/prompt-templates.md` | Prompt templates |
| 7 | `docs/batch-execution.md` | Batch execution |
| 8 | `docs/experiment-tracking.md` | Experiment tracking |
| 9 | `docs/review-fix-pipeline.md` | Review-fix pipeline |
| 10 | `docs/hook-system.md` | Hook system |
| 11 | `docs/preflight-checks.md` | Preflight checks |
| 12 | `docs/cli-reference.md` | CLI reference |
| 13 | `docs/state-and-events.md` | State and events |
| 14 | `docs/configuration-files.md` | Configuration files |
| 15 | `docs/conflict-resolution.md` | Conflict resolution |
| 16 | `docs/troubleshooting.md` | Troubleshooting |
| 17 | `docs/architecture.md` | Architecture |
