# Handover: dgov skills reorganized and accessible to all agents

## Session context
- Date: 2026-03-29
- Branch: main @ 23b07ef
- Last commit: Add handover state

## Open work
- No active panes
- No open bugs in ledger

## Changes made this session
1. Reorganized dgov skills into function-based structure:
   - `dgov-bootstrap` — session start, environment checks
   - `dgov-plan` — primary dispatch (multi-step work)
   - `dgov-pane` — micro-task dispatch
   - `dgov-ledger` — knowledge operations
   - `dgov-handover` — session end

2. Removed deprecated skills:
   - `dgov` (replaced by `dgov-bootstrap`)
   - `dgov-governor` (split into `dgov-plan` + `dgov-pane`)

3. Made skills accessible to all agents:
   - Canonical source: `~/.agents/skills/`
   - Symlinks: `~/.claude/skills/`, `~/.codex/skills/`, `~/.pi/agent/skills/`

## Git status
- Deleted: `.claude/skills/dgov/SKILL.md`
- Deleted: `.pi/skills/dgov-handover/SKILL.md`
- Deleted: `skills/dgov-governor/SKILL.md`

## Blockers/debt
- None

## Next steps
1. Commit the deleted skill files (cleanup from reorganization)
2. Continue with any planned dgov development

## Notes
- All agents (Claude, Codex, Pi) now share canonical skill definitions
- No deprecated skills remain in any location
- Skills follow dgov principles: data over procedure, domain-first placement
