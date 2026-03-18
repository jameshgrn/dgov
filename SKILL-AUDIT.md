# Skill & Plugin Audit

Audited 2026-03-18. Scope: all custom skills + all installed plugins.

Jake's profile: Python/geospatial PhD, dgov governor/worker workflow, PostGIS/DuckDB, ADHD — noise costs attention.

## Verdict Table

### Custom Skills

| Name | Verdict | Reason |
|------|---------|--------|
| **napkin** | KEEP | Core workflow — per-repo mistake tracker, always-on, low overhead |
| **search-conversations** | KEEP | Custom PostgreSQL search of 449K+ AI messages; more targeted than episodic-memory plugin |
| **dgov** | KEEP | Core workflow — governor bootstrap, environment checks, agent readiness |
| **verification-before-completion** | KEEP | Prevents false "done" claims; directly relevant to dgov worker completion flow |
| **systematic-debugging** | KILL | 1,504 words. Claude already knows how to debug. Rarely triggered; when it is, it dumps ~2K tokens of process you already internalize. Overkill for dgov dispatch workflow where you debug by reading worker output, not by following a 4-phase ritual. |
| **test-driven-development** | KILL | 1,496 words. Prescriptive TDD doctrine. Workers write code, not the governor. When workers need TDD guidance it belongs in worker CLAUDE.md, not the governor's system prompt. |
| **receiving-code-review** | KILL | 929 words. Style guide for handling PR feedback. Good content but purely behavioral — if needed, put 3 key rules in CLAUDE.md instead of a full skill. |
| **writing-skills** | KILL | 3,204 words — the single largest custom skill. Meta-skill for authoring new skills. Used maybe once a quarter. When needed, can be read manually via `cat`. Doesn't need to be discoverable every session. |

### Plugins

| Name | Verdict | Reason |
|------|---------|--------|
| **context7** | KEEP | MCP server for live library docs. Lightweight (~30 tok). Actually useful for Python package APIs. |
| **github** | KEEP | MCP server for GitHub operations. Lightweight. Used for PRs/issues. |
| **swift-engineering** | KILL | **17 skills + 11 agents** for iOS/Swift/SwiftUI/TCA. ~1,500 tok/session. Jake doesn't write Swift. Largest single source of prompt bloat. |
| **playwright** | KILL | 25+ MCP tools for browser automation. ~200 tok in deferred tools list. Not testing web UIs. |
| **frontend-design** | KILL | Skill for building "distinctive, production-grade frontend interfaces." Python/geospatial PhD, not web design. |
| **playground** | KILL | Creates interactive HTML playgrounds. Not part of the workflow. |
| **elements-of-style** | KILL | Strunk's writing rules. Claims ~12K tokens when the reference file loads. Claude writes fine without it. |
| **episodic-memory** | KILL | Semantic search for past conversations via MCP server + agent. Redundant — search-conversations custom skill queries the same PostgreSQL archive directly without the MCP overhead. |
| **feature-dev** | KILL | Feature development workflow with 3 specialized agents. Completely redundant with dgov (which IS the feature dev workflow). |
| **code-review** | KILL | Automated PR review agents. dgov has its own review flow (`dgov pane review`). |
| **pr-review-toolkit** | KILL | Specialized PR review agents. Overlaps with code-review plugin AND dgov review. |
| **ralph-loop** | KILL | Run Claude in a while-true loop. dgov handles iteration through dispatch/wait/retry. |
| **code-simplifier** | KILL | Code simplification agent. `/simplify` skill already built into Claude Code. Redundant. |
| **commit-commands** | KILL | Git commit/push/PR shortcuts. Claude already handles git natively. Zero value-add. |
| **hookify** | KILL | Hook creation from conversation patterns. 4 commands + 1 agent + 1 skill. Useful concept but rare — hooks can be written directly in settings.json. Not worth permanent prompt real estate. |
| **claude-code-setup** | KILL | Recommends Claude Code automations. One-time setup tool, not ongoing. |
| **claude-md-management** | KILL | CLAUDE.md auditor/improver. Occasional use at best. When needed, the user can ask directly. |
| **greptile** | KILL | AI code review for GitHub/GitLab PRs. Not using Greptile service. |

## Prompt Bloat Analysis

### Per-session system prompt injection (always loaded, every conversation)

Each installed skill/plugin injects description text into the system prompt's skill listing and agent type descriptions. These tokens are consumed before any work begins.

