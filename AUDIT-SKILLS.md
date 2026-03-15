# Audit: Handover + Napkin Skills for dgov Swarm Compatibility

Both skills were designed for a single Claude Code instance with a single conversation.
dgov runs a governor on `main` + N workers in git worktrees. Several assumptions break.

---

## Handover Skill

### What it does
- **Trigger**: `PreCompact` hook (fires when context window is nearing capacity)
- **Mechanism**: Pipes conversation transcript to `claude -p` subprocess, writes `HANDOVER.md`
- **Content**: Plan, Done, Lookup Cache (file paths + functions discovered), Next steps
- **Output**: `HANDOVER.md` in project root, gitignored

### How it breaks in dgov

| Problem | Detail |
|---------|--------|
| **Takes 6-8 minutes** | The `claude -p` subprocess summarization is painfully slow. PreCompact fires when context is nearly full — blocking for 6-8 minutes while Claude processes the transcript means the session is frozen. If context pressure is high, the instance may hit the hard limit before handover completes. This is unusable for any fast-moving workflow. |
| **Workers don't trigger PreCompact** | Workers are killed by `tmux kill-pane` or SIGTERM from dgov. They don't reach context compaction. The hook never fires for workers. |
| **Workers lack orchestration context** | A worker knows what code it changed. It doesn't know: what other panes exist, what's been merged, what failed, what's queued. A worker-issued handover is blind to the bigger picture. |
| **Workers can't write to main's HANDOVER.md** | Workers run in worktrees like `.dgov/worktrees/fix-auth-1/`. Their `HANDOVER.md` goes there, not to the main repo root. The governor wouldn't find it. |
| **Governor sessions are long-lived** | The governor may run for hours across many worker dispatches. A single PreCompact handover would only capture the governor's *own* chat history, not the workers' results. |

### Who should write HANDOVER.md?

**Only the governor.**

Workers produce structured artifacts (commits, test results, diff stats) that the governor already captures via `dgov pane review`. The governor has:
- Full pane state (active, done, failed, merged)
- Review diffs and verdicts
- Retry/escalation history
- The actual orchestration narrative ("dispatched X, merged Y, abandoned Z")

A worker handover would be redundant with `git log` on the worktree branch.

### Recommendations

1. **Remove PreCompact hook entirely — too slow to be useful** — 6-8 minutes is unacceptable for a hook that fires under time pressure. The `claude -p` subprocess approach is the bottleneck. Better options:
   - **Governor writes handover manually** when pausing work: `dgov handover` command that takes <1s (reads pane state, not a Claude subprocess)
   - **Template-based handover** — fill in a template from dgov's internal state (pane list, recent reviews) without calling Claude. Takes milliseconds.
   - **If Claude summarization is wanted**, run it async / non-blocking, never in a PreCompact hook

2. **Add worktree guard to hook** (until removed) — Add a guard at the top of `pre-compact-handover.sh`:
   ```bash
   GITDIR=$(git rev-parse --git-dir 2>/dev/null || echo "")
   if [[ "$GITDIR" == */.dgov/worktrees/* ]]; then
     echo "Skipping handover — worktree worker, not governor"
     exit 0
   fi
   ```

3. **Add governor-side handover command** — New `dgov handover` that writes `HANDOVER.md` from dgov's internal state:
   - Summarize all panes (dispatched, merged, failed, abandoned)
   - Include recent `dgov pane review` output
   - Reference the governor's session context (what was being worked on)
   - **No Claude subprocess** — just format existing state. <1s.

4. **Governor CLAUDE.md prompt should mention handover** — Add to the dgov governor instructions: "Periodically write a HANDOVER.md summarizing active mission state. Include: what panes are active, what was recently merged, what failed and why, what the next dispatch should be."

---

## Napkin Skill

