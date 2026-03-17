"""Governor-side control surface for SPIM agents and claims."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import models


class Governor:
    def __init__(
        self,
        db_path: str | Path,
        *,
        default_ttl: float = 60.0,
        now_fn=models.utc_now,
    ) -> None:
        self.db_path = Path(db_path)
        self.default_ttl = default_ttl
        self._now_fn = now_fn
        models.ensure_schema(self.db_path)

    def spawn(self, role: str, region: str) -> dict[str, Any]:
        spawned_at = self._now_fn()
        agent_id = models.create_agent(
            self.db_path,
            role,
            region,
            status="watching",
            spawned_at=models.isoformat_utc(spawned_at),
            expires_at=models.add_ttl(spawned_at, self.default_ttl),
        )
        models.record_event(
            self.db_path,
            agent_id,
            "agent_spawned",
            {"role": role, "region": region},
        )
        agent = models.get_agent(self.db_path, agent_id)
        if agent is None:
            raise RuntimeError(f"Failed to reload spawned agent {agent_id}")
        return agent

    def accept(self, claim_id: int) -> dict[str, Any]:
        claim = self._require_claim(claim_id)
        if claim["status"] == "applied":
            raise ValueError(f"Claim {claim_id} has already been applied")

        models.update_claim_status(self.db_path, claim_id, "accepted")
        models.record_event(
            self.db_path,
            int(claim["agent_id"]),
            "claim_accepted",
            {"claim_id": claim_id, "region": claim["region"]},
        )
        accepted = models.get_claim(self.db_path, claim_id)
        if accepted is None:
            raise RuntimeError(f"Failed to reload accepted claim {claim_id}")
        return accepted

    def reject(self, claim_id: int) -> dict[str, Any]:
        claim = self._require_claim(claim_id)
        if claim["status"] == "applied":
            raise ValueError(f"Claim {claim_id} has already been applied and cannot be rejected")

        models.update_claim_status(self.db_path, claim_id, "rejected")
        models.update_agent(self.db_path, int(claim["agent_id"]), status="done")
        models.record_event(
            self.db_path,
            int(claim["agent_id"]),
            "claim_rejected",
            {"claim_id": claim_id, "region": claim["region"]},
        )
        rejected = models.get_claim(self.db_path, claim_id)
        if rejected is None:
            raise RuntimeError(f"Failed to reload rejected claim {claim_id}")
        return rejected

    def retarget(self, agent_id: int, new_region: str) -> dict[str, Any]:
        self._require_agent(agent_id)
        models.update_agent(self.db_path, agent_id, focus_region=new_region, status="watching")
        models.record_event(
            self.db_path,
            agent_id,
            "agent_retargeted",
            {"region": new_region},
        )
        agent = models.get_agent(self.db_path, agent_id)
        if agent is None:
            raise RuntimeError(f"Failed to reload retargeted agent {agent_id}")
        return agent

    def escalate(self, agent_id: int, new_role: str) -> dict[str, Any]:
        self._require_agent(agent_id)
        models.update_agent(self.db_path, agent_id, role=new_role, status="idle")
        models.record_event(
            self.db_path,
            agent_id,
            "agent_escalated",
            {"role": new_role},
        )
        agent = models.get_agent(self.db_path, agent_id)
        if agent is None:
            raise RuntimeError(f"Failed to reload escalated agent {agent_id}")
        return agent

    def _require_agent(self, agent_id: int) -> dict[str, Any]:
        agent = models.get_agent(self.db_path, agent_id)
        if agent is None:
            raise ValueError(f"Unknown agent_id: {agent_id}")
        return agent

    def _require_claim(self, claim_id: int) -> dict[str, Any]:
        claim = models.get_claim(self.db_path, claim_id)
        if claim is None:
            raise ValueError(f"Unknown claim_id: {claim_id}")
        return claim
