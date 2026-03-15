# Audit: Worker Context Management

## Summary

dgov currently gives workers three real context channels:

1. The launch prompt.
2. A worktree-local `CLAUDE.md` written by the `worktree_created` hook.
3. The checked-out repository snapshot in the worker worktree.

That is enough for many small tasks, but most of the recurring failure modes are not "missing knowledge base" problems. They are mostly:

- prompt-shape problems,
- missing stop conditions,
- hidden-but-important state that exists in dgov but is not surfaced to the worker,
- and one confusing design choice: workers are told they can inspect `HEAD:CLAUDE.md`, but in this repo that file is the governor prompt, not a worker-safe conventions file.

For a ~9.6k-line tool, the minimal useful context upgrade is not qmd or a retrieval stack. It is:

- make the worker-visible context explicit,
- provide a worker-safe conventions artifact,
- expose a few missing facts as env vars,
- carry retry/review context forward on retries and escalations,
- and add agent-specific failure budgets / scope ceilings.

## What a Worker Receives Today

### 1. Prompt

`create_worker_pane()` in `src/dgov/lifecycle.py` does the following:

1. Captures `base_sha` from the main repo `HEAD`.
2. Creates a git worktree on branch `<slug>`.
3. Creates a tmux pane in that worktree.
4. Runs the `worktree_created` hook.
5. Rewrites absolute paths in the prompt from main repo path to worktree path.
6. Launches the agent with that prompt.

Actual prompt behavior:

- For most agents, the worker gets the task prompt essentially as written.
- For `pi`, `_structure_pi_prompt()` rewrites the prompt into numbered steps, adds inferred `Read ...` steps, adds `ruff check` for changed Python files, and appends `git add` + `git commit`.
- On `resume`, dgov appends a short resume note: run `git status` and `git log --oneline -5`, continue from existing work, do not redo committed work.
- On auto-retry, dgov appends failure context from `retry_context()`: tail of the worker log, exit code if present, and recent pane events.
- On escalation, dgov does not append failure context. It just reuses the original prompt.

Important detail:

- For non-`send-keys` agents, `build_launch_command()` writes the prompt to `WORKTREE/.dgov/prompts/<slug>--<ts>-<rand>.txt`, reads it into the shell command, then deletes the file. The prompt is a transient launch artifact, not a persistent worktree document.

### 2. Hook-injected `CLAUDE.md`

Today there is no repo-local `.dgov/hooks/worktree_created` and no repo-local `.dgov-hooks/worktree_created`, so the active hook is the global fallback:

- `/Users/jakegearon/.dgov/hooks/worktree_created`

That hook overwrites the worktree's `CLAUDE.md` with worker instructions. It injects:

- shared worker rules,
- tool commands inferred from project type,
- agent-specific guidance for `pi`, `claude`, `codex`, `gemini`, `hunter`, `cursor`, or a generic fallback.

It also:

- marks `CLAUDE.md` as `assume-unchanged`,
- creates agent-native symlinks where applicable:
  - `GEMINI.md -> CLAUDE.md`
  - `.cursorrules -> CLAUDE.md`
  - `.clinerules -> CLAUDE.md`

The injected worker `CLAUDE.md` currently tells workers:

- commit when done,
- do not touch `CLAUDE.md` or `.napkin.md`,
- do not create docs,
- use `uv run ruff check`, `uv run ruff format`, and targeted `pytest -q`,
- and, if needed, run `git show HEAD:CLAUDE.md` to see the original project `CLAUDE.md`.

That last point is a problem in this repo:

- `HEAD:CLAUDE.md` is the governor prompt, not a worker conventions file.
- It contains useful tool/style rules, but also role-conflicting instructions like "You are the governor" and "You never edit source code directly."
- So dgov currently exposes the original `CLAUDE.md`, but not in a worker-safe form.

### 3. Environment Variables

There are three distinct env channels. They are easy to conflate.

#### Pane startup env

Passed to tmux pane creation:

