Generate a HANDOVER.md in the project root for the next governor session.

## Step 1: Gather state (run all in parallel)

```bash
# Git state
git rev-parse --abbrev-ref HEAD
git log --oneline -10

# dgov state
uv run dgov status -r . 2>/dev/null || echo "no active panes"
uv run dgov ledger list -r . -c bug -s open 2>/dev/null || echo "no open bugs"
uv run dgov ledger list -r . -c debt -s open 2>/dev/null || echo "no open debt"
uv run dgov ledger list -r . -c rule 2>/dev/null || echo "no rules"
```

For test health, do **not** run the full unit suite during handover. Reuse the latest targeted verification from the session, or if none exists, run only the smallest changed-area test slice and report that explicitly.

## Step 2: Write HANDOVER.md

Use this structure exactly. Be concise. Bullet points. No prose paragraphs.

```markdown
# HANDOVER

## Current State
- Branch: main at <sha>
- Tests: <N> passed, <N> failed
- Panes: <active/done/failed counts or "none">

## Completed This Session
- **<summary>** (`<sha>`): <what changed and why, one line>
- (repeat for each commit this session)

## Ledger Snapshot
### Open Bugs
- #<id> — <summary> (<severity>)
### Open Debt
- #<id> — <summary>
### Rules
- #<id> — <summary>
(omit empty sections)

## Lookup Cache
Every file path, function, class, config key discovered or read this session:
- `path/to/file.py` — what it does / why it matters
- `ClassName.method()` in `path/to/file.py:123` — what it does
This prevents the next session from wasting tokens re-discovering things.

## Open Issues
Unresolved problems, blockers, or partial work. Include file:line refs and fix approaches.

## Next Steps
Clear, actionable items referencing Lookup Cache entries.
```

## Step 3: Check skill freshness

Read these files and flag any that reference retired agents, wrong commands, or outdated patterns:
- `.claude/skills/dgov/SKILL.md` — bootstrap skill
- `.claude/commands/dgov-handover.md` — this skill
- `.claude/commands/dgov-dispatch.md` — dispatch prompt builder
- `.claude/commands/dgov-debrief.md` — session debrief

Cross-reference against CLAUDE.md Policy Core. If any skill contradicts a policy rule or references
something that no longer exists (dead agents, removed commands, old flag names), add to Open Issues:
```
- Skill `/dgov-<name>` is stale: <what's wrong and what it should say>
```

Also check: did this session change CLAUDE.md policy, agent routing, or CLI commands? If so,
verify the skills still match. The skills are static templates — they don't auto-update.

## Step 4: Ensure gitignored

```bash
grep -qxF 'HANDOVER.md' .gitignore 2>/dev/null || echo 'HANDOVER.md' >> .gitignore
```

## Rules
- Lead with facts, not narrative
- Lookup Cache is the most important section — list everything discovered
- Include commit SHAs so next governor can trace changes
- Do not run the full test suite from handover unless the user explicitly requests a push-time CI pass
- Open Issues should have enough detail to act on without re-reading code
- Do NOT include project architecture or background — CLAUDE.md and CODEBASE.md cover that
