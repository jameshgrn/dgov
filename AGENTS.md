# dgov Agent Guidance

Instruction Pack Version: `1.0.0`  
Status: `LOCKED`  
Canonical Source: `AGENTS.md`  
Mirrors: `CLAUDE.md`, `GEMINI.md`

These files are intentionally synchronized. Edit `AGENTS.md` first, copy the
same content into the mirrors, and bump the version in all three files in the
same change.

## Read First

1. `HANDOVER.md`
2. `.napkin.md`
3. `.dgov/governor.md`

## Authority Order

1. Direct user and system instructions
2. `.dgov/governor.md`
3. This instruction pack
4. `.dgov/sops/*.md` for worker execution guidance

If this file disagrees with `.dgov/governor.md`, follow `.dgov/governor.md` and
treat this pack as stale.

## Operating Rules

- Act as the governor, not an inline feature implementer.
- Prefer plan-mediated execution through `.dgov/plans/<name>/`.
- Keep tasks atomic and file claims explicit.
- Do not restate general governance rules inside task prompts.
- Update repo-level guidance in `.dgov/governor.md` or `.dgov/sops/*.md`,
  not in one-off prompts.

## Toolchain

- Use `uv run` for Python tooling.
- Lint with `uv run ruff check .`.
- Format with `uv run ruff format .`.
- Type check with `uv run ty check`.
- Never run the full test suite. Target the narrowest relevant tests with
  `uv run pytest -q -m <marker>` or specific test files.

## Governor Loop

1. Read `HANDOVER.md`, `.napkin.md`, and `.dgov/governor.md`.
2. Check live state with `dgov status`.
3. Author or adjust plan files in `.dgov/plans/<name>/`.
4. Compile with `uv run dgov compile <dir>` and validate before execution.
5. Run with `uv run dgov run <plan-or-compiled-file>`.
6. Monitor with `uv run dgov watch`.
7. Use `uv run dgov plan status <dir>` and targeted verification before
   closing work.
