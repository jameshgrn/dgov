# Yapper — Conversational Front-End for dgov

## Problem

The governor today requires exact CLI commands. The user thinks in intentions
("fix that flaky test", "idea: what if workers could negotiate"), but must
translate into `dgov pane create -a claude -p "..." -r .`. Yapper bridges
natural language → dgov actions.

## What Yapper Is

A **lightweight classifier + router** that:
1. Accepts freeform text from the user
2. Classifies intent (command, idea, question, chatter)
3. Routes to the right handler (governor dispatch, idea board, help, ack)
4. Responds conversationally with confirmation or result

Yapper is NOT a full agent. It doesn't write code, debug, or make architectural
decisions. It's a thin shim between human speech and dgov's existing CLI.

## Architecture

```
User input (text)
    │
    ▼
┌──────────┐
│  Yapper  │  ← classify + extract + route
└────┬─────┘
     │
     ├──► COMMAND  → build dgov CLI call → dispatch worker → ack
     ├──► IDEA     → append to .dgov/ideas.jsonl → ack
     ├──► QUESTION → answer from context (codebase, pane state, docs)
     └──► CHATTER  → conversational ack (no side effects)
```

## Classification

Single LLM call via existing `openrouter.chat_completion()`. System prompt:

```
Classify the user input below the --- separator into exactly one category:
- COMMAND: user wants work done (fix, add, refactor, test, deploy, merge)
- IDEA: user is brainstorming, speculating, or deferring ("what if", "maybe later", "idea:")
- QUESTION: user wants info about state, code, or process ("what's the status", "which file")
- CHATTER: greeting, thanks, acknowledgment, thinking out loud

Also extract:
- agent_hint: if user names an agent (claude, codex, gemini, pi), else null
- files: any file paths mentioned, else []
- urgency: "now" or "later"
- summary: one-line imperative task description (strip conversational filler like "please", "could you")

Reply as JSON only: {"category": "...", "agent_hint": null, "files": [], "urgency": "now", "summary": "..."}
```

User text is passed after a `---` delimiter to reduce prompt injection surface.

