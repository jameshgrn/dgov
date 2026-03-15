# dgov Design Decisions: Gemini Opinion

## Q1: Subpackages or flat modules?
**Recommendation: Nested subpackages (`dgov/state/`, `dgov/runtime/`, `dgov/merge/`)**

While "flat is better than nested" is a common Python mantra, `dgov` has outgrown it. The current 9.5k-line structure is held together by a fragile web of local imports to hide circular dependencies (e.g., between `waiter`, `lifecycle`, and `persistence`). Moving to subpackages isn't just about "looking organized"—it's about enforcing architectural boundaries that prevent the system from becoming a "big ball of mud."

Nested packages like `dgov/runtime/` (for execution logic) and `dgov/state/` (for the SQLite/persistence layer) allow us to group related logic and, more importantly, define clear internal APIs. It makes the codebase easier to reason about for new contributors and significantly simplifies unit testing by allowing developers to mock entire subsystems (like the `runtime`) without fighting a monolith.

## Q2: panes.py shim — delete or deprecate?
**Recommendation: A) Delete entirely in 0.9.0. Break all imports.**

`dgov` is an internal tool for power users and agent orchestrators. We are entering a stabilization phase where architectural correctness is more valuable than backward compatibility for leaked private helpers. Re-exporting underscore-prefixed functions like `_is_done` and `_trigger_hook` is an anti-pattern that encourages brittle dependencies.

If a function is internal, it should be private; if it’s useful to the library, it should be promoted to a stable, typed public API in a proper module. Keeping a shim for one version (Option B) just delays the inevitable pain and clutters the filesystem. Clean cuts are faster to heal.

## Q3: Mission primitive — replace batch or sit alongside it?
**Recommendation: A) Mission replaces batch.**

A "Mission" is the logical evolution of a "Batch." If a Batch is a DAG of tasks, and a Mission is a DAG of tasks with attached policies (review, merge, retry), then a Batch is simply a Mission where all policies are `Manual` or `Null`.

Introducing "Mission" as a separate primitive alongside "Batch" creates redundant code paths and user confusion. We should unify the orchestration engine. `batch.py` currently handles the DAG logic; we should rename or refactor it to `mission.py`, where a "batch" becomes a convenience constructor or a "simplified view" of a Mission. This prevents feature creep by forcing us to maintain one robust execution engine rather than two overlapping ones.

## Q4: Release vehicle
**Recommendation: C) Publish to PyPI as 0.x; break freely until 1.0.**

The current "local-only" model hinders adoption and testing. Engineering tools thrive when they are easy to install (`uv tool install dgov`). Publishing to PyPI as a 0.x release is the industry-standard way to signal that the API is in flux.

We should not be afraid of breaking changes in 0.x. The goal of this phase is to find the *correct* API, not to commit to a *wrong* one forever. By publishing to PyPI, we gain the benefits of versioned releases and easier distribution, while maintaining the "Unstable" shield until we are ready to commit to the 1.0 contract. This allows for aggressive stabilization without the friction of manual path management.
