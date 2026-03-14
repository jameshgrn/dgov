# Prompt templates

Templates allow you to reuse common prompts with variable substitution. They ensure that repetitive tasks are executed consistently across your team.

## What templates are

A template is a prompt with `{variable}` placeholders. When you create a pane using a template, you provide values for each variable via the `--var` flag.

## Built-in templates

dgov ships with several optimized templates:

| Name | Purpose | Default Agent |
|------|---------|---------------|
| `bugfix` | Fix a bug in a file with targeted tests | `pi` |
| `feature` | Implement a new feature with tests | `claude` |
| `refactor`| Refactor code while preserving behavior | `pi` |
| `test` | Write tests for an existing file | `pi` |
| `review` | Review code and output structured JSON findings | `claude` |

## Using templates

To use a template, pass its name with `-T` and any required variables with `--var`.

```bash
# Example bugfix
dgov pane create -T bugfix \
  --var file=src/parser.py \
  --var description="off-by-one in loop" \
  --var test_file=tests/test_parser.py
```

## Listing templates

See all available templates and their required variables:

```bash
dgov template list
```

## Showing template details

View the full text and metadata of a specific template:

```bash
dgov template show bugfix
```

## Creating user templates

User templates live in `.dgov/templates/` as TOML files. Any user template with the same name as a built-in will override it.

1. Create the directory: `mkdir -p .dgov/templates`
2. Generate a skeleton: `dgov template create my-template`
3. Save it to `.dgov/templates/my-template.toml`:

```toml
name = "my-template"
description = "A custom task template"
template = "Do {thing} in {file}. Use {library}."
required_vars = ["thing", "file", "library"]
default_agent = "pi"
```

## Override built-ins

To customize a built-in template like `bugfix` for your repo, simply create `.dgov/templates/bugfix.toml` with your preferred prompt text. dgov will prioritize your local version.
