"""State file management and event journal.

Manages .dgov/state.json (pane records) and .dgov/events.jsonl (event log).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dgov import tmux

logger = logging.getLogger(__name__)

# -- Event log --

VALID_EVENTS = frozenset(
    {
        "pane_created",
        "pane_done",
        "pane_resumed",
        "pane_timed_out",
        "pane_merged",
        "pane_merge_failed",
        "pane_escalated",
        "pane_superseded",
        "pane_closed",
        "pane_retry_spawned",
        "checkpoint_created",
        "review_pass",
        "review_fail",
    }
)


def _emit_event(session_root: str, event: str, pane: str, **kwargs) -> None:
    """Append a structured event to .dgov/events.jsonl."""
    from datetime import datetime, timezone

    if event not in VALID_EVENTS:
        raise ValueError(f"Unknown event: {event!r}. Valid: {sorted(VALID_EVENTS)}")
    events_path = Path(session_root) / _STATE_DIR / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "pane": pane,
        **kwargs,
    }
    with open(events_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# -- Pane record --


# Canonical pane states — no others allowed
PANE_STATES = frozenset(
    {
        "active",
        "done",
        "failed",
        "reviewed_pass",
        "reviewed_fail",
        "merged",
        "merge_conflict",
        "timed_out",
        "escalated",
        "superseded",
        "closed",
        "abandoned",
    }
)


def _validate_state(state: str) -> str:
    """Validate and return a canonical pane state. Raises ValueError for unknown states."""
    if state not in PANE_STATES:
        raise ValueError(f"Unknown pane state: {state!r}. Valid: {sorted(PANE_STATES)}")
    return state


@dataclass
class WorkerPane:
    slug: str
    prompt: str
    pane_id: str
    agent: str
    project_root: str
    worktree_path: str
    branch_name: str
    created_at: float = field(default_factory=time.time)
    owns_worktree: bool = True
    base_sha: str = ""
    state: str = "active"

    def __post_init__(self) -> None:
        _validate_state(self.state)


# -- State file helpers --

_STATE_DIR = ".dgov"
_PROTECTED_FILES = {"CLAUDE.md", "THEORY.md", "ARCH-NOTES.md", ".napkin.md"}
_STATE_FILE = "state.json"


def _state_path(session_root: str) -> Path:
    return Path(session_root) / _STATE_DIR / _STATE_FILE


def _read_state(session_root: str) -> dict:
    path = _state_path(session_root)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"panes": []}


def _write_state(session_root: str, state: dict) -> None:
    path = _state_path(session_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.rename(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _add_pane(session_root: str, pane: WorkerPane) -> None:
    state = _read_state(session_root)
    # Upsert: remove any existing entry with the same slug before appending
    state["panes"] = [p for p in state["panes"] if p.get("slug") != pane.slug]
    state["panes"].append(asdict(pane))
    _write_state(session_root, state)


def _remove_pane(session_root: str, slug: str) -> None:
    state = _read_state(session_root)
    state["panes"] = [p for p in state["panes"] if p.get("slug") != slug]
    _write_state(session_root, state)


def _get_pane(session_root: str, slug: str) -> dict | None:
    state = _read_state(session_root)
    return next((p for p in state["panes"] if p.get("slug") == slug), None)


def _all_panes(session_root: str) -> list[dict]:
    return _read_state(session_root).get("panes", [])


def _update_pane_state(session_root: str, slug: str, new_state: str) -> None:
    """Update the state field of a pane record."""
    _validate_state(new_state)
    state = _read_state(session_root)
    for p in state["panes"]:
        if p.get("slug") == slug:
            p["state"] = new_state
            break
    _write_state(session_root, state)

    # Update tmux pane title to reflect new status
    pane = _get_pane(session_root, slug)
    if pane:
        pane_id = pane.get("pane_id", "")
        agent = pane.get("agent", "")
        if pane_id:
            tmux.update_pane_status(pane_id, agent, slug, new_state)
