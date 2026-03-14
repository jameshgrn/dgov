# state management
"""State module — reads pane state."""

from __future__ import annotations

from dgov.panes import list_worker_panes


def get_status(project_root: str, session_root: str | None = None) -> dict:
    """Get full dgov status as JSON-serializable dict."""
    panes = list_worker_panes(project_root, session_root=session_root)
    return {
        "panes": panes,
        "total": len(panes),
        "alive": sum(1 for p in panes if p["alive"]),
    }
