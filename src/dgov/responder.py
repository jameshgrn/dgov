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
from enum import StrEnum
from pathlib import Path


class ResponseAction(StrEnum):
    SEND = "send"
    SIGNAL_DONE = "signal_done"
    SIGNAL_FAILED = "signal_failed"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class ResponseRule:
    pattern: str  # regex
    response: str  # text to send (ignored for escalate/signal actions)
    action: ResponseAction

    def __post_init__(self) -> None:
        # Coerce str → ResponseAction; raises ValueError for invalid values.
        object.__setattr__(self, "action", ResponseAction(self.action))


BUILT_IN_RULES: list[ResponseRule] = [
    # Auth / permission — escalate, never auto-respond
    ResponseRule(r"(?i)enter password", "", ResponseAction.ESCALATE),
    ResponseRule(r"(?i)enter passphrase", "", ResponseAction.ESCALATE),
    ResponseRule(r"(?i)permission denied", "", ResponseAction.ESCALATE),
    # Common yes/no prompts — auto-respond
    ResponseRule(r"(?i)do you want to proceed", "yes", ResponseAction.SEND),
    ResponseRule(r"(?i)proceed\?", "yes", ResponseAction.SEND),
    ResponseRule(r"\[yes/no\]", "yes", ResponseAction.SEND),
    ResponseRule(r"(?i)are you sure", "yes", ResponseAction.SEND),
    ResponseRule(r"\b[yY]/[nN]\b", "y", ResponseAction.SEND),
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
    Does NOT send for 'send' actions if the pane has dropped to a shell prompt.
    """
    from dgov.backend import get_backend
    from dgov.done import _agent_still_running
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

    if rule.action == ResponseAction.ESCALATE:
        emit_event(session_root, "pane_blocked", slug, question=rule.pattern)
        record_cooldown(session_root, slug, rule.pattern)
        return rule

    if rule.action == ResponseAction.SEND:
        # Only send if pane exists, is alive, and agent is still running
        if pane_id and get_backend().is_alive(pane_id) and _agent_still_running(pane_id):
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

    if rule.action in (ResponseAction.SIGNAL_DONE, ResponseAction.SIGNAL_FAILED):
        done_dir = Path(session_root) / STATE_DIR / "done"
        done_dir.mkdir(parents=True, exist_ok=True)
        if rule.action == ResponseAction.SIGNAL_DONE:
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
