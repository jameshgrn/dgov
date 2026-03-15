# Codex design decisions

## Q1. Subpackages or flat modules?

Recommendation: keep `src/dgov/` flat, rename the confusing modules, and split only the actual god files. Do not introduce broad `state/`, `runtime/`, `merge/`, `commands/` subpackages yet.

Why:
- 9.5k LOC is not a scale problem. Deep package grouping here is mostly theater borrowed from larger systems.
- The proposed grouping buys little over `dgov.store`, `dgov.launcher`, `dgov.waiter`, `dgov.merge`, `dgov.status`, etc., but it does add `__init__.py` glue, re-export decisions, and longer imports.
- The real issue is file responsibility, not namespace depth. `persistence.py` and `merger.py` are too wide; that does not imply `dgov.state.store` is better than `dgov.store`.
- Flat modules also keep the public import surface simple if this ever becomes a real library.

Adversarial point: my earlier subpackage proposal overfit “clean architecture” instincts. For this codebase size, that is more ceremony than clarity.

Exception: if `cli.py` is split into many command files, a `commands/` subpackage is justified. That is a real namespace, not decorative taxonomy.

## Q2. `panes.py` shim

Recommendation: delete it and update importers. No deprecation cycle.

Who actually uses it:
- Internal code: `cli.py`, `dashboard.py`, `preflight.py`, `batch.py`, `experiment.py`, `retry.py`, `review_fix.py`, `waiter.py`, `merger.py`.
- Tests: heavily. Many patch `dgov.panes.*`, including private names like `_trigger_hook`, `_full_cleanup`, `_is_done`, and `_count_active_agent_workers`.
- External users: no evidence.

Why:
- This is not a compatibility layer for users; it is an internal facade plus a test patch seam.
- Keeping it preserves the worst part of the current API: private underscore helpers treated as public.
- Deprecation warnings are wasted if there is no distribution channel and no known external consumers.

Adversarial point: option C is the tempting compromise, but it keeps dead weight around and delays the cleanup. If you want a facade later, reintroduce one intentionally with public names only.

## Q3. Mission primitive

Recommendation: mission should wrap a reduced batch core. So: **B**, but not by calling current `run_batch()` as-is.

Why:
- Replacing batch entirely turns mission into a god primitive that must own DAG parsing, tiering, dispatch, waiting, merge, review, retry, and policy. That is exactly the wrong direction for a small tool.
- Letting mission sit alongside batch duplicates the same DAG/tier machinery and creates two overlapping orchestration models.
- The right move is to extract the reusable kernel from `batch.py`: spec parsing, DAG validation, tier computation, and tier execution. Keep `batch` as the minimal “run this DAG once” primitive. Let `mission` add policy around it.

Adversarial point: “mission wraps batch” becomes bad indirection if mission literally shells into or calls `run_batch()`. The shared layer has to be a small executor, not the current 389-line mixed bag.

## Q4. Release vehicle and break aggressiveness

Recommendation: be aggressive now. Break imports, rename modules, delete shims, and tighten the public surface before PyPI is even considered.

Why:
- Current install mode is local editable use, not a distributed package with a real compatibility contract.
- Pretending there is an ecosystem to protect will freeze bad boundaries early.
- The right sequence is: simplify aggressively in `0.9.0`, dogfood it locally, then decide later whether a smaller, explicit API is stable enough for PyPI.

Constraint: “break freely” does not mean “break randomly.” Make one deliberate pass that removes accidental API surface and writes down what is actually public after the cleanup.
