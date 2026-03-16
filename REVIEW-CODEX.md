# Codex Adversarial Review of DESIGN-V2

## Overall verdict

`/Users/jakegearon/projects/dgov/DESIGN-V2.md` is not safe to implement as written.

The LT-GOV concept currently collapses the governor/worker boundary. The shared `.dgov/` file channels are unauthenticated mutable state with race windows. The SQLite/WAL argument is overstated for the command pattern proposed. The crash model is mostly "manual cleanup later", which means split-brain is the default, not an edge case.

## Findings

### 1. Critical: the LT-GOV is either impossible to run or effectively a second unrestricted governor

Sources:
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:98`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:104`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:137`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:152`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/cli/__init__.py:22`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/cli/__init__.py:72`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/cli/pane.py:25`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/cli/pane.py:38`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/lifecycle.py:202`

Why this is broken:
- The design assumes a worker running inside a git worktree can call `dgov pane create/wait/review/merge/close`.
- The current CLI explicitly rejects non-info `dgov` subcommands inside a worktree unless `DGOV_SKIP_GOVERNOR_CHECK=1` is set.
- If you set that bypass for LT-GOVs, they are not "tier-limited". They inherit ambient authority over any `--project-root` and `--session-root` they can name.
- Worse, `dgov pane util` already exists and will run arbitrary commands in a utility pane. A bypassed LT-GOV can spawn unrestricted helper panes outside its worktree.
- The proposed `metadata.role` and `metadata.parent_ltgov` fields are display metadata, not authorization. Nothing in the command path checks them.

Impact:
- An LT-GOV can create, merge, close, or inspect panes outside its tier.
- An LT-GOV can target another repo or session root.
- An LT-GOV can break out of "never edit directly" by issuing arbitrary `dgov` or shell-adjacent commands.

Options:
- Add a brokered control plane where LT-GOVs can only submit typed requests and only the governor executes them.
- Add capability tokens scoped to a tier and enforce them on every mutating CLI command.
- Keep the current prompt-only LT-GOV idea.

Recommendation:
- Use the broker. The prompt-only model is not a security boundary.

### 2. Critical: the file-based packet channels are shared mutable memory with no ownership, atomicity, or integrity

Sources:
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:161`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:173`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:203`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:222`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/dashboard.py:189`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/lifecycle.py:203`

Why this is broken:
- Every worker gets `DGOV_ROOT` and `DGOV_SESSION_ROOT`, so it can see the shared `.dgov/` tree.
- The design makes `.dgov/progress/`, `.dgov/advisories/`, and `.dgov/attention/` writable coordination channels.
- The dashboard reads `progress/<slug>.json` directly and silently ignores parse errors. That is exactly what torn writes look like in production.
- There is no temp-file-plus-rename protocol, no file lock, no monotonic sequence number, no owner identity, and no lease/epoch in the file payload.
- Any worker can overwrite another worker's status file, forge a tier summary, or poison another worker's attention file.

Impact:
- Cross-tier spoofing is trivial.
- Concurrent writers can produce partial JSON and dashboard blind spots.
- Crashed workers leave stale files that look current unless the reader invents its own staleness rules.
- "Human-readable for debugging" becomes "human-editable shared state", which is the opposite of trustworthy coordination.

Options:
- Move cross-actor packets into SQLite as append-only events.
- Use per-pane append-only JSONL journals with sequence numbers and atomic rename.
- Keep the single-file-per-slug design.

Recommendation:
- Use append-only event records for authority-bearing communication. File caches are fine as derived views, not as the control plane.

### 3. High: the SQLite/WAL argument is too weak for concurrent LT-GOV orchestration, and metadata updates are lossy

Sources:
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:139`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:177`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:484`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/persistence.py:231`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/persistence.py:276`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/persistence.py:309`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/persistence.py:456`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/status.py:168`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/done.py:102`

Why this is broken:
- WAL helps readers not block readers. It does not turn SQLite into a multi-writer coordinator. There is still one writer at a time.
- The design's concurrency example is `3 LT-GOVs x 5 workers = 18 processes`, many of which will be polling, waiting, emitting events, and updating state.
- `busy_timeout=5000` with five retries is not much headroom once you have lock convoys around merge, retry, review, and waiter activity.
- `set_pane_metadata()` is a read-modify-replace cycle implemented as `SELECT` plus `INSERT OR REPLACE`. Two concurrent metadata updates can clobber each other without any conflict signal.
- `list_worker_panes()` calls `_is_done()` on active panes, which can write state and emit events from a hot status path. That amplifies write pressure in exactly the place you want cheap reads.

Impact:
- Intermittent `database is locked` failures under load are plausible.
- Metadata fields like `parent_ltgov`, retry counters, or future tier annotations can be silently lost.
- A monitoring path can become a write amplifier.