- `DISABLE_AUTO_UPDATE=true`
- `DISABLE_UPDATE_PROMPT=true`

These exist in the worker shell because tmux creates the pane with `-e KEY=VALUE`.

#### Shell mutations after pane creation

dgov sends commands into the shell to:

- `unset CLAUDECODE`
- `unset ANTHROPIC_API_KEY`
- `unset CLAUDE_CODE_API_KEY`

Then it exports:

- any `[agents.<id>.env]` values from the merged agent registry,
- any explicit `env_vars` passed at worker creation.

In the actual config on this machine:

- `~/.dgov/agents.toml` overrides `pi`, `hunter`, and `qwen`,
- but does not define any `[agents.<id>.env]` entries,
- so current workers get no extra agent env vars from config.

#### Hook-only env

The `worktree_created` hook receives:

- `DGOV_ROOT`
- `DGOV_PANE_ID`
- `DGOV_SLUG`
- `DGOV_PROMPT`
- `DGOV_AGENT`
- `DGOV_WORKTREE_PATH`
- `DGOV_BRANCH`
- `DGOV_OWNS_WORKTREE`

Crucial distinction:

- these are hook env vars,
- not worker-shell env vars.

The worker process does not automatically see `DGOV_ROOT`, `DGOV_BRANCH`, `DGOV_SLUG`, or `DGOV_WORKTREE_PATH` after launch.

### 4. Files Present in the Worktree at Launch

What exists in the worker worktree:

- all tracked repo files at `base_sha`,
- the worker branch checkout,
- `.git` pointing at the worktree gitdir,
- the hook-generated worker `CLAUDE.md`,
- optional native symlinked instruction files,
- a transient `.dgov/prompts/...txt` prompt file during launch for most agents.

What does not come along automatically:

- the main repo's `.napkin.md` if it is gitignored,
- the main repo's `.dgov/state.db`,
- the main repo's `.dgov/logs/`,
- the main repo's `.dgov/events.jsonl`,
- the main repo's untracked files,
- any local hook/config state outside tracked files unless the worker knows the main repo path and goes looking.

Observed in this repo:

- the main repo has `/Users/jakegearon/projects/dgov/.napkin.md`,
- but this worktree did not start with `.napkin.md`,
- which matches the previous audit's finding that worktrees get an empty napkin context by default.

### 5. What the Worker Does Not See, But Might Need

Not surfaced directly today:

- `base_sha`
- the main repo/session root path
- the worker slug
- whether sibling workers are active on overlapping files
- the latest curated lessons from `.napkin.md`
- reviewer findings from a previous failed attempt
- escalation context from the prior weaker agent

Most of that information exists in dgov state. It is just not surfaced to the worker.

## Context Category Evaluation

| Category | Does worker need it? | How much? | Current dgov behavior | What form is actually useful? |
|---|---|---:|---|---|
| Task prompt | Yes, absolutely | High | Raw prompt for most agents; auto-structured for `pi`; path-rewritten to worktree; retry adds log/event context | A precise task with file paths, expected tests, commit instruction, and stop condition |
| Project conventions | Yes | Medium | Generic worker `CLAUDE.md`; original `HEAD:CLAUDE.md` only via manual `git show` | A worker-safe subset: tools, test policy, commit style, protected files, no-go rules |
| Agent-specific guidance | Yes | Medium | Hook injects per-agent guidance | Keep it short, but add stop rules and scope limits |
| Codebase orientation | Sometimes | Low-Medium | None beyond the repo snapshot and whatever files the prompt names | A few entrypoints or exact files in the prompt; not a giant architecture dump |
| Accumulated learnings (`.napkin.md`) | Yes, selectively | Low-Medium | Not copied into worktrees; worker is told not to modify it | Read-only recent lessons, ideally a short curated excerpt |
| Cross-worker awareness | Usually no; sometimes yes | Low | None | Only file-overlap/conflict hints when relevant |
| Git state | Yes | Low-Medium | Inferable with `git`; resume prompt explicitly says to inspect status/log; base SHA hidden | Export branch/base SHA/root/session path; no prose needed |
| Prior art | Mostly on retries/escalations | Medium for retries, low otherwise | Retry carries log tail + exit code + recent events; escalation loses that context | Reuse retry context for escalations and include review findings |

