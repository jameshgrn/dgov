# Handover: dgov release branch, sentrux hardening, and publish prep

**Date:** 2026-04-09T17:53:14Z  
**Branch:** `release-prep-polish` @ `1b71ce79`  
**Context:** This session turned the release-prep work from one large dirty diff into a release branch with coherent commits. The main outcomes were: provider-aware SOP bundling, root-resolution cleanup, installed-tool execution outside the source repo, stricter sentrux baseline semantics, `dgov init` bootstrap/guidance improvements, aligned agent guidance files, updated docs, and removal of stale dogfood artifacts.

---

## Current State

| Area | Status | Location | Notes |
|------|--------|----------|-------|
| Release branch split | Done | `/Users/jakegearon/projects/dgov` | Work is now split into 4 coherent commits on `release-prep-polish` |
| Sentrux integration | Done | `/Users/jakegearon/projects/dgov/src/dgov/cli/run.py`, `/Users/jakegearon/projects/dgov/src/dgov/settlement.py`, `/Users/jakegearon/projects/dgov/src/dgov/cli/init.py` | Baseline is explicit + governor-owned; final post-run compare is authoritative |
| Bootstrap/init UX | Done | `/Users/jakegearon/projects/dgov/src/dgov/cli/init.py` | `dgov init` now scaffolds governor guidance and offers immediate `dgov sentrux gate-save` when available |
| Public docs pass | Done | `/Users/jakegearon/projects/dgov/README.md`, `/Users/jakegearon/projects/dgov/docs/docs/pages` | Install/setup/provider/sentrux wording is now aligned with runtime behavior |
| Dogfood cleanup | Done | `/Users/jakegearon/projects/dgov/.dgov/plans/dogfood-debut` | Untracked plan dir removed; stale indexed leading-space path removed |

## Commits On This Branch

1. `10cd687c` — `chore: drop stale dogfood artifacts`
2. `88371e36` — `feat: add provider-aware SOP bundling`
3. `df13bb4a` — `feat: harden bootstrap and sentrux flow`
4. `1b71ce79` — `docs: standardize guidance and release docs`

## Remaining Work

1. Finish the GitHub release/publish steps for PyPI from `release-prep-polish`.
2. Decide whether to merge this branch to `main` before tagging, or tag directly from the release branch.
3. Bump version only if `0.1.0` is no longer the intended first public release.
4. After publish, run one real smoke with `uv tool install dgov` against PyPI rather than a local wheel.

## GitHub / PyPI Notes

- PyPI trusted publisher setup was completed on the PyPI side during the session.
- The repo already has `/Users/jakegearon/projects/dgov/.github/workflows/publish.yml` configured for OIDC trusted publishing.
- GitHub-side expectation: environment name `pypi`, workflow file `publish.yml`, tag trigger `v*`.

## Verification Performed

- `uv build`
- `uv run --with twine twine check dist/*`
- `uv run actionlint .github/workflows/*.yml`
- `uv run pytest -q -m unit /Users/jakegearon/projects/dgov/tests/test_settlement.py /Users/jakegearon/projects/dgov/tests/test_cli_run_strict.py`
- `uv run pytest -q -m unit /Users/jakegearon/projects/dgov/tests/test_cli.py /Users/jakegearon/projects/dgov/tests/test_settlement.py /Users/jakegearon/projects/dgov/tests/test_cli_run_strict.py`
- `uv run pytest -q -m unit /Users/jakegearon/projects/dgov/tests/test_cli.py`
- `uv run ruff check /Users/jakegearon/projects/dgov/src/dgov/settlement.py /Users/jakegearon/projects/dgov/src/dgov/cli/run.py /Users/jakegearon/projects/dgov/src/dgov/cli/sentrux.py /Users/jakegearon/projects/dgov/tests/test_cli.py /Users/jakegearon/projects/dgov/tests/test_settlement.py /Users/jakegearon/projects/dgov/tests/test_cli_run_strict.py`
- `uv run ruff check /Users/jakegearon/projects/dgov/src/dgov/cli/init.py /Users/jakegearon/projects/dgov/tests/test_cli.py`
- `cd /Users/jakegearon/projects/dgov/docs && npm run build`
- Clean-room installed-wheel smoke:
  - `uv tool install --from /Users/jakegearon/projects/dgov/dist/dgov-0.1.0-py3-none-any.whl dgov`
  - installed `dgov --version`
  - ran installed `dgov init` in a temp git repo
  - ran installed `dgov status`

## Working Tree

- The only expected uncommitted file at handoff is this handover itself: `/Users/jakegearon/projects/dgov/HANDOVER.md`

## References

- Publish workflow: `/Users/jakegearon/projects/dgov/.github/workflows/publish.yml`
- Sentrux runtime gate: `/Users/jakegearon/projects/dgov/src/dgov/cli/run.py`
- Settlement sentrux gate: `/Users/jakegearon/projects/dgov/src/dgov/settlement.py`
- Init/bootstrap flow: `/Users/jakegearon/projects/dgov/src/dgov/cli/init.py`
- Root resolution helper: `/Users/jakegearon/projects/dgov/src/dgov/project_root.py`
- Agent guidance canonical source: `/Users/jakegearon/projects/dgov/AGENTS.md`
