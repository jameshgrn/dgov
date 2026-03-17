from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dgov.spim import models
from dgov.spim.engine import EventBus, SimEngine
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


def test_governor_reject(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    models.ensure_schema(db_path)
    clock = Clock(datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc))
    governor = Governor(db_path, default_ttl=120, now_fn=clock.now)
    engine = SimEngine(db_path, now_fn=clock.now)

    agent = governor.spawn("scout", "reach-a")
    claim_id = engine.propose(
        Proposal(
            agent=int(agent["id"]),
            region="reach-a",
            summary="test proposal",
            confidence=0.8,
            patch={"foo": "bar"},
        )
    )

    rejected = governor.reject(claim_id)
    claim = models.get_claim(db_path, claim_id)

    assert rejected["status"] == "rejected"
    assert claim is not None and claim["status"] == "rejected"


def test_governor_retarget(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    models.ensure_schema(db_path)
    clock = Clock(datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc))
    governor = Governor(db_path, default_ttl=120, now_fn=clock.now)

    agent = governor.spawn("scout", "region-a")
    models.update_agent(db_path, int(agent["id"]), status="idle")
    agent = models.get_agent(db_path, int(agent["id"]))
    assert agent["focus_region"] == "region-a"

    retargeted = governor.retarget(int(agent["id"]), "region-b")

    assert retargeted["focus_region"] == "region-b"


def test_governor_escalate(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    models.ensure_schema(db_path)
    clock = Clock(datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc))
    governor = Governor(db_path, default_ttl=120, now_fn=clock.now)

    agent = governor.spawn("scout", "reach-a")
    assert agent["role"] == "scout"

    escalated = governor.escalate(int(agent["id"]), "critic")

    assert escalated["role"] == "critic"


def test_eventbus_wildcard() -> None:
    bus = EventBus()
    received: list[dict] = []

    def handler(payload: dict) -> None:
        received.append(payload)

    bus.subscribe("*", handler)
    bus.publish("test.event", {"foo": "bar", "baz": 42})

    assert len(received) == 1
    assert received[0]["event_type"] == "test.event"
    assert received[0]["foo"] == "bar"
    assert received[0]["baz"] == 42


def test_engine_observe(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    models.ensure_schema(db_path)
    clock = Clock(datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc))
    agent_id = models.create_agent(
        db_path,
        "scout",
        "reach-a",
        status="idle",
        spawned_at=models.isoformat_utc(clock.now()),
        expires_at=models.add_ttl(clock.now(), 60),
    )
    engine = SimEngine(db_path, now_fn=clock.now)

    claim_id = engine.observe(
        Observation(
            agent=agent_id,
            region="reach-a",
            summary="high turbidity",
            confidence=0.9,
        )
    )

    claim = models.get_claim(db_path, claim_id)
    agent = models.get_agent(db_path, agent_id)

    assert claim is not None and claim["kind"] == "observation"
    assert agent is not None and agent["status"] == "watching"


def test_lock_exclusion(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    models.ensure_schema(db_path)
    clock = Clock(datetime(2026, 3, 17, tzinfo=timezone.utc))
    manager = RegionLockManager(db_path, now_fn=clock.now)

    agent_a = models.create_agent(
        db_path,
        "observer",
        "reach-a",
        status="watching",
        spawned_at=models.isoformat_utc(clock.now()),
        expires_at=models.add_ttl(clock.now(), 60),
    )
    agent_b = models.create_agent(
        db_path,
        "observer",
        "reach-b",
        status="watching",
        spawned_at=models.isoformat_utc(clock.now()),
        expires_at=models.add_ttl(clock.now(), 60),
    )

    assert manager.acquire("reach-a", agent_a, 30) is True
    assert manager.acquire("reach-a", agent_b, 30) is False


def test_lock_expiry(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    models.ensure_schema(db_path)
    clock = Clock(datetime(2026, 3, 17, tzinfo=timezone.utc))
    manager = RegionLockManager(db_path, now_fn=clock.now)

    agent_id = models.create_agent(
        db_path,
        "observer",
        "reach-a",
        status="watching",
        spawned_at=models.isoformat_utc(clock.now()),
        expires_at=models.add_ttl(clock.now(), 60),
    )

    assert manager.acquire("reach-a", agent_id, 2.0) is True
    assert manager.check("reach-a") is not None

    clock.advance(seconds=3)
    assert manager.expire_stale() == 1
    assert manager.check("reach-a") is None


def test_update_agent_nonexistent(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    models.ensure_schema(db_path)

    with pytest.raises(ValueError, match="Unknown agent_id"):
        models.update_agent(db_path, 99999, status="idle")


def test_state_transition_guard(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    models.ensure_schema(db_path)
    clock = Clock(datetime(2026, 3, 17, tzinfo=timezone.utc))

    agent_id = models.create_agent(
        db_path,
        "scout",
        "reach-a",
        status="done",
        spawned_at=models.isoformat_utc(clock.now()),
        expires_at=models.add_ttl(clock.now(), 60),
    )

    with pytest.raises(ValueError, match="Invalid transition"):
        models.update_agent(db_path, agent_id, status="watching")


def test_reject_with_other_claims(tmp_path) -> None:
    db_path = tmp_path / "spim.db"
    models.ensure_schema(db_path)
    clock = Clock(datetime(2026, 3, 17, 12, 0, tzinfo=timezone.utc))
    governor = Governor(db_path, default_ttl=120, now_fn=clock.now)

    agent = governor.spawn("scout", "reach-a")
    agent_id = int(agent["id"])

    claim_1 = models.create_claim(db_path, agent_id, "reach-a", "proposal", 0.8)
    models.create_claim(db_path, agent_id, "reach-a", "proposal", 0.8)

    governor.reject(claim_1)

    agent_after = models.get_agent(db_path, agent_id)
    assert agent_after is not None
    assert agent_after["status"] == "idle"