## Notes by Category

### 1. Task Prompt

This is still the primary context object. Worker success mostly depends on prompt quality.

Current strengths:

- `pi` gets automatic numbered steps.
- absolute path rewriting prevents workers from editing the main repo by mistake.
- retries already append concrete failure evidence.

Current gaps:

- prompt structure is agent-specific and uneven,
- escalation discards prior failure context,
- no explicit scope ceiling is attached for long refactors,
- no explicit failure budget exists for agents that loop on tests.

Conclusion:

- strongest ROI is still better task prompts and retry/escalation prompts, not a retrieval system.

### 2. Project Conventions

Workers do need project conventions, but not the governor prompt.

Current state:

- the hook-generated `CLAUDE.md` carries a generic worker subset,
- the repo's tracked `CLAUDE.md` is governor-specific,
- the current advice to inspect `HEAD:CLAUDE.md` mixes useful rules with role-conflicting instructions.

Conclusion:

- workers need a dedicated worker-safe conventions artifact,
- not a pointer to the governor's instruction file.

### 3. Agent-Specific Guidance

dgov already has the right mechanism here: the hook.

What is missing is not more context volume. It is better operational rules:

- `pi`: always explicit final commit instruction, ideally with smaller-scope task discipline.
- `hunter`: explicit stop-after-N-identical-failures rule.
- `claude` / `gemini`: explicit scope ceiling for large refactors.
- `codex`: current adversarial guidance is already directionally correct.

### 4. Codebase Orientation

Workers do not need a persistent architecture encyclopedia for most tasks in a 9.6k-line codebase.

They need:

- the right files named in the prompt,
- maybe one or two entrypoints,
- and permission to explore with `rg`.

This is governor prompting and task shaping, not knowledge management.

### 5. Accumulated Learnings (`.napkin.md`)

The main repo napkin exists and contains real, relevant lessons:

- `claude` workers exceed context on 20+ file refactors,
- `hunter` spins on failing tests,
- `pi` sometimes fails to commit,
- decorator/parameter mismatches caused real breakage,
- protected file handling has caused issues.

But workers do not receive that file by default.

There is also a policy mismatch:

- the worker hook says "Do NOT modify `.napkin.md`",
- `.napkin.md` is in `PROTECTED_FILES`,
- merge protection treats it as read-only.

So the cheapest useful interpretation is:

- workers should read a napkin-derived artifact,
- not write back to `.napkin.md` directly.

### 6. Cross-Worker Awareness

Workers generally do not need to know the entire swarm state.

Full cross-worker awareness would usually be noise. The useful subset is narrower:

- "another worker is already changing these files,"
- or "this task depends on branch X being merged first."

That is dispatch-time coordination. It does not justify a shared worker knowledge base.

### 7. Git State

Workers need some git state, but only a few facts:

- current branch,
- main repo root or session root,
- base SHA,
- whether they are resuming or retrying.

Today they can infer some of this with `git`, but it is unnecessary friction because dgov already knows it.

### 8. Prior Art

Current support is partial:

- `resume` gives generic "inspect status/log" guidance,
- auto-retry appends the previous run's log tail, exit code, and recent events,
- escalation drops that context and just relaunches the task on a stronger agent.

That means prior art exists in the system, but is only partially fed forward.

## qmd Evaluation

The qmd idea is understandable: local markdown search with BM25, vector search, reranking, and context trees sounds like a clean way to query docs, napkins, audits, and design notes.

But for dgov, this is mostly the wrong layer to optimize first.

### What qmd would help with

- Searching many markdown artifacts quickly.
- Retrieving old audit notes, roadmaps, design docs, and napkin entries.
- Potentially giving a governor or large-context analysis agent a better way to find related prior docs.

### What qmd would not fix