### What it does
- **Trigger**: Manual (no hook). Claude reads `.napkin.md` at session start, updates it during session.
- **Mechanism**: Plain markdown file, gitignored. Claude appends corrections, preferences, patterns.
- **Content**: Corrections (what went wrong + what to do instead), user preferences, patterns that work/don't work, domain notes
- **Output**: `.napkin.md` in project root, gitignored

### How it breaks in dgov

| Problem | Detail |
|---------|--------|
| **Worktrees get empty .napkin.md** | `.napkin.md` is gitignored. Each worktree starts with no file. Workers never see the corrections learned from prior sessions. |
| **Workers lack cross-worker pattern awareness** | A worker knows *its own* mistake. It can't know "3 other workers made the same Click decorator/param mismatch" — only the governor sees that pattern. |
| **Workers' napkin entries vanish** | When a worktree is closed (`dgov pane close`), the worktree directory is deleted. Any `.napkin.md` written by the worker is lost. |
| **Main repo napkin is governor's, not workers'** | The existing `.napkin.md` is a governor session log. It tracks orchestration-level findings (merge cleanup bugs, resume prompt cascading). Workers can't write here. |

### Who should write the napkin?

**Primarily the governor. Workers can contribute structured findings that the governor aggregates.**

Workers *should* log their own mistakes — but only so the governor can collect them. Two-tier approach:

- **Worker napkin**: Ephemeral. Captures what the worker learned during its task. Written to worktree's `.napkin.md` (or stdout, or a structured file).
- **Governor napkin**: Persistent. Aggregates cross-worker patterns, orchestration failures, systemic issues. This is the existing `.napkin.md`.

### Recommendations

1. **Workers emit structured corrections on completion** — Add a worker output format (stdout or file) that lists:
   ```yaml
   corrections:
     - what_went_wrong: "Click crash from decorator/param mismatch"
       fix_applied: "Removed both @click.option and parameter"
       category: "code-change"
   ```
   The governor collects these during `dgov pane review`.

2. **Governor aggregates corrections into .napkin.md** — After reviewing a worker, the governor adds relevant findings to `.napkin.md`. The governor already does this manually (see existing napkin entries referencing worker mistakes).

3. **Copy .napkin.md into worktrees on creation** — When `dgov pane create` spawns a worker, copy the current `.napkin.md` into the worktree root. This gives workers access to accumulated learnings. Add to worktree creation:
   ```bash
   if [[ -f "${PROJECT_DIR}/.napkin.md" ]]; then
     cp "${PROJECT_DIR}/.napkin.md" "${WORKTREE_DIR}/.napkin.md"
   fi
   ```

4. **Add a napkin-diff step to pane review** — After a worker finishes, diff its `.napkin.md` against the original. Any new entries get extracted and appended to the governor's `.napkin.md`:
   ```bash
   diff <(cat main/.napkin.md) <(cat worktree/.napkin.md)
   ```

5. **Don't gitignore worktree napkins differently** — They're already isolated per worktree. The copy-on-creation + diff-on-review approach handles this without changing gitignore.

---

## Summary of Required Changes

| File | Change | Priority |
|------|--------|----------|
| `~/.claude/hooks/pre-compact-handover.sh` | **Delete or disable** — 6-8min is unusable. Replace with `dgov handover` command. | Critical |
| `src/dgov/cli.py` | Add `dgov handover` command (template-based, <1s, reads pane state) | High |
| `src/dgov/tmux.py` or `panes.py` | Copy `.napkin.md` into worktree on creation | Medium |
| `src/dgov/cli.py` | In `pane review`, extract worker napkin diffs | Low |
| `~/.claude/CLAUDE.md` | Document: governor writes handover, governor aggregates napkin | Low |
| Worker prompts (in `agents.py`) | Instruct workers to emit corrections in structured format | Low |

### What NOT to change

- Don't use Claude subprocess for anything synchronous in hooks — always too slow
- Don't move `.napkin.md` to a tracked file — gitignore is correct (session-specific learnings)
- Don't give workers write access to main's `.napkin.md` — governor is the aggregation point
