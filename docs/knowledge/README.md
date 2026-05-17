# dgov Knowledge Base

This directory is a repo-local knowledge vault. It is meant for curated
explanations of dgov concepts, architecture, and operations.

It is not a second ledger. Durable bugs, rules, decisions, patterns, and debt
belong in `dgov ledger`. Operational law belongs in `.dgov/governor.md` and
worker execution guidance belongs in `.dgov/sops/`.

## Article Format

Every article except this README must be a Markdown file with strict
frontmatter:

```md
---
id: sentrux
title: Sentrux
kind: concept
status: living
sources:
  - .dgov/governor.md
related:
  - settlement-flow
---
```

Required fields:

- `id`: stable lowercase slug, unique across the vault
- `title`: article title; the first `#` heading must match it
- `kind`: one of `architecture`, `concept`, `index`, or `operation`
- `status`: one of `draft`, `living`, or `stable`
- `sources`: repo-relative canonical files the article derives from
- `related`: article IDs for graph navigation

`sources` must point outside `docs/knowledge/`. The KB pulls from canonical
repo state; it does not cite itself as authority.

Validate the vault with:

```bash
uv run dgov kb validate
```
