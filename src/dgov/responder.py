"""Auto-respond to blocked worker panes.

Matches captured pane output against response rules and sends
automatic replies (or escalates to the governor).
"""

from __future__ import annotations

import os
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResponseRule:
    pattern: str  # regex
    response: str  # text to send (ignored for escalate/signal actions)
    action: str  # 'send' | 'signal_done' | 'signal_failed' | 'escalate'

    def __post_init__(self) -> None:
        valid = ("send", "signal_done", "signal_failed", "escalate")
        if self.action not in valid:
            raise ValueError(f"Invalid action {self.action!r}. Must be one of {valid}")


BUILT_IN_RULES: list[ResponseRule] = [
    # Auth / permission — escalate, never auto-respond
    ResponseRule(r"(?i)enter password", "", "escalate"),
    ResponseRule(r"(?i)enter passphrase", "", "escalate"),
    ResponseRule(r"(?i)permission denied", "", "escalate"),
    # Common yes/no prompts — auto-respond
    ResponseRule(r"(?i)do you want to proceed", "yes", "send"),
    ResponseRule(r"(?i)proceed\?", "yes", "send"),
    ResponseRule(r"\[yes/no\]", "yes", "send"),
    ResponseRule(r"(?i)are you sure", "yes", "send"),
    ResponseRule(r"\b[yY]/[nN]\b", "y", "send"),
]

# Cooldown tracking: {(session_root, slug, pattern): last_response_time}
_cooldowns: dict[tuple[str, str, str], float] = {}

COOLDOWN_SECONDS = 30


def load_response_rules(session_root: str) -> list[ResponseRule]:
    """Load response rules: user config merged with built-ins.

    User rules from .dgov/responses.toml override built-in rules
    that share the same pattern.
    """
    config_path = Path(session_root) / ".dgov" / "responses.toml"
    if not config_path.is_file():
        return list(BUILT_IN_RULES)

    with open(config_path, "rb") as f:
        data = tomllib.load(f)

    user_rules: list[ResponseRule] = []
    for entry in data.get("rules", {}).get("rule", []):
        pattern = entry.get("pattern", "")
        if not pattern:
            continue
        user_rules.append(
            ResponseRule(
                pattern=pattern,
                response=entry.get("response", ""),
                action=entry.get("action", "send"),
            )
        )

    # User rules override built-ins with the same pattern
    user_patterns = {r.pattern for r in user_rules}
    merged = list(user_rules)
    for rule in BUILT_IN_RULES:
        if rule.pattern not in user_patterns:
            merged.append(rule)
    return merged


def match_response(output: str, rules: list[ResponseRule]) -> ResponseRule | None:
    """Check last 10 lines of output against rules. Return first match or None."""
    if not output:
        return None
    lines = output.strip().splitlines()[-10:]
    tail = "\n".join(lines)
    for rule in rules:
        if re.search(rule.pattern, tail):
            return rule
    return None


def _cooldown_key(session_root: str, slug: str, pattern: str) -> tuple[str, str, str]:
    return (os.path.abspath(session_root), slug, pattern)


def check_cooldown(session_root: str, slug: str, pattern: str) -> bool:
    """Return True if we should skip (still in cooldown)."""
    key = _cooldown_key(session_root, slug, pattern)
    last = _cooldowns.get(key)
    if last is None:
        return False
    return (time.monotonic() - last) < COOLDOWN_SECONDS


def record_cooldown(session_root: str, slug: str, pattern: str) -> None:
    """Record that we just responded to this pattern for this slug."""
    _cooldowns[_cooldown_key(session_root, slug, pattern)] = time.monotonic()


def reset_cooldowns() -> None:
    """Clear all cooldown state (useful for testing)."""
    _cooldowns.clear()


def auto_respond(
    session_root: str,
    slug: str,
    output: str,
    rules: list[ResponseRule] | None = None,
) -> ResponseRule | None:
    """Check output for a matching rule and execute the response.

    Returns the matched rule if an action was taken, None otherwise.
    Does NOT send for 'escalate' actions — the caller should handle those.
    """
    from dgov.backend import get_backend
    from dgov.persistence import STATE_DIR, emit_event, get_pane

    if rules is None:
        rules = load_response_rules(session_root)

    rule = match_response(output, rules)
    if rule is None:
        return None

    # Cooldown check
    if check_cooldown(session_root, slug, rule.pattern):
        return None

    target = get_pane(session_root, slug)
    if not target:
        return None
    pane_id = target.get("pane_id", "")

    if rule.action == "escalate":
        emit_event(session_root, "pane_blocked", slug, question=rule.pattern)
        record_cooldown(session_root, slug, rule.pattern)
        return rule

    if rule.action == "send":
        if pane_id and get_backend().is_alive(pane_id):
            get_backend().send_input(pane_id, rule.response)
            emit_event(
                session_root,
                "pane_auto_responded",
                slug,
                pattern=rule.pattern,
                response=rule.response,
            )
            record_cooldown(session_root, slug, rule.pattern)
            return rule

    if rule.action in ("signal_done", "signal_failed"):
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        if rule.action == "signal_done":
            (done_dir / slug).touch()
        else:
            (done_dir / f"{slug}.exit").write_text("auto_respond")
        emit_event(
            session_root,
            "pane_auto_responded",
            slug,
            pattern=rule.pattern,
            action=rule.action,
        )
        record_cooldown(session_root, slug, rule.pattern)
        return rule

    return None
