---
name: dgov-handover
author: Jake Gearon
version: 1.0.0
date: 2026-03-28
---

# dgov handover

When user invokes `/dgov-handover` or asks to "hand over" or "prepare handover":

1. **Gather state** (run these, capture output):
   ```bash
   uv run dgov ledger list -r . -c bug -s open 2>/dev/null || true
   uv run dgov pane list -r . 2>/dev/null || true
   git log --oneline -5
   git status --short
   ```

2. **Write HANDOVER.md** with structure:
   ```markdown
   # Handover: <brief summary>

   ## Session context
   - Date: <ISO date>
   - Branch: <current branch>
   - Last commit: <sha> - <message>

   ## Open work
   - <pane slug>: <state> - <one-line description>
   - <pane slug>: <state> - <one-line description>

   ## Open bugs/issues (from ledger)
   - #<id>: <summary> (<severity>)

   ## Blockers/debt
   - <anything blocking progress>

   ## Next steps
   1. <specific actionable item>
   2. <specific actionable item>

   ## Notes
   - <anything else worth knowing>
   ```

3. **Commit HANDOVER.md**:
   ```bash
   git add HANDOVER.md
   git commit -m "Add handover state"
   ```

4. **Report**: "Handover written to HANDOVER.md and committed."
