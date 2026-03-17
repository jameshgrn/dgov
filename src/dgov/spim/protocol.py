"""Structured SPIM messages exchanged between region-scoped agents and the engine."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Observation:
    agent: int
    region: str
    summary: str
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Observation":
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class Proposal:
    agent: int
    region: str
    summary: str
    confidence: float
    patch: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Proposal":
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class Action:
    agent: int
    region: str
    delta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Action":
        return cls(**payload)
