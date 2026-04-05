# Plan System Design

## Thesis

dgov is a CLI tool invoked by a conversational AI (Claude Code) during multi-day
planning sessions with the human. The governor-human loop produces granular plan
trees and markdown SOPs attached per-unit. Intense upfront planning makes
deployment deterministic at coarse-grained scale.

> Prevent writing code that hasn't been thought about before.

## Vocabulary

| Term | Meaning |
|---|---|
| **SOP** | Markdown worker-context doc. Not runnable. Prepended to unit prompts at compile. |
| **Invariant** | Hardcoded compile check. Fails compile, no override. |
| **Plan tree** | Directory of TOML unit specs, organized by section. |
| **Unit** | Single worker task: prompt, file claims, depends_on. |
| **Root unit** | Unit with empty `depends_on`. |
| **Governor** | Conversational AI. Authors plans + SOPs, picks SOPs per unit at compile. |
| **Worker** | Disposable LLM dispatched into a git worktree for one unit. |
| **Compile** | walker→merger→resolver→validator→sop-bundler → `_compiled.toml`. |
| **Deploy** | Run compiled plan via `dgov run`, append to `deployed.jsonl`. |

## Tree structure

```
.dgov/plans/<plan-name>/
  _root.toml            # [plan] name, summary, sections=[]
  compile/              # flat depth-1 directory of unit files
  runtime/
  cli/
  _compiled.toml        # output of compile; flat PlanSpec format
```

**Walker is flat, depth-1.** Nested subdirs under sections are ignored for v0.
**Declared section missing its dir** → hard error.
**Dir without a declared section** → silently ignored.

## Slug grammar

- **Bare slug**: `[A-Za-z0-9_-]+`. No `.`, no `/`. Same-file scope.
- **Path-qualified unit ID**: `<section>/<file-stem>.<bare-slug>`.
- **Cross-file ref**: contains `/` → path-qualified. Bare ref = same-file only.
- Unit IDs appear in TOML as quoted keys: `[tasks."compile/pipeline.merger"]`.

## Compile pipeline

```
walker      → reads _root.toml, enumerates sections, collects *.toml files (depth-1)
merger      → assigns path-qualified IDs; detects within-file slug dupes
resolver    → resolves depends_on (bare=same-file, qualified=cross-file)
validator   → cycles, unreachable units
sop-bundler → governor picks SOPs per unit; prepends SOP bodies to each prompt
              outputs _compiled.toml (flat PlanSpec format, dispatch-ready)
```

## Compile output format

`_compiled.toml` is the existing flat PlanSpec format — `[plan] + [tasks.<id>]`
— with three additions:

- Unit IDs as quoted TOML table keys (e.g. `[tasks."compile/pipeline.merger"]`)
- Each unit's `prompt` = concatenated SOP bodies + `\n\n` + original unit prompt
- `[plan]` gains `source_mtime_max` (ISO timestamp, newest source TOML) and
  `sop_set_hash` (hash of sorted SOP `(filename, title)` pairs)
- Each unit gains `sop_mapping` (list of SOP names picked by governor; cached
  to skip governor re-call when `sop_set_hash` is unchanged)

**Dispatch**: `dgov run .dgov/plans/<name>/_compiled.toml`. Reuses existing
runner, persistence, settlement — no new dispatch path.

## Integration with existing plan.py

Tree compile outputs `_compiled.toml` that parses directly via existing
`parse_plan_file` → `PlanSpec` → `compile_plan` → `DagDefinition`.

- **Tree validator** adds: cycles, unreachable units, self-refs, unresolved refs, within-file slug dupes.
- **Existing `validate_plan`** continues to check: file-claim conflicts between independent units.
- No duplication — each check lives in one place.

Claim semantics (both validators): `create ∪ edit ∪ delete`. `read` is not a
claim. Lifecycle ordering (create→edit→delete within a chain) is out of scope
for v0.

## SOPs

Project-local at `.dgov/sops/*.md`. Prose only. Governor selects via one LLM
call per compile, seeing all units + all SOP titles/summaries. Mapping cached
in `_compiled.toml` for deterministic re-runs.

**SopBundler protocol**: `LLMSopBundler` (production, no fallback) +
`IdentityBundler` (test stub, returns empty mapping). `dgov compile --dry-run`
selects the stub. Production path has no fallback — governor call is
load-bearing by design.

**Cache key**: `sop_set_hash` = SHA256 of sorted `(filename, title)` pairs.
Add/remove/rename a SOP, or edit a title → miss (governor re-called). Edits
to SOP bodies do NOT invalidate — workers always read current bodies at
compile. `dgov compile --recompile-sops` forces a miss.

**Empty `.dgov/sops/`**: bundler no-ops, units keep original prompts.

### Promotion
Governor suggests SOPs for promotion when stable; human confirms. Canonical
SOPs land in the project's default set.

### SOPs are NOT policy checks
Enforcement lives in settlement gates (lint, test, sentrux), not SOP validation.

## Invariants (hardcoded compile checks)

Fixed set in `src/dgov/plan_tree.py`. No plugin system. Fail compile, no override.

1. Slug grammar violations (merger)
2. Unresolved `depends_on` refs (resolver)
3. Self-references (resolver)
4. Dep cycles (validator)
5. Unreachable units — no path from any root unit (validator)

Within-file slug duplicates are rejected by `tomllib` at parse time (duplicate
tables are a TOML syntax error), so the merger inherits that check for free.

File-claim conflicts are enforced post-compile by existing `validate_plan`.

## Deploy log

`.dgov/plans/deployed.jsonl` — single append-only file, filter by plan name:

```json
{"plan": "arch-refactor", "unit": "modularity/extract.runner", "sha": "abc", "ts": "..."}
```

## Staleness detection

`dgov plan status` compares `source_mtime_max` (on `_compiled.toml`) to current
source TOML mtimes. If any source is newer → warn:
`compile stale; rerun 'dgov compile <plan-root>'`.

## Status (v0 / branch state)

- Not executable yet. This branch = design-of-record.
- `dgov compile` unimplemented.
- `.dgov/sops/` empty. Bundler will no-op until SOPs authored.

## Out of scope for this branch

- Canonical SOP library (follow-up: decompose CLAUDE.md → ~5 files)
- `dgov init --with-sops` wizard
- Global SOP layering (`~/.config/dgov/sops/`)
- Subdirs under sections (walker is flat depth-1)
- Lifecycle ordering (create→edit→delete within a chain)
- Drift/kill instrumentation
- Semantic review gate
- Resume/checkpoint