Options:
- Introduce a single writer process/thread and treat SQLite as a serialized event sink.
- Normalize LT-GOV linkage into real columns and use targeted `UPDATE` statements instead of JSON blob replacement.
- Leave the current per-process write pattern and hope WAL is enough.

Recommendation:
- Serialize writes through a broker or at least remove read-modify-replace metadata updates before adding LT-GOV concurrency.

### 4. High: the parent/child model is not durable, so split-brain and cross-tier ownership bugs are built in

Sources:
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:146`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:152`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:404`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:458`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/persistence.py:190`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/persistence.py:214`

Why this is broken:
- The design explicitly says there is no formal parent-child relationship in the DB for MVP.
- Then it relies on prompt memory, metadata, and a vague slug convention to infer topology.
- Slugs are globally unique pane primary keys, not tier-scoped task IDs.
- The example text says workers created by `ltgov-tier1` will have slugs like `fix-parser` and `add-metrics`, which is not a prefix scheme at all.
- If an LT-GOV crashes and the governor respawns the tier, nothing prevents duplicate logical tasks, child theft, or stale tier summaries referring to the wrong generation.

Impact:
- You cannot answer basic questions reliably: who owns this worker, which tier generation created it, is this summary current, can this LT-GOV legally merge this slug?
- Adoption and orphan handling become guesswork.
- Dashboard grouping becomes cosmetic while the real orchestration state diverges underneath it.

Options:
- Add durable `parent_slug`, `tier_id`, `epoch`, and `assigned_by` fields to state.
- Keep metadata-only grouping and hope prompts stay coherent.

Recommendation:
- Add durable topology to the DB before implementing LT-GOV automation. This is not premature abstraction; it is the minimum identity model.

### 5. High: the failure model is mostly manual triage, which means orphaned workers and stale summaries will accumulate

Sources:
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:486`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:491`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/done.py:202`
- `/Users/jakegearon/projects/dgov/.dgov/worktrees/review-design-codex/src/dgov/status.py:281`

Why this is broken:
- The design's recommended LT-GOV crash policy is "orphans continue running; governor manually triages".
- That is not a failure mode; it is deferred inconsistency.
- Existing cleanup only handles dead panes with missing worktrees or orphaned directories. It does not reconcile live workers whose supervisor died.
- There is no LT-GOV heartbeat, no lease expiry, no "tier done" barrier, no child quarantine on parent death, and no cleanup strategy for stale progress/advisory/attention files.

Impact:
- A dead LT-GOV can leave live workers continuing to merge into main with no active owner.
- A restarted LT-GOV can redispatch work already in flight.
- The governor cannot distinguish "parent crashed" from "parent slow" from "stale summary file".

Options:
- Add a governor-owned reconciliation loop with LT-GOV heartbeats and child lease expiry.
- Auto-close all children on parent death.
- Keep manual triage as the primary recovery path.

Recommendation:
- Use governor-owned leases plus reconciliation. If the governor is still the only trusted actor, it must also be the only authoritative scheduler.

### 6. Medium: the SPIM terrain budget is internally inconsistent and likely optimistic

Sources:
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:245`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:269`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:331`
- `/Users/jakegearon/projects/dgov/DESIGN-V2.md:386`

Why this is broken:
- The design says terrain updates run at about 5 FPS, "every 3rd dashboard refresh tick", while pane data refreshes every 1 second.
- If the terrain updates every third tick at 5 FPS, the full dashboard is implicitly redrawing around 15 FPS. That is aggressive for a Rich `Layout` with panels, keyboard handling, log-derived summaries, and terminal diffing.
- `30x40` is only `1200` cells, but SPIM is not just painting 1200 characters. It implies multiple passes over elevation, drainage/area, erosion parameters, and color mapping before Rich renders the sidebar.
- The visual payoff also degrades quickly. At 15 active panes, region packing gives tiny basins, so the metaphor loses legibility before it proves its worth.

Impact:
- The first implementation is likely to trade dashboard responsiveness for a sidebar that does not encode enough stable information to justify the budget.
- The stated frame target is not evidence-backed.

Options:
- Prototype and profile SPIM first, then set the frame budget from measurements.
- Decouple simulation tick from dashboard redraw and cap terrain to 1-2 FPS.
- Replace SPIM with a cheaper basin heatmap or sparkline-style state map.

Recommendation:
- Do not commit to 5 FPS. Profile a prototype first; absent measurements, assume 1 FPS maximum and make terrain strictly optional.

## Bottom line

I would not start Phase 3 from this design. The missing piece is not a better prompt template. It is an actual authority model:

- governor-only execution of mutating `dgov` actions
- durable tier and parent identity in the DB
- append-only, authenticated coordination state
- leases/heartbeats for LT-GOV liveness
- measured, not asserted, terminal rendering budgets

## Ask before proceeding

If you want, the next useful step is a hardened alternative design for LT-GOVs built around a brokered command queue and DB-backed leases, instead of prompt-only delegation.
