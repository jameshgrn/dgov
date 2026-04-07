---
name: git-commits
title: Git & Commit Standards
---
## Git Standards
- **Feature Branches:** Never push directly to `main`. Use feature branches and PRs.
- **One Logical Change:** Each commit should be one logical, atomic change.
- **No Staging:** Do not stage or commit unless explicitly requested.

## Commit Messages
- **Imperative Mood:** Use the imperative mood (e.g., "Add", "Fix", "Update").
- **Length:** Subject line must be ≤72 characters.
- **Format:** Prefer messages that explain *why* over *what*.
- **HEREDOC:** Use HEREDOC for multi-line commit messages.
- **Security:** Never commit secrets, API keys, or `.env` files.
