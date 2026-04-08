# Contributing to dgov

Thank you for your interest in contributing to dgov! This guide outlines the workflow and standards we follow.

## Development Setup

We use `uv` for dependency management. To set up your environment:

```bash
# Install dependencies
uv pip install -e .

# Install dev dependencies (for testing, linting)
uv pip install -e ".[dev]"
```

## Code Quality

### Linting and Formatting

We use `ruff` for both linting and formatting:

```bash
# Check for lint errors
uv run ruff check src/ tests/

# Auto-fix lint issues
uv run ruff check --fix src/ tests/

# Format code
uv run ruff format src/ tests/
```

### Testing

Run the test suite with pytest:

```bash
# Run all tests (quiet mode)
uv run pytest tests/ -q

# Run specific test file
uv run pytest tests/test_kernel.py -q
```

## Workflow

### Feature Branches

Never push directly to `main`. All changes must be made via feature branches and pull requests:

1. Create a new branch from `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. Make your changes with atomic commits

3. Push your branch and open a pull request against `main`

### Commit Messages

Use atomic commits that represent one logical change. Write commit messages in the **imperative mood** (e.g., "Add", "Fix", "Update"):

```bash
# Good
Add validation for plan TOML files
Fix race condition in kernel event loop
Update worker timeout to 60 seconds

# Bad
Added validation
Fixed stuff
Updates
```

Keep the subject line to 72 characters or less. Prefer messages that explain *why* over *what*.

### Pull Request Guidelines

Before submitting a PR:

1. Ensure tests pass: `uv run pytest tests/ -q`
2. Run linting: `uv run ruff check src/ tests/`
3. Format code: `uv run ruff format src/ tests/`
4. Keep PRs focused on a single logical change

## Questions?

Open an issue if you have questions about contributing or need clarification on any of these guidelines.