Model: OpenRouter (hunter-alpha or whatever's free) → Qwen 4B fallback.
Max tokens: 150. Temperature: 0.

### Post-classification validation (P0)

1. **category** must be in `{"COMMAND", "IDEA", "QUESTION", "CHATTER"}` — unknown → CHATTER
2. **agent_hint** must be in agent registry or null — unknown → null
3. **files** validated against `^[a-zA-Z0-9_./-]+$` regex — invalid entries stripped
4. **Fail-closed default**: on classifier failure (JSON parse error, LLM outage),
   default to CHATTER (no side effects), NOT COMMAND. Notify user: "Classification
   failed; treating as acknowledgment."

## Handlers

### COMMAND handler
1. Extract summary from classification
2. Route to `classify_task()` (strategy.py) for agent selection (unless agent_hint provided)
3. Do NOT call `_structure_pi_prompt()` — lifecycle.py auto-structures pi prompts
   (pass raw summary, let lifecycle handle it to avoid double-wrapping)
4. Call `create_worker_pane()` (lifecycle.py) directly — no subprocess
5. Emit `yap_received` event with category, summary, agent
6. Return `{"action": "dispatched", "slug": slug, "agent": agent, "summary": summary}`
7. On `create_worker_pane` failure: catch exception, return error YapperResult

### IDEA handler
1. Append to `.dgov/ideas.jsonl` with timestamp and raw text (O_APPEND for safety)
2. Emit `yap_received` event with category=IDEA
3. Return `{"action": "noted", "idea": summary}`

### QUESTION handler
1. For state questions ("what panes are running"): call `list_worker_panes()` and format
2. For code questions: defer to governor (out of scope for v1)
3. Return `{"action": "answered", "answer": "..."}`

### CHATTER handler
1. Return `{"action": "ack", "reply": "conversational response"}`

## Module: `src/dgov/yapper.py`

```python
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
    category: str          # COMMAND | IDEA | QUESTION | CHATTER
    action: str            # dispatched | noted | answered | ack
    summary: str
    slug: str | None = None
    agent: str | None = None
    reply: str | None = None
    raw_classification: dict | None = None


_VALID_CATEGORIES = {"COMMAND", "IDEA", "QUESTION", "CHATTER"}
_SAFE_FILE_RE = re.compile(r"^[a-zA-Z0-9_./-]+$")

_CLASSIFY_SYSTEM = """Classify the user input below the --- separator into exactly one category:
- COMMAND: user wants work done (fix, add, refactor, test, deploy, review, merge, rename)
- IDEA: brainstorming or deferring ("what if", "maybe later", "idea:", "we could")
- QUESTION: wants info about state, code, or process ("status", "which file", "how does")
- CHATTER: greeting, thanks, acknowledgment, thinking out loud

Extract from the input:
- agent_hint: if user names a specific agent (claude, codex, gemini, pi), else null
- files: file paths mentioned (src/..., tests/..., *.py), else []
- urgency: "now" (default) or "later" (if user says "eventually", "low priority", "when you get to it")
- summary: one-line imperative task description, strip filler ("please", "could you")

Reply as JSON only: {"category": "COMMAND", "agent_hint": null, "files": [], "urgency": "now", "summary": "..."}"""


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
    from dgov.openrouter import chat_completion

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

    emit_event(session_root, "yap_received", pane.slug,
               category="COMMAND", summary=summary, agent=agent)
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

    emit_event(session_root, "yap_received", "",
               category="IDEA", summary=summary)
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
        reply=f"I can answer status questions. For code questions, ask the governor directly.",
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
```

## CLI: `dgov yap`

```python
# In src/dgov/cli/yap_cmd.py

@click.command("yap")
@click.argument("text", nargs=-1, required=True)
@click.option("--project-root", "-r", default=".", envvar="DGOV_PROJECT_ROOT")
@SESSION_ROOT_OPTION
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON")
def yap_cmd(text, project_root, session_root, as_json):
    """Talk to dgov in natural language."""
    from dgov.cli import _check_governor_context
    from dgov.yapper import yap

    _check_governor_context()
    full_text = " ".join(text)
    result = yap(full_text, os.path.abspath(project_root), session_root)
    if as_json:
        click.echo(json.dumps(asdict(result), default=str))
    else:
        click.echo(result.reply)
```

Register in `cli/__init__.py`:
```python
from dgov.cli.yap_cmd import yap_cmd
cli.add_command(yap_cmd)
```

## Usage Examples

```bash
# Command — dispatches a worker
dgov yap fix the flaky test in test_merger.py
# → "Dispatched pi worker 'fix-flaky-test-merger': fix the flaky test in test_merger.py"

# Idea — saves for later
dgov yap "idea: workers should be able to request help from siblings"
# → "Noted: workers should be able to request help from siblings"

# Question — answers from state
dgov yap "what's running right now?"
# → "2 pane(s):\n  fix-parser (claude) — active\n  add-tests (pi) — done"

# Chatter — acknowledges
dgov yap thanks
# → "You got it."

# Agent hint — respects user's choice
dgov yap "have claude debug why the merger drops commits"
# → "Dispatched claude worker 'debug-merger-drops-commits': debug why the merger drops commits"
```

## Interactive Mode (v2)

Future: `dgov yap --interactive` opens a REPL that:
- Shows a prompt (`yap> `)
- Classifies + routes each line
- Maintains conversation context (last 5 exchanges)
- Shows worker status updates inline

Not in v1. v1 is single-shot CLI only.

## What Yapper Does NOT Do

- Write or edit code
- Make architectural decisions
- Run tests or lint
- Access the filesystem directly
- Replace the governor — it feeds the governor

## Dependencies

- Zero new deps. Uses existing `openrouter.chat_completion()`, `strategy.classify_task()`,
  `lifecycle.create_worker_pane()`, `status.list_worker_panes()`
- Model: same OpenRouter fallback chain (hunter-alpha → Qwen 4B)

## Testing Strategy

- Unit test `classify()` with mocked LLM responses
- Unit test `_validate_classification()` with edge cases (unknown category, bad agent, unsafe files)
- Unit test each handler independently
- Integration test `yap()` end-to-end with mocked backend
- Test classification fallback (malformed JSON → defaults to CHATTER, not COMMAND)
- Test `_fallback` notification appears in reply
- Test `create_worker_pane` failure → error result (not traceback)
- Test `yap_received` events emitted for COMMAND and IDEA categories

## Review Fixes Applied

Codex (adversarial) + Gemini (architectural) reviewed this design. Fixes:

| Issue | Source | Fix |
|-------|--------|-----|
| Fail-open default on classifier failure | Codex #2 | Fail closed → CHATTER, notify user |
| Raw LLM output trusted as control data | Codex #1 | `_validate_classification()` — allowlist category, agent, file paths |
| Double pi prompt structuring | Codex #4 | Removed `_structure_pi_prompt` from yapper; lifecycle handles it |
| ideas.jsonl concurrent append race | Codex #5 | `O_APPEND` flag on file descriptor |
| No error path on dispatch failure | Codex #7 | try/except around `create_worker_pane`, return error result |
| Prompt injection surface | Codex #1 | `---` delimiter between system prompt and user text |
| Missing event emission | Gemini #3 | `yap_received` event for COMMAND and IDEA |
| Missing governor context check | Gemini #4 | `_check_governor_context()` in CLI command |
| Conversational filler in summary | Gemini #6 | System prompt instructs to strip filler |

Deferred to v2:
- Slug collision defense (transactional reserve) — Codex #6
- Shell injection hardening in pi prompts — Codex #3
- `dgov ideas` list command — Gemini #5
- Handler plugin registry — Gemini #4

## Open Questions

1. Should `yap` auto-wait on dispatched workers, or just fire-and-forget?
   → v1: fire-and-forget. User polls with `dgov pane list` or `dgov yap "what's running"`.

2. Should IDEA entries be searchable via `dgov yap "show ideas"`?
   → v1: no. Just `cat .dgov/ideas.jsonl`. v2: add `dgov ideas list`.

3. Should Yapper maintain conversation history across invocations?
   → v1: no. Each `dgov yap` is stateless. v2: optional `--context` flag.

4. Should Yapper be able to chain commands ("fix X then run tests")?
   → No. That's a mission/DAG. Yapper dispatches single workers.
