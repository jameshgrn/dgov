Build a structured worker prompt for dgov dispatch.

The user will describe a task. You will:

## Step 1: Analyze the task

Determine:
- **Scope**: single-file or multi-file?
- **Complexity**: micro-task (numbered steps) or design-decision (autonomous)?
- **Files involved**: read the source to identify exact file paths, function names, line numbers

## Step 2: Read the target files

Always read the actual code before constructing the prompt. Workers need accurate function names, class names, and line references. Wrong names cause import errors.

## Step 3: Choose prompt style

**Numbered steps** (single-file, mechanical changes, qwen-9b/4b):
```
1. Read <file>. Find <function/class>.
2. <Exact edit instruction with code block>
3. git add <files>
4. git commit -m "<message>"
```

**Autonomous** (multi-file, design decisions, qwen-35b):
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

- Single file, mechanical: `worker` (routes to qwen-9b)
- Single file, needs judgment: `worker` with `--agent qwen-35b`
- Multi-file (2-4 files): `worker` with `--agent qwen-35b`, autonomous prompt
- Large refactor / security audit: suggest LT-GOV

## Step 5: Output the dispatch command

```bash
uv run dgov pane create --land -a <agent> -s <slug> -r . -p "<prompt>"
```

Run with `run_in_background: true` so governor stays responsive.

## Rules
- NEVER put function/class names in prompts you haven't verified by reading the source
- ALWAYS end prompts with explicit git add + git commit
- ALWAYS use `--land` flag (full lifecycle: dispatch + wait + review + merge + close)
- Use logical agent names (qwen-35b, qwen-9b), never physical names (river-35b)
- Slug format: lowercase-kebab, descriptive, <30 chars
- For plan-driven work (>1 task, dependencies), suggest `dgov plan run` instead
