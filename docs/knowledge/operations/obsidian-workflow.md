---
id: obsidian-workflow
title: Obsidian Workflow
kind: operation
status: living
sources:
  - src/dgov/cli/kb.py
  - src/dgov/kb.py
related:
  - index
---

# Obsidian Workflow

You can open the repo root as an Obsidian vault to browse knowledge base
articles with Obsidian's graph view, backlinks, and search. The repo remains
the source of truth; Obsidian is only a viewer.

## Setup

1. Install [Obsidian](https://obsidian.md/).
2. Choose **Open folder as vault** and select the repo root.
3. The vault root will show all repo files. Navigate to `docs/knowledge/` for
   articles.

## Usage

- **Browse**: Use Obsidian's file explorer, graph view, or backlinks to move
  between articles.
- **Read**: Frontmatter renders as metadata; article text renders as Markdown.
- **Edit**: You can edit articles in Obsidian, but keep frontmatter strict.

## Validation and traversal

Always use `dgov` for validation and graph traversal:

```bash
uv run dgov kb validate      # check frontmatter, links, and sources
uv run dgov kb graph         # dump the article + source graph
uv run dgov kb related <id>  # follow related edges
uv run dgov kb path <from> <to>  # shortest path between articles
uv run dgov kb open <id>     # open an article in Obsidian
```

Obsidian's graph view is helpful for visual exploration, but `dgov kb graph`
is the canonical graph because it validates sources and resolves edges.

## Do not commit workspace state

Obsidian creates `.obsidian/` in the vault root. Do not commit it. The repo's
`.gitignore` should already ignore `.obsidian/` after `dgov init`; if not, add
it manually:

```gitignore
# Obsidian
.obsidian/
```

Personal plugins, themes, and workspace layouts belong to your local machine,
not the repo.
