from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dgov.spim import models
from dgov.spim.engine import SimEngine
from dgov.spim.governor import Governor
from dgov.spim.locking import RegionLockManager
from dgov.spim.protocol import Action, Observation, Proposal

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def test_protocol_messages_are_json_serializable() -> None:
    observation = Observation(agent=1, region="reach-a", summary="high turbidity", confidence=0.9)
    proposal = Proposal(
        agent=2,
        region="reach-b",
        summary="deposit pulse",
        confidence=0.8,
        patch={"bed": {"elevation": 1.25}},
    )
    action = Action(agent=3, region="reach-c", delta={"delta_id": 7})

    assert json.loads(observation.to_json()) == observation.to_dict()
    assert Proposal.from_dict(json.loads(proposal.to_json())) == proposal
    assert Action.from_dict(json.loads(action.to_json())) == action


def test_region_lock_manager_expires_stale_locks(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    clock = Clock(datetime(2026, 3, 17, tzinfo=timezone.utc))
    manager = RegionLockManager(db_path, now_fn=clock.now)

    first_agent = models.create_agent(
        db_path,
        "observer",
        "reach-a",
        status="watching",
        spawned_at=models.isoformat_utc(clock.now()),
        expires_at=models.add_ttl(clock.now(), 60),
    )
    second_agent = models.create_agent(
        db_path,
        "observer",
        "reach-b",
        status="watching",
        spawned_at=models.isoformat_utc(clock.now()),
        expires_at=models.add_ttl(clock.now(), 60),
    )

    assert manager.acquire("reach-a", first_agent, 5) is True
    assert manager.acquire("reach-a", second_agent, 5) is False
    assert manager.check("reach-a") is not None

    clock.advance(seconds=6)

    assert manager.expire_stale() == 1
    assert manager.check("reach-a") is None
    assert manager.acquire("reach-a", second_agent, 5) is True


def test_governor_accepts_claim_and_engine_applies_delta(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    clock = Clock(datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc))
    governor = Governor(db_path, default_ttl=120, now_fn=clock.now)
    engine = SimEngine(db_path, state={"reach-a": {"bed": {"elevation": 0.5}}}, now_fn=clock.now)

    agent = governor.spawn("sedimentologist", "reach-a")
    claim_id = engine.propose(
        Proposal(
            agent=int(agent["id"]),
            region="reach-a",
            summary="increase bar height",
            confidence=0.87,
            patch={"bed": {"elevation": 1.5}, "flow": {"velocity": 0.9}},
        )
    )

    accepted = governor.accept(claim_id)
    processed = engine.tick()
    claim = models.get_claim(db_path, claim_id)
    delta = models.get_delta_for_claim(db_path, claim_id)
    updated_agent = models.get_agent(db_path, int(agent["id"]))

    assert accepted["status"] == "accepted"
    assert processed == 1
    assert claim is not None and claim["status"] == "applied"
    assert delta is not None and delta["applied_at"] is not None
    assert updated_agent is not None and updated_agent["status"] == "done"
    assert engine.state["reach-a"] == {
        "bed": {"elevation": 1.5},
        "flow": {"velocity": 0.9},
    }
