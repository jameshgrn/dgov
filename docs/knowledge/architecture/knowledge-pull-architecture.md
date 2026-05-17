---
id: knowledge-pull-architecture
title: Knowledge Pull Architecture
kind: architecture
status: living
sources:
  - .dgov/governor.md
  - src/dgov/kb.py
related:
  - index
  - ledger
  - failure-shapes
---

# Knowledge Pull Architecture

The knowledge base is derived, not authoritative. Articles pull from canonical
repo surfaces through explicit `sources` frontmatter. The validator checks that
those sources exist, that they are repo-relative, and that articles do not cite
other KB pages as authority.

This boundary prevents the KB from becoming a shadow policy system. If an
article discovers a rule, bug, decision, or recurring failure pattern, the
durable update belongs in the ledger or in the governor charter. The article
can explain the idea afterward, but it should still cite the governing source.

The practical contract is:

- code owns executable behavior
- `.dgov/governor.md` owns planning and operating law
- `.dgov/sops/` owns worker-facing execution guidance
- `dgov ledger` owns durable memory
- `docs/knowledge/` owns source-backed explanation

This makes articles easy to browse in editors like Obsidian while keeping
dgov's truth surfaces explicit and testable.
