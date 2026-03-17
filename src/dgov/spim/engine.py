"""Event bus and tick-driven simulation engine for SPIM."""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import models
from .locking import RegionLockManager
from .protocol import Action, Observation, Proposal

EventHandler = Callable[[dict[str, Any]], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._subscribers[event_type].append(handler)

    def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        for handler in self._subscribers.get(event_type, []):
            handler(payload)
        for handler in self._subscribers.get("*", []):
            handler({"event_type": event_type, **payload})


class SimEngine:
    def __init__(
        self,
        db_path: str | Path,
        *,
        event_bus: EventBus | None = None,
        state: dict[str, Any] | None = None,
        lock_ttl: float = 30.0,
        now_fn: Callable[[], datetime] = models.utc_now,
    ) -> None:
        self.db_path = Path(db_path)
        self.event_bus = event_bus or EventBus()
        self.state: dict[str, Any] = state if state is not None else {}
        self.lock_ttl = lock_ttl
        self._now_fn = now_fn
        self.lock_manager = RegionLockManager(self.db_path, now_fn=now_fn)
        self._blocked_retry_count: dict[int, int] = {}
        models.ensure_schema(self.db_path)

    def observe(self, message: Observation) -> int:
        claim_id = models.create_claim(
            self.db_path,
            message.agent,
            message.region,
            "observation",
            message.confidence,
        )
        models.update_agent(self.db_path, message.agent, status="watching")
        models.record_event(self.db_path, message.agent, "observation", message.to_dict())
        self.event_bus.publish(
            "observation.recorded",
            {"agent_id": message.agent, "claim_id": claim_id, "region": message.region},
        )
        return claim_id

    def propose(self, message: Proposal) -> int:
        claim_id = models.create_claim(
            self.db_path,
            message.agent,
            message.region,
            "proposal",
            message.confidence,
        )
        delta_id = models.create_delta(self.db_path, claim_id, message.patch)
        models.update_agent(self.db_path, message.agent, status="proposing")
        models.record_event(self.db_path, message.agent, "proposal", message.to_dict())
        self.event_bus.publish(
            "proposal.recorded",
            {
                "agent_id": message.agent,
                "claim_id": claim_id,
                "delta_id": delta_id,
                "region": message.region,
            },
        )
        return claim_id

    def act(self, message: Action) -> int:
        event_id = models.record_event(self.db_path, message.agent, "action", message.to_dict())
        self.event_bus.publish(
            "action.recorded",
            {"agent_id": message.agent, "event_id": event_id, "region": message.region},
        )
        return event_id

    def tick(self) -> int:
        processed = 0
        self.lock_manager.expire_stale()
        for agent_id in models.expire_agents(self.db_path, now=self._now_fn()):
            models.record_event(self.db_path, agent_id, "agent_expired", {})
            self.event_bus.publish("agent.expired", {"agent_id": agent_id})

        for claim in models.list_claims(self.db_path):
            if claim["status"] not in {"accepted", "blocked"}:
                continue
            claim_id = int(claim["claim_id"])
            if claim["status"] == "blocked":
                retries = self._blocked_retry_count.get(claim_id, 0)
                if retries >= 5:
                    continue
            n = self._process_claim(claim)
            processed += n
            if n == 0:
                self._blocked_retry_count[claim_id] = (
                    self._blocked_retry_count.get(claim_id, 0) + 1
                )
        return processed

    def run(self, *, ticks: int, sleep_seconds: float = 0.0) -> int:
        if ticks <= 0:
            raise ValueError(f"ticks must be positive, got {ticks!r}")

        processed = 0
        for _ in range(ticks):
            processed += self.tick()
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
        return processed

    def _process_claim(self, claim: dict[str, Any]) -> int:
        agent_id = int(claim["agent_id"])
        region = str(claim["region"])
        claim_id = int(claim["claim_id"])

        if not self.lock_manager.acquire(region, agent_id, self.lock_ttl):
            models.update_agent(self.db_path, agent_id, status="blocked")
            models.update_claim_status(self.db_path, claim_id, "blocked")
            models.record_event(
                self.db_path,
                agent_id,
                "claim_blocked",
                {"claim_id": claim_id, "region": region},
            )
            self.event_bus.publish(
                "claim.blocked",
                {"agent_id": agent_id, "claim_id": claim_id, "region": region},
            )
            return 0

        try:
            models.update_agent(self.db_path, agent_id, status="acting")
            delta = models.get_delta_for_claim(self.db_path, claim_id)
            if delta is not None and delta["applied_at"] is None and delta["reverted_at"] is None:
                self._apply_patch(region, dict(delta["patch_json"]))
                models.mark_delta_applied(self.db_path, int(delta["delta_id"]))
                models.record_event(
                    self.db_path,
                    agent_id,
                    "delta_applied",
                    {
                        "claim_id": claim_id,
                        "delta_id": int(delta["delta_id"]),
                        "region": region,
                    },
                )
                self.event_bus.publish(
                    "delta.applied",
                    {
                        "agent_id": agent_id,
                        "claim_id": claim_id,
                        "delta_id": int(delta["delta_id"]),
                        "region": region,
                    },
                )
            else:
                models.record_event(
                    self.db_path,
                    agent_id,
                    "claim_observed",
                    {"claim_id": claim_id, "region": region},
                )
                self.event_bus.publish(
                    "claim.applied",
                    {"agent_id": agent_id, "claim_id": claim_id, "region": region},
                )

            models.update_claim_status(self.db_path, claim_id, "applied")
            models.update_agent(self.db_path, agent_id, status="done")
            return 1
        finally:
            self.lock_manager.release(region, agent_id)

    def _apply_patch(self, region: str, patch: dict[str, Any]) -> None:
        current = self.state.get(region)
        if isinstance(current, dict):
            merged = dict(current)
            self._merge_dicts(merged, patch)
            self.state[region] = merged
            return
        self.state[region] = dict(patch)

    def _merge_dicts(self, target: dict[str, Any], patch: dict[str, Any]) -> None:
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._merge_dicts(target[key], value)
            else:
                target[key] = value