| Source | Items | Est. Tokens/Session | Notes |
|--------|-------|---------------------|-------|
| **swift-engineering** | 17 skills + 11 agents | **~1,500** | Single biggest offender. 0% relevance. |
| **feature-dev** | 1 skill + 3 agents | ~230 | Redundant with dgov |
| **hookify** | 1 skill + 1 agent + 4 commands | ~180 | Rarely used |
| **playwright** | 25+ MCP tools | ~200 | Deferred tool names add up |
| **episodic-memory** | 1 skill + 1 agent + 2 commands + 2 MCP | ~150 | Redundant with custom skill |
| **claude-md-management** | 1 skill + 2 commands | ~90 | Rarely used |
| **code-simplifier** | 1 skill + 1 agent | ~70 | Redundant with /simplify |
| **claude-code-setup** | 1 skill | ~65 | One-time use |
| **frontend-design** | 1 skill | ~50 | Irrelevant |
| **elements-of-style** | 1 skill | ~50 | Irrelevant |
| **playground** | 1 skill | ~50 | Irrelevant |
| **ralph-loop** | 3 commands | ~50 | Redundant with dgov |
| **commit-commands** | 3 commands | ~40 | Redundant with native git |
| **greptile** | MCP tools | ~20 | Not using service |
| **code-review** | 1 command | ~20 | Redundant with dgov |
| **pr-review-toolkit** | 1 command | ~20 | Redundant with code-review |
| Custom: writing-skills | 1 skill | ~20 | Rarely needed |
| Custom: systematic-debugging | 1 skill | ~20 | Claude knows this |
| Custom: test-driven-development | 1 skill | ~20 | Workers handle TDD |
| Custom: receiving-code-review | 1 skill | ~40 | Behavioral, not operational |
| **Total killable** | | **~2,885** | |

### When triggered (loaded on demand)

| Skill | Words | Est. Tokens | Trigger frequency |
|-------|-------|-------------|-------------------|
| writing-skills | 3,204 | ~4,160 | Quarterly |
| systematic-debugging | 1,504 | ~1,950 | Monthly |
| test-driven-development | 1,496 | ~1,940 | Monthly |
| receiving-code-review | 929 | ~1,200 | Per PR review |
| elements-of-style reference | ~9,000 | ~12,000 | Never for this user |
| hookify writing-rules | ~2,800 | ~3,600 | Rarely |

### Summary

- **Current baseline bloat from skills/plugins: ~3,200+ tokens/session**
- **After cleanup: ~315 tokens/session** (napkin, search-conversations, dgov, verification, context7, github)
- **Savings: ~2,885 tokens/session (~90% reduction)**
- Removes **28 skill descriptions**, **16 agent types**, and **27+ MCP tools** from every conversation's system prompt

For ADHD: fewer options = less decision fatigue for the model = faster, more focused responses.

## Recommended Custom Skills (replacements)

Instead of 8 custom skills + 18 plugins, keep 4 custom skills + 2 plugins:

### 1. napkin (KEEP as-is)
Per-repo mistake tracker. Always-on. No changes needed.

### 2. dgov (KEEP as-is)
Governor bootstrap. Core workflow. No changes needed.

### 3. search-conversations (KEEP as-is)
Custom PostgreSQL archive search. Replaces episodic-memory plugin entirely.

### 4. verification-before-completion (KEEP, consider slimming)
668 words is reasonable. Could trim rationalization table to save ~200 words, but not urgent. This is the one behavioral skill worth keeping — it prevents the single most common failure mode (claiming work is done when it isn't).

### What NOT to replace

**systematic-debugging + test-driven-development**: Don't replace these with a combined skill. The dgov workflow delegates implementation to workers. The governor doesn't debug or write tests — workers do. If workers need these processes, put a 3-line version in the `worktree_created` hook's worker CLAUDE.md, not in the governor's skill set.

**receiving-code-review**: Put the 3 useful rules directly in CLAUDE.md (already mostly there via "Push back on oversimplifications" and "No speculative features"). No separate skill needed.

**writing-skills**: Archive to `~/.claude/skills/writing-skills/` but remove from active discovery. Read it manually when creating a new skill.

## Plugin Consolidation

### Kill list (16 plugins to remove)

```bash
# These can be uninstalled via claude plugin remove or equivalent
swift-engineering       # 17 skills + 11 agents, 0% relevance
playwright              # 25+ MCP tools, not testing web UIs
feature-dev             # 3 agents, redundant with dgov
episodic-memory         # MCP server, redundant with custom skill
hookify                 # 4 commands + agent, write hooks directly
code-review             # redundant with dgov review
pr-review-toolkit       # redundant with code-review + dgov
ralph-loop              # redundant with dgov dispatch/retry
code-simplifier         # redundant with /simplify
commit-commands          # redundant with native git
frontend-design         # irrelevant to workflow
playground              # irrelevant to workflow
elements-of-style       # unnecessary for this user
claude-code-setup       # one-time use
claude-md-management    # occasional, not worth permanent slot
greptile                # not using the service
```

### Keep list (2 plugins)

```
context7                # lightweight MCP for library docs
github                  # MCP for GitHub operations
```

### What a single custom plugin could replace

A `dgov-toolkit` plugin bundling napkin + dgov + verification + search-conversations would:
- Consolidate 4 skills into 1 installable unit
- Make the setup portable across machines
- Keep the same functionality with zero bloat increase

This is worth doing only if Jake uses Claude Code on multiple machines. Otherwise, the current custom skills directory is fine.

## Action Items

1. Remove 16 plugins (see kill list above)
2. Delete 4 custom skills: systematic-debugging, test-driven-development, receiving-code-review, writing-skills
3. Move writing-skills to an archive location (it's useful reference, just not as an always-discovered skill)
4. Add 3 lines from receiving-code-review to CLAUDE.md if not already covered
5. Verify the cleanup: restart Claude Code, check system prompt is leaner
