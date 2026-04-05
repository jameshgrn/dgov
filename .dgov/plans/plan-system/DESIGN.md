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
| **SOP** | Markdown worker-context doc. Not runnable. Injected into worker prompt at dispatch. |
| **Invariant** | Hardcoded compile check (cycle, conflict, bad ref). Fails compile, no override. |
| **Plan tree** | Directory of TOML unit specs, organized by section. |
| **Unit** | Single worker task: prompt, file claims, depends_on. |
| **Governor** | Conversational AI (Claude Code). Authors plans + SOPs, picks SOPs per unit at compile. |
| **Worker** | Disposable LLM (Kimi) dispatched into a git worktree for one unit. |
| **Compile** | walker→merger→resolver→validator→sop-bundler → `_compiled.toml`. |
| **Deploy** | Run compiled plan, dispatch units, record to `deployed.jsonl`. |

## Tree structure

```
.dgov/plans/<plan-name>/
  _root.toml            # [plan] name, sections=[], sops=[]
  compile/              # compile pipeline machinery
  runtime/              # runtime/settlement feature tasks
  cli/                  # CLI commands
```

**Unit IDs**: path-qualified globally — `section/file.slug`
**Bare slugs**: same-file scope only. Cross-file refs must contain `/` (path-qualified).

## Compile pipeline

```
walker      → reads _root.toml, enumerates sections, collects *.toml files
merger      → flattens, assigns path-qualified IDs (section/file.slug)
resolver    → resolves depends_on (bare=same-file, qualified=cross-file)
validator   → hardcoded invariant checks: cycles, conflicts, bad refs, self-refs
sop-bundler → governor picks SOPs per unit, injects into system_prompt
              outputs _compiled.toml (flat, dispatch-ready)
```

**sop-bundler** issues ONE governor call that sees all units + all available SOPs,
emits unit→[sops] mapping. Mapping cached in `_compiled.toml` for deterministic
re-runs. Governor is load-bearing — no fallback.

## SOPs

Project-local at `.dgov/sops/*.md`. Prose, not runnable. Python in markdown is
illustrative, not executed. Examples:

- `testing.md` — pytest conventions, what to test, what to mock
- `linting.md` — ruff order (lint→format), zero warnings, inline-ignore justification
- `commits.md` — imperative mood, ≤72 char subject, one logical change
- `errors.md` — fail fast, clear messages, no silent swallow
- `style.md` — no commented-out code, absolute paths, explicit > clever

Governor attaches per unit. When no SOPs exist, bundler no-ops (unit prompts
unchanged).

### Promotion (draft → canonical)
Governor suggests SOPs for promotion when stable; human confirms. Canonical
SOPs land in the project's default set for new plans.

### SOPs are NOT policy checks
SOPs are thought artifacts — reference for workers. Enforcement comes from
settlement gates (lint, test, sentrux), not SOP validation.

## Invariants (hardcoded compile checks)

Fixed set in `src/dgov/plan_tree.py`. No plugin system. Fail compile, no override.

1. Dep cycles
2. File-claim conflicts between independent units
3. Unresolved depends_on refs
4. Self-references

Analogous to a type-checker's built-in errors.

## Deploy log

`.dgov/plans/deployed.jsonl` — single append-only file, filter by plan name:

```json
{"plan": "arch-refactor", "unit": "modularity/extract.runner", "sha": "abc", "ts": "..."}
```

## Status (v0 / branch state)

- Not executable yet. This branch = design-of-record.
- `dgov compile` unimplemented.
- `.dgov/sops/` empty. Bundler will no-op until SOPs authored.

## Out of scope for this branch

- Canonical SOP library (follow-up: decompose CLAUDE.md → ~5 files)
- `dgov init --with-sops` wizard
- Global SOP layering (`~/.config/dgov/sops/`)
- Drift/kill instrumentation
- Semantic review gate
- Resume/checkpoint
