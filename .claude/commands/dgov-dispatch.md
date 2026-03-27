Build a structured worker prompt for dgov dispatch.

The user will describe a task. You will:

## Step 1: Analyze the task

Determine:
- **Execution surface**: plan-driven work or true micro-task?
- **Scope**: single-file or multi-file?
- **Complexity**: micro-task (numbered steps) or design-decision (autonomous)?
- **Files involved**: read the source to identify exact file paths, function names, line numbers

Default bias: if the task spans multiple files, distinct file claims, or multiple dependent steps, recommend a plan TOML plus `uv run dgov plan run --wait`. Do not force ad-hoc panes onto plan-shaped work.

## Step 2: Read the target files

Always read the actual code before constructing the prompt. Workers need accurate function names, class names, and line references. Wrong names cause import errors.

## Step 3: Choose prompt style

**Numbered steps** (single-file, mechanical micro-task):
```
1. Read <file>. Find <function/class>.
2. <Exact edit instruction with code block>
3. git add <files>
4. git commit -m "<message>"
```

**Autonomous** (multi-file, design decisions, richer context):
```
Goal: <what and why>

Read these files first:
- `src/dgov/foo.py` — look at BarClass, follow the pattern
- `src/dgov/baz.py` — the interface you'll implement

Principles:
- <relevant CLAUDE.md rules>
- <project conventions>

Deliver:
- <concrete deliverables>
- Tests in tests/test_<module>.py
- git add <files> && git commit -m "<message>"
```

## Step 4: Choose agent role

- If the task is multi-step, dependent, or spans distinct file claims: stop and recommend `uv run dgov plan run`
- Single-file micro-task: `pane create --land` is acceptable
- Start with the cheapest policy-approved worker tier; do not pin physical backends
- Large refactor / security audit: suggest LT-GOV only when the task actually needs it

## Step 5: Output the dispatch command

For plan-driven work, output a recommendation to write a plan TOML and run:
```bash
uv run dgov plan run .dgov/plans/<name>.toml --wait
```

For true micro-tasks, output:
```bash
uv run dgov pane create --land --role worker -a <logical-agent> -s <slug> -r . -p "<prompt>"
```

If the repo default worker is acceptable, `-a` may be omitted. When present, `<logical-agent>` must be a router-approved logical name, not a provider-specific backend.

## Rules
- NEVER put function/class names in prompts you haven't verified by reading the source
- ALWAYS end prompts with explicit git add + git commit
- Use `--land` only on ad-hoc micro-task pane dispatches
- Use policy-approved logical routing identifiers only; never physical names
- Slug format: lowercase-kebab, descriptive, <30 chars
- Prefer `dgov plan run` for anything beyond a single well-scoped micro-task
- Prefer plan file claims over prompt heuristics whenever the task can be planned
