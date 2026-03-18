"""Aggregate statistics and health metrics from pane records and the event log."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from dgov.persistence import all_panes, read_events

_FAILURE_STATES = frozenset({"failed", "abandoned", "escalated"})
_SUCCESS_STATES = frozenset({"merged"})


def compute_stats(session_root: str) -> dict:
    """Compute aggregate stats from pane records and events."""
    panes = all_panes(session_root)
    events = read_events(session_root)

    # -- by_state --
    by_state: dict[str, int] = defaultdict(int)
    for p in panes:
        by_state[p["state"]] += 1

    # -- by_agent --
    agent_panes: dict[str, list[dict]] = defaultdict(list)
    for p in panes:
        agent_panes[p["agent"]].append(p)

    # Build per-slug event index for duration calculation
    slug_events: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        slug_events[ev["pane"]].append(ev)

    by_agent: dict[str, dict] = {}
    for agent, agent_pane_list in agent_panes.items():
        successes = sum(1 for p in agent_pane_list if p["state"] in _SUCCESS_STATES)
        failures = sum(1 for p in agent_pane_list if p["state"] in _FAILURE_STATES)
        total = len(agent_pane_list)
        success_rate = successes / total if total else 0.0

        durations: list[float] = []
        for p in agent_pane_list:
            evs = slug_events.get(p["slug"], [])
            created_ts = _find_event_ts(evs, "pane_created")
            end_ts = _find_event_ts(evs, "pane_merged") or _find_event_ts(evs, "pane_done")
            if created_ts and end_ts:
                dur = (end_ts - created_ts).total_seconds()
                if dur >= 0:
                    durations.append(dur)

        avg_duration_s = sum(durations) / len(durations) if durations else None

        by_agent[agent] = {
            "total": total,
            "success_rate": round(success_rate, 4),
            "avg_duration_s": round(avg_duration_s, 2) if avg_duration_s is not None else None,
            "failures": failures,
        }

    # -- recent_failures --
    failure_panes = [p for p in panes if p["state"] in _FAILURE_STATES]
    # Sort by last event timestamp descending
    for p in failure_panes:
        evs = slug_events.get(p["slug"], [])
        p["_last_event_ts"] = evs[-1]["ts"] if evs else ""

    failure_panes.sort(key=lambda p: p["_last_event_ts"], reverse=True)

    recent_failures = [
        {
            "slug": p["slug"],
            "agent": p["agent"],
            "state": p["state"],
            "last_event_ts": p["_last_event_ts"],
        }
        for p in failure_panes[:5]
    ]

    return {
        "total_panes": len(panes),
        "by_state": dict(by_state),
        "by_agent": by_agent,
        "recent_failures": recent_failures,
        "event_count": len(events),
    }


def _find_event_ts(events: list[dict], event_name: str) -> datetime | None:
    """Find the first event with the given name and parse its timestamp."""
    for ev in events:
        if ev["event"] == event_name:
            try:
                return datetime.fromisoformat(ev["ts"])
            except (ValueError, KeyError):
                return None
    return None
