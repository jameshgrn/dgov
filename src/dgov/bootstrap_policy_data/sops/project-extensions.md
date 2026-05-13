---
name: project-extensions
title: Project-Local Extensions
summary: When and how to create project-local verify recipes and SOPs instead of embedding repeated commands in task prompts.
applies_to: [project.toml, verify, recipe, sop, extension, local, convention]
priority: must
---
## When
- a project uses the same verification command in multiple task prompts
- a project has custom setup, lint, test, or coverage steps that repeat across plans
- a team wants to change a verify command once and have it apply to all future tasks

## Do
- move repeated verification commands into `[verify.<name>]` recipes in `.dgov/project.toml`
- create project-local SOPs in `.dgov/sops/` for conventions that are specific to this repo
- reference the verify recipe by name in task prompts instead of pasting the full command
- keep language-neutral examples in SOPs; reserve language-specific tooling to `project.toml`

Example `.dgov/project.toml` snippet:

```toml
[verify.test]
command = "pytest -q {test_dir}"
description = "Run targeted tests"

[verify.lint]
command = "ruff check {file}"
description = "Check formatting and style"
```

## Do Not
- paste the same long command string into every task prompt
- embed project-specific conventions in ad hoc prompt text when a local SOP would suffice
- treat `.dgov/project.toml` as a static template; update it when verification needs change

## Verify
- confirm that `dgov verify run <recipe-name>` works before committing the plan
- check that `.dgov/sops/` guidance is discoverable by workers during the `Orient` phase
- ensure verify recipes are referenced by name rather than inlined in prompt text

## Escalate
- if a verify recipe needs to vary per task and a single `project.toml` entry is insufficient
- if the project-local extension would duplicate a core SOP; prefer updating the core SOP instead
