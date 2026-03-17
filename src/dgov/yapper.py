"""Yapper — conversational front-end for dgov."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
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


@dataclass
class YapperSession:
    """Holds conversation context across a REPL session."""

    history: list[tuple[str, YapperResult]] = field(default_factory=list)
    max_context: int = 8

    def record(self, text: str, result: YapperResult) -> None:
        self.history.append((text, result))
        if len(self.history) > self.max_context:
            self.history = self.history[-self.max_context :]

    def format_context(self) -> str:
        if not self.history:
            return ""
        lines = []
        for text, result in self.history[-5:]:
            if result.category == "COMMAND" and result.slug:
                files = (result.raw_classification or {}).get("files", [])
                file_str = f" files={files}" if files else ""
                lines.append(f"- [{result.agent}] '{result.slug}': {result.summary}{file_str}")
            elif result.category == "IDEA":
                lines.append(f"- idea: {result.summary}")
            else:
                lines.append(f"- user said: {text[:80]}")
        return "\n".join(lines)


_VALID_CATEGORIES = {"COMMAND", "IDEA", "QUESTION", "CHATTER"}
_SAFE_FILE_RE = re.compile(r"^(?![/])(?!.*\.\./)[a-zA-Z0-9_./-]+$")

_CLASSIFY_SYSTEM = (
    "You are a secretary for a software governor. "
    "Classify user input and extract structured task info.\n"
    "\n"
    "Categories:\n"
    "- COMMAND: user wants work done (fix, add, refactor, test, "
    "deploy, review, rename)\n"
    "- IDEA: brainstorming or deferring (what if, maybe later, "
    "idea:, we could)\n"
    "- QUESTION: wants info about state, code, or process "
    "(status, which file, how does)\n"
    "- CHATTER: greeting, thanks, acknowledgment, thinking aloud\n"
    "\n"
    "Extract:\n"
    "- agent_hint: if user names an agent (claude, codex, gemini, "
    "pi), else null\n"
    "- files: file paths mentioned (src/..., tests/...), else []\n"
    "- urgency: now (default) or later\n"
    "- summary: one-line imperative task description, strip filler. "
    "Resolve pronouns (that, it, same thing) using recent context.\n"
    "\n"
    "Reply as JSON only:\n"
    '{"category": "COMMAND", "agent_hint": null, "files": [], '
    '"urgency": "now", "summary": "..."}'
)


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


def classify(
    text: str,
    agent_registry: dict | None = None,
    session: YapperSession | None = None,
) -> dict:
    """Classify user input via LLM. Returns validated JSON dict."""
    from dgov.openrouter import chat_completion_local_first as chat_completion

    context = session.format_context() if session else ""
    user_content = ""
    if context:
        user_content += f"Recent context:\n{context}\n\n"
    user_content += f"---\n{text[:500]}"

    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": user_content},
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


def _queue_dispatch(session_root: str, entry: dict) -> int:
    """Append to dispatch queue. Returns queue depth."""
    import os as _os

    queue_path = Path(session_root) / ".dgov" / "dispatch_queue.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    row = json.dumps({**entry, "ts": time.time()}) + "\n"
    fd = _os.open(
        str(queue_path),
        _os.O_WRONLY | _os.O_CREAT | _os.O_APPEND,
        0o644,
    )
    try:
        _os.write(fd, row.encode())
    finally:
        _os.close(fd)
    return sum(1 for _ in queue_path.open())


def read_dispatch_queue(session_root: str) -> list[dict]:
    """Read all queued dispatches."""
    queue_path = Path(session_root) / ".dgov" / "dispatch_queue.jsonl"
    if not queue_path.is_file():
        return []
    items = []
    for line in queue_path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items


def clear_dispatch_queue(session_root: str) -> int:
    """Clear the dispatch queue. Returns number of items cleared."""
    queue_path = Path(session_root) / ".dgov" / "dispatch_queue.jsonl"
    if not queue_path.is_file():
        return 0
    count = sum(1 for _ in queue_path.open())
    queue_path.unlink()
    return count


def _handle_command(
    text: str,
    classification: dict,
    project_root: str,
    session_root: str,
    permission_mode: str = "bypassPermissions",
) -> YapperResult:
    """Dispatch an LT-GOV for a COMMAND, or queue if deferred."""
    from dgov.lifecycle import create_worker_pane
    from dgov.persistence import emit_event
    from dgov.strategy import _generate_slug

    summary = classification.get("summary", text[:100])
    agent_hint = classification.get("agent_hint")
    urgency = classification.get("urgency", "now")
    files = classification.get("files", [])

    # Deferred commands go to the queue
    if urgency == "later":
        depth = _queue_dispatch(
            session_root,
            {
                "summary": summary,
                "agent_hint": agent_hint,
                "files": files,
                "text": text[:500],
            },
        )
        emit_event(
            session_root,
            "yap_received",
            "",
            category="COMMAND",
            summary=summary,
            urgency="later",
        )
        return YapperResult(
            category="COMMAND",
            action="queued",
            summary=summary,
            reply=f"Queued ({depth} in queue): {summary}",
            raw_classification=classification,
        )

    # Immediate commands → dispatch LT-GOV
    slug = _generate_slug(summary)

    try:
        pane = create_worker_pane(
            project_root=project_root,
            prompt=summary,
            agent="claude",
            permission_mode=permission_mode,
            session_root=session_root,
            slug=slug,
            role="lt-gov",
            env_vars={
                "DGOV_SKIP_GOVERNOR_CHECK": "1",
                "DGOV_PROJECT_ROOT": project_root,
            },
        )
    except Exception as exc:
        logger.error("LT-GOV dispatch failed: %s", exc)
        return YapperResult(
            category="COMMAND",
            action="error",
            summary=summary,
            reply=f"Dispatch failed: {exc}",
            raw_classification=classification,
        )

    emit_event(
        session_root,
        "yap_received",
        pane.slug,
        category="COMMAND",
        summary=summary,
        agent="claude",
        role="lt-gov",
    )
    return YapperResult(
        category="COMMAND",
        action="dispatched",
        summary=summary,
        slug=pane.slug,
        agent="claude",
        reply=f"LT-GOV '{pane.slug}': {summary}",
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
    session: YapperSession | None = None,
) -> YapperResult:
    """Main entry point: classify input and route to handler."""
    session_root = session_root or project_root

    from dgov.agents import load_registry

    registry = load_registry(project_root)
    classification = classify(text, agent_registry=registry, session=session)
    category = classification.get("category", "CHATTER").upper()

    # Notify on fallback classification
    if classification.get("_fallback"):
        result = _handle_chatter(text, classification)
        result.reply = f"(classification failed) {result.reply}"
        if session:
            session.record(text, result)
        return result

    if category == "COMMAND":
        result = _handle_command(text, classification, project_root, session_root, permission_mode)
    elif category == "IDEA":
        result = _handle_idea(text, classification, session_root)
    elif category == "QUESTION":
        result = _handle_question(text, classification, project_root, session_root)
    else:
        result = _handle_chatter(text, classification)

    if session:
        session.record(text, result)
    return result
