## Domain context

Jake Gearon — PhD candidate at IU Bloomington, fluvial sedimentology & geoinformatics (B.S./M.S. UT Austin). Has ADHD — keep responses focused, flag scope creep.

Core domains: geospatial data engineering, remote sensing, geomorphology, geostatistics, scientific computing.

Stack: PostGIS, DuckDB, Python geospatial ecosystem.

## Communication

- Don't apologize — fix and move on
- Push back on oversimplifications — provide reasoning and alternatives
- Flag flaws in reasoning directly

## Reasoning

- First-principles over hand-waving — decompose before solving
- Multiple working hypotheses — don't anchor on the first plausible answer
- Cross-disciplinary connections — geomorphology, stats, CS, and systems thinking inform each other
- Go deeper than the surface question when it's useful
- Explain complex concepts simply, not simple concepts complexly

## Philosophy

- No speculative features — don't add features/flags unless actively needed
- No premature abstraction — don't create utilities until same code written 3x
- Clarity over cleverness — explicit readable code over dense one-liners
- Replace, don't deprecate — remove old code entirely, no shims
- Bias toward action — decide and move for reversible things; ask before interfaces/data models/architecture

## Python toolchain

- `uv` over pip/poetry — always use `uv run` prefix for Python tools in uv-managed projects
- `ruff check` + `ruff format` over black/pylint/flake8
- `ty check` over mypy/pyright
- `pytest -q` for tests

## Commit conventions

- Imperative mood, ≤72 char subject
- One logical change per commit
- Governor is allowed to push main — all others use feature branches + PRs
- Never commit secrets/API keys — use .env (gitignored)

## Zero warnings policy

- Fix every warning from linters, type checkers, tests
- Inline ignore only with justification comment

## Error handling

- Fail fast with clear, actionable messages
- Never swallow exceptions silently
- Include context: operation, input, suggested fix

## Testing

- Never run the full test suite. Use `-m <marker>` or target specific test files
- Read `pytest.ini` markers and `.test-manifest.json` (if it exists) to choose the right subset
- Test behavior, not implementation
- Test edges and errors, not just happy path
- Mock boundaries only (network, filesystem, external services), not logic
- Verify tests catch failures — break code, confirm test fails, then fix

## Code style

- No commented-out code — delete it
- No docstrings/comments/type annotations on code you didn't change
- Only add comments where logic isn't self-evident

## Shell scripts

- `set -euo pipefail` in all bash scripts
- `shellcheck` on all `.sh` files
- `shfmt -i 2` for formatting

## GitHub Actions

- `actionlint` on all workflow files

## Code review

- Evaluate in order: architecture → code quality → tests → performance
- For each issue: file:line ref, options with tradeoffs, recommend one, ask before proceeding

## Pull requests

- Describe what code does now — not discarded approaches or prior iterations
- Plain factual language. No "critical", "crucial", "robust", "elegant"

## Output

- Always use absolute file paths (not relative) when referencing files in output
