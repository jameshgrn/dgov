"""Rule types: Severity, Tier, Finding, Fix."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(Enum):
    """Finding severity levels."""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    ADVISORY = "advisory"


class Tier(Enum):
    """Rule execution tiers."""

    T1 = 1
    T2 = 2
    T3 = 3


@dataclass
class Fix:
    """A suggested fix for a finding."""

    tier: Tier
    original: str
    replacement: str
    confidence: float
    explanation: str

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "original": self.original,
            "replacement": self.replacement,
            "confidence": self.confidence,
            "explanation": self.explanation,
        }


@dataclass
class Finding:
    """A lint finding produced by a rule."""

    rule_id: str
    tier: Tier
    title: str
    severity: Severity
    message: str
    match: str
    suggestion: str
    paragraph_idx: int
    char_offset: int
    source_line: int = 1
    source_offset: int = 0
    related: list[str] = field(default_factory=list)
    fix: Fix | None = None

    def to_dict(self) -> dict:
        result: dict = {
            "rule_id": self.rule_id,
            "tier": self.tier.value,
            "title": self.title,
            "severity": self.severity.value,
            "message": self.message,
            "match": self.match,
            "suggestion": self.suggestion,
            "paragraph_idx": self.paragraph_idx,
            "char_offset": self.char_offset,
            "source_line": self.source_line,
            "source_offset": self.source_offset,
            "related": self.related,
        }
        if self.fix is not None:
            result["fix"] = self.fix.to_dict()
        return result
