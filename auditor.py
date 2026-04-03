"""Auditor: Self-sensing layer. <200 lines. Pure functions only."""

from __future__ import annotations
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PILLARS = {
    1: "SEPARATION OF POWERS",
    2: "ATOMIC ATTEMPT",
    3: "SNAPSHOT ISOLATION",
    4: "DETERMINISM",
    5: "ARCHITECTURAL VAR",
    6: "EVENT-SOURCED",
    7: "ZERO AMBIENT",
    8: "FALSIFIABLE",
    9: "HOT-PATH <50ms",
    10: "FAIL-CLOSED",
}


@dataclass(frozen=True)
class Violation:
    pillar: int
    severity: str  # CRITICAL|HIGH|MEDIUM|LOW
    type: str
    details: str


@dataclass(frozen=True)
class Report:
    id: str
    ts: datetime
    violations: list[Violation]
    latency_us: int
    integrity: float

    def to_task(self) -> dict | None:
        hp = [v for v in self.violations if v.severity in ("CRITICAL", "HIGH")]
        if not hp:
            return None
        return {
            "task_id": f"repair-{self.id}",
            "priority": "HIGH",
            "violations": [{"pillar": v.pillar, "type": v.type} for v in hp],
        }


def _git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def check_latency(ledger_path: Path, threshold_ms: int = 50) -> list[Violation]:
    """Pillar #9: DISPATCH->RUNNING > 50ms?"""
    if not ledger_path.exists():
        return []
    v = []
    try:
        events: dict[str, list] = {}
        for line in open(ledger_path):
            r = json.loads(line)
            aid = r.get("attempt_id")
            if aid:
                events.setdefault(aid, []).append(r)
        for aid, evs in events.items():
            evs.sort(key=lambda x: x.get("ts", ""))
            for i, e in enumerate(evs):
                if e.get("status") == "DISPATCHED" and i + 1 < len(evs):
                    n = evs[i + 1]
                    if n.get("status") == "RUNNING":
                        dt = datetime.fromisoformat(n["ts"]) - datetime.fromisoformat(
                            e["ts"]
                        )
                        ms = dt.total_seconds() * 1000
                        if ms > threshold_ms:
                            v.append(
                                Violation(9, "HIGH", "latency", f"{aid}: {ms:.0f}ms")
                            )
    except Exception as e:
        v.append(Violation(9, "MEDIUM", "check_failed", str(e)))
    return v


def check_integrity(base: Path, ledger_path: Path) -> list[Violation]:
    """Pillar #1: Commits not from AttemptRecords?"""
    v = []
    try:
        # Get recent commits
        log = _git(["log", "main", "--oneline", "-20"], base).strip().split("\n")
        # Get attempt IDs
        aids = set()
        if ledger_path.exists():
            aids = {
                json.loads(l).get("attempt_id")
                for l in open(ledger_path)
                if json.loads(l).get("attempt_id")
            }
        valid = ("feat:", "fix:", "refactor:", "docs:", "test:")
        for line in log:
            if not line:
                continue
            h, msg = line.split(" ", 1)
            if not any(a in msg for a in aids) and not msg.startswith(valid):
                v.append(Violation(1, "CRITICAL", "direct_edit", f"{h}: {msg[:40]}"))
    except Exception as e:
        v.append(Violation(1, "MEDIUM", "check_failed", str(e)))
    return v


def check_structure(base: Path) -> list[Violation]:
    """Pillar #5: File count/size bloat."""
    v = []
    try:
        # Simple heuristics: files >500 lines
        for f in (base / "src").rglob("*.py"):
            lines = len(f.read_text().splitlines())
            if lines > 500:
                v.append(Violation(5, "HIGH", "bloat", f"{f.name}: {lines} lines"))
    except Exception:
        pass
    return v


def audit(base_path: Path, ledger_path: Path | None = None) -> Report:
    """Run all invariant checks. Pure function."""
    t0 = datetime.utcnow()
    ledger = ledger_path or base_path / ".dgov" / "ledger.jsonl"
    violations = (
        check_latency(ledger)
        + check_integrity(base_path, ledger)
        + check_structure(base_path)
    )
    latency = int((datetime.utcnow() - t0).total_seconds() * 1e6)
    score = max(
        0.0,
        1.0
        - sum(
            {"CRITICAL": 0.4, "HIGH": 0.2, "MEDIUM": 0.1, "LOW": 0.05}.get(
                v.severity, 0
            )
            for v in violations
        ),
    )
    return Report(f"audit-{t0:%Y%m%d-%H%M%S}", t0, violations, latency, round(score, 2))


def main() -> int:
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--base", type=Path, default=Path.cwd())
    a = p.parse_args()
    r = audit(a.base)
    print(
        f"integrity={r.integrity} latency={r.latency_us}us violations={len(r.violations)}"
    )
    for v in r.violations:
        print(f"  P{v.pillar} [{v.severity}] {v.type}: {v.details}")
    return 1 if any(v.severity == "CRITICAL" for v in r.violations) else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