- `pi` not committing.
- `hunter` looping on failing tests.
- workers touching protected files.
- `claude` being assigned a task that is too broad for its context window.
- hidden git/session facts like base SHA or slug not being exposed.
- escalation dropping failure context.

### Cost / complexity tradeoff

For this repo size, qmd looks like over-engineering if used as the core worker context system:

- extra subsystem,
- extra indexing / embedding lifecycle,
- new operational surface area,
- more hidden magic in worker startup,
- and a risk of solving retrieval while the real failures remain prompt/policy issues.

### Recommendation

Do not make qmd part of the default worker startup path.

If you want it at all, use it later as an optional governor-side research tool for searching markdown history. Do not make worker success depend on retrieval over markdown corpora.

## Failure Modes: Context vs Prompt vs Policy

| Failure mode | Type | Why | Cheapest fix |
|---|---|---|---|
| `pi` doesn't commit | Mostly prompt engineering | `pi` needs explicit end-state instructions | Keep auto-structuring, require explicit `git add` + `git commit` in all `pi` tasks, and fail review fast if there are no commits |
| `hunter` spins on test failures | Policy / prompt, not context | The model lacks a stopping rule, not information | Add "after 2 repeated failing test runs, stop and report" to Hunter guidance |
| Workers clobber `CLAUDE.md` | Enforcement more than context | Workers already get "don't touch it," but mistakes still happen | Keep protected-file enforcement; also surface protected files in the worker-safe conventions artifact |
| `claude` workers exceed context on 20+ file refactors | Task sizing / governor policy | The task is too broad for the assigned worker | Add a scope ceiling: if task spans too many files/subsystems, stop and return a split plan |

The pattern is consistent:

- these are mostly not "worker lacks searchable knowledge" failures,
- they are prompt, policy, and interface failures.

## Recommended Minimal Changes

### High priority

1. Stop pointing workers at `git show HEAD:CLAUDE.md` as the fallback conventions source.

   Replace it with a worker-safe conventions artifact. Two simple options:

   - add a small tracked worker conventions file,
   - or have the hook materialize a worker-safe conventions section derived from repo config.

2. Export the small set of dgov facts workers actually need into the worker shell, not just the hook.

   Suggested vars:

   - `DGOV_ROOT`
   - `DGOV_SESSION_ROOT`
   - `DGOV_SLUG`
   - `DGOV_AGENT`
   - `DGOV_BRANCH`
   - `DGOV_BASE_SHA`
   - `DGOV_WORKTREE_PATH`

   That is cheap and removes avoidable discovery friction.

3. Feed forward retry context on escalation too.

   Stronger agents should inherit:

   - last output tail,
   - exit code,
   - recent events,
   - and, ideally, prior review findings.

### Medium priority

4. Provide read-only accumulated learnings in a worker-safe form.

   Do not dump the entire governor napkin into the prompt. Instead:

   - copy a short curated learnings file into the worktree,
   - or materialize the latest relevant napkin bullets into a generated read-only file.

   Keep `.napkin.md` protected and read-only for workers.

5. Add agent-specific stop rules in the hook.

   Minimum useful additions:

   - `pi`: task must end in commit; if blocked, say exactly why.
   - `hunter`: stop after repeated identical failures.
   - `claude` / `gemini`: stop and propose task split if the task exceeds scope.

### Low priority

6. Add dispatch-time file overlap hints.

   If dgov already knows that an active pane is changing the same files, surface that fact to the new worker or, better, to the governor before dispatch.

7. Align docs with reality.

   Right now the docs imply more worker env/context than the code actually provides. In particular:

   - hook env is not worker env,
   - `DGOV_TDD_STATUS_FILE` appears documented but not implemented in current code.

## Bottom Line

The worker does not need a large context management system.

It needs:

- a sharp task prompt,
- worker-safe conventions,
- a few missing state facts surfaced explicitly,
- selective learnings from prior failures,
- and clear stop conditions.

That is a small product change set, not a retrieval architecture project.
