"""Yapper — conversational front-end for dgov."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class YapperResult:
    category: str  # COMMAND | IDEA | QUESTION | CHATTER
    action: str  # dispatched | noted | answered | ack
    summary: str
    slug: str | None = None
    agent: str | None = None
    reply: str | None = None
    raw_classification: dict | None = None


_VALID_CATEGORIES = {"COMMAND", "IDEA", "QUESTION", "CHATTER"}
_SAFE_FILE_RE = re.compile(r"^(?![/])(?!.*\.\./)[a-zA-Z0-9_./-]+$")

_CLASSIFY_SYSTEM = """Classify the user input below the --- separator into exactly one category:
- COMMAND: user wants work done (fix, add, refactor, test, deploy, review, merge, rename)
- IDEA: brainstorming or deferring ("what if", "maybe later", "idea:", "we could")
- QUESTION: wants info about state, code, or process ("status", "which file", "how does")
- CHATTER: greeting, thanks, acknowledgment, thinking out loud

Extract from the input:
- agent_hint: if user names a specific agent (claude, codex, gemini, pi), else null
- files: file paths mentioned (src/..., tests/..., *.py), else []
- urgency: "now" (default) or "later" ("eventually", "low priority")
- summary: one-line imperative task description, strip filler

Reply as JSON only:
{"category": "COMMAND", "agent_hint": null, "files": [], "urgency": "now", "summary": "..."}"""


def _validate_classification(raw: dict, agent_registry: dict | None = None) -> dict:
    """Sanitize classifier output against allowlists."""
    cat = str(raw.get("category", "")).upper()
    if cat not in _VALID_CATEGORIES:
        cat = "CHATTER"
    agent_hint = raw.get("agent_hint")
    if agent_hint and agent_registry and agent_hint not in agent_registry:
        agent_hint = None
    files = [f for f in raw.get("files", []) if isinstance(f, str) and _SAFE_FILE_RE.match(f)]
    return {
        "category": cat,
        "agent_hint": agent_hint,
        "files": files,
        "urgency": raw.get("urgency", "now"),
        "summary": str(raw.get("summary", ""))[:200],
    }


def classify(text: str, agent_registry: dict | None = None) -> dict:
    """Classify user input via LLM. Returns validated JSON dict."""
    from dgov.openrouter import chat_completion_local_first as chat_completion

    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": f"---\n{text[:500]}"},
    ]
    try:
        resp = chat_completion(messages, max_tokens=150, temperature=0)
        content = resp["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        raw = json.loads(content)
        return _validate_classification(raw, agent_registry)
    except (json.JSONDecodeError, KeyError, RuntimeError) as exc:
        logger.warning("Classification failed: %s", exc)
        # Fail closed: CHATTER (no side effects), not COMMAND
        return {
            "category": "CHATTER",
            "agent_hint": None,
            "files": [],
            "urgency": "now",
            "summary": text[:100],
            "_fallback": True,
        }


def _handle_command(
    text: str,
    classification: dict,
    project_root: str,
    session_root: str,
    permission_mode: str = "bypassPermissions",
) -> YapperResult:
    """Dispatch a worker for a COMMAND."""
    from dgov.agents import load_registry
    from dgov.lifecycle import create_worker_pane
    from dgov.persistence import emit_event
    from dgov.strategy import classify_task

    summary = classification.get("summary", text[:100])
    agent_hint = classification.get("agent_hint")

    registry = load_registry(project_root)
    agent = agent_hint or classify_task(summary, list(registry.keys()))

    # Do NOT call _structure_pi_prompt here — lifecycle.py auto-structures
    # pi prompts (skip_auto_structure defaults to False). Avoids double-wrapping.
    try:
        pane = create_worker_pane(
            project_root=project_root,
            prompt=summary,
            agent=agent,
            permission_mode=permission_mode,
            session_root=session_root,
        )
    except Exception as exc:
        logger.error("Worker dispatch failed: %s", exc)
        return YapperResult(
            category="COMMAND",
            action="error",
            summary=summary,
            reply=f"Dispatch failed: {exc}",
            raw_classification=classification,
        )

    emit_event(
        session_root, "yap_received", pane.slug, category="COMMAND", summary=summary, agent=agent
    )
    return YapperResult(
        category="COMMAND",
        action="dispatched",
        summary=summary,
        slug=pane.slug,
        agent=agent,
        reply=f"Dispatched {agent} worker '{pane.slug}': {summary}",
        raw_classification=classification,
    )


def _handle_idea(
    text: str,
    classification: dict,
    session_root: str,
) -> YapperResult:
    """Append idea to .dgov/ideas.jsonl."""
    import os as _os

    from dgov.persistence import emit_event

    ideas_path = Path(session_root) / ".dgov" / "ideas.jsonl"
    ideas_path.parent.mkdir(parents=True, exist_ok=True)
    summary = classification.get("summary", text[:100])
    entry = {
        "ts": time.time(),
        "text": text,
        "summary": summary,
    }
    # O_APPEND for atomic-ish concurrent writes
    fd = _os.open(str(ideas_path), _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND, 0o644)
    try:
        _os.write(fd, (json.dumps(entry) + "\n").encode())
    finally:
        _os.close(fd)

    emit_event(session_root, "yap_received", "", category="IDEA", summary=summary)
    return YapperResult(
        category="IDEA",
        action="noted",
        summary=summary,
        reply=f"Noted: {summary}",
        raw_classification=classification,
    )


def _handle_question(
    text: str,
    classification: dict,
    project_root: str,
    session_root: str,
) -> YapperResult:
    """Answer state/status questions."""
    from dgov.status import list_worker_panes

    summary = classification.get("summary", text[:100])
    lower = text.lower()

    # Status queries
    if any(w in lower for w in ("status", "panes", "workers", "running", "active")):
        panes = list_worker_panes(project_root, session_root=session_root)
        if not panes:
            answer = "No active panes."
        else:
            lines = []
            for p in panes:
                state = p.get("state", "?")
                agent = p.get("agent", "?")
                slug = p.get("slug", "?")
                lines.append(f"  {slug} ({agent}) — {state}")
            answer = f"{len(panes)} pane(s):\n" + "\n".join(lines)
        return YapperResult(
            category="QUESTION",
            action="answered",
            summary=summary,
            reply=answer,
            raw_classification=classification,
        )

    # Default: can't answer
    return YapperResult(
        category="QUESTION",
        action="answered",
        summary=summary,
        reply="I can answer status questions. For code questions, ask the governor directly.",
        raw_classification=classification,
    )


def _handle_chatter(
    text: str,
    classification: dict,
) -> YapperResult:
    """Conversational ack."""
    summary = classification.get("summary", text[:100])
    # Simple responses
    lower = text.lower().strip()
    if any(w in lower for w in ("thanks", "thank", "ty", "thx")):
        reply = "You got it."
    elif any(w in lower for w in ("hi", "hello", "hey", "yo")):
        reply = "Ready. What do you need?"
    elif any(w in lower for w in ("ok", "cool", "nice", "good", "great")):
        reply = "Standing by."
    else:
        reply = "Copy."
    return YapperResult(
        category="CHATTER",
        action="ack",
        summary=summary,
        reply=reply,
        raw_classification=classification,
    )


def yap(
    text: str,
    project_root: str,
    session_root: str | None = None,
    permission_mode: str = "bypassPermissions",
) -> YapperResult:
    """Main entry point: classify input and route to handler."""
    session_root = session_root or project_root

    from dgov.agents import load_registry

    registry = load_registry(project_root)
    classification = classify(text, agent_registry=registry)
    category = classification.get("category", "CHATTER").upper()

    # Notify on fallback classification
    if classification.get("_fallback"):
        result = _handle_chatter(text, classification)
        result.reply = f"(classification failed) {result.reply}"
        return result

    if category == "COMMAND":
        return _handle_command(text, classification, project_root, session_root, permission_mode)
    elif category == "IDEA":
        return _handle_idea(text, classification, session_root)
    elif category == "QUESTION":
        return _handle_question(text, classification, project_root, session_root)
    else:
        return _handle_chatter(text, classification)
