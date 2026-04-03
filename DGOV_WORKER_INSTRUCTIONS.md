# Worker Instructions — r1-race-2

You are a **worker**. Complete the task, commit, and signal done.

## Rules
- Read the claimed files before editing; trust the code, not the prompt.
- Edit only the task files unless the task explicitly requires more.
- Do not modify `.gitignore` or `pyproject.toml` unless asked.
- Do not push to remote.
- You are in a git worktree, not the main repo.
- NEVER use `dgov` commands. NEVER dispatch sub-workers. Edit files DIRECTLY.
## Start here
Exact edit claims:
- base.txt

Read first:
- base.txt
- src/dgov/waiter.py

Tests:
- tests/test_done_strategy.py
- tests/test_dgov_panes.py

Commit message: `Apply race-2`


## Tooling
- Lint: `uv run ruff check <file>` then `uv run ruff format <file>`
- Test: `uv run pytest <test_file> -q -m unit`
- Never run the full test suite — target specific test files

## Testing rules
- Every code change needs tests for new/changed behavior.
- Test behavior, not implementation. Assert outputs and errors.
- Use @pytest.mark.unit. Use tmp_path. Mock boundaries only.
- Test edges: empty, None, zero, max size, error cases.
- **When deleting or renaming code**: check .test-manifest.json
  for affected test files, then grep those files for references
  to deleted functions/classes/commands. Delete or update the tests.
- **Run affected tests before committing**: if any fail, fix them.
  Do NOT leave broken tests for the governor.

## Commit checklist
1. Run ruff check + format on changed files
2. Check .test-manifest.json for test files related to your changes
3. If you deleted/renamed any function, class, or CLI command:
   grep the test files for references and fix/delete them
4. Run affected tests: uv run pytest <test_files> -q -m unit
5. Fix any test failures — do NOT commit with failing tests
6. git add <all changed files including test files>
7. git commit -m "<message>"
8. Verify commit exists: git log --oneline $DGOV_BASE_SHA..HEAD
9. Run `dgov worker complete` ONLY after step 8 succeeds
10. If the task is already done or no changes are needed,
    run `dgov worker complete -m 'already implemented'`

## Task

1. Read base.txt. 2. Append ok on a new line. 3. git add base.txt 4. git commit -m "race" 5. dgov worker complete

## Evals to satisfy
- [ADHOC_EVAL] happy_path: Ad-hoc task completed
  Evidence: true
