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
Classify user input into exactly one category:
- COMMAND: user wants work done (fix, add, refactor, test, deploy, merge)
- IDEA: user is brainstorming, speculating, or deferring ("what if", "maybe later", "idea:")
- QUESTION: user wants info about state, code, or process ("what's the status", "which file")
- CHATTER: greeting, thanks, acknowledgment, thinking out loud

Also extract:
- agent_hint: if user names an agent ("have claude do it"), extract it
- files: any file paths mentioned
- urgency: "now" or "later"

Reply as JSON: {"category": "COMMAND", "agent_hint": null, "files": [], "urgency": "now", "summary": "one-line task description"}
```

Model: OpenRouter (hunter-alpha or whatever's free) → Qwen 4B fallback.
Max tokens: 100. Temperature: 0.

## Handlers

### COMMAND handler
1. Extract summary from classification
2. Route to `classify_task()` (strategy.py) for agent selection (unless agent_hint provided)
3. If agent is pi, run `_structure_pi_prompt()` to format the prompt
4. Call `create_worker_pane()` (lifecycle.py) directly — no subprocess
5. Return `{"action": "dispatched", "slug": slug, "agent": agent, "summary": summary}`

### IDEA handler
1. Append to `.dgov/ideas.jsonl` with timestamp and raw text
2. Return `{"action": "noted", "idea": summary}`

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


_CLASSIFY_SYSTEM = """Classify user input into exactly one category:
- COMMAND: user wants work done (fix, add, refactor, test, deploy, review, merge, rename)
- IDEA: brainstorming or deferring ("what if", "maybe later", "idea:", "we could")
- QUESTION: wants info about state, code, or process ("status", "which file", "how does")
- CHATTER: greeting, thanks, acknowledgment, thinking out loud

Extract from the input:
- agent_hint: if user names a specific agent (claude, codex, gemini, pi), else null
- files: file paths mentioned (src/..., tests/..., *.py), else []
- urgency: "now" (default) or "later" (if user says "eventually", "low priority", "when you get to it")
- summary: one-line imperative task description (for COMMAND) or short note (for others)

Reply as JSON only: {"category": "COMMAND", "agent_hint": null, "files": [], "urgency": "now", "summary": "..."}"""


def classify(text: str) -> dict:
    """Classify user input via LLM. Returns parsed JSON dict."""
    from dgov.openrouter import chat_completion

    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": text[:500]},
    ]
    try:
        resp = chat_completion(messages, max_tokens=150, temperature=0)
        content = resp["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(content)
    except (json.JSONDecodeError, KeyError, RuntimeError) as exc:
        logger.warning("Classification failed: %s", exc)
        # Fallback: treat as command
        return {
            "category": "COMMAND",
            "agent_hint": None,
            "files": [],
            "urgency": "now",
            "summary": text[:100],
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
    from dgov.strategy import _structure_pi_prompt, classify_task

    summary = classification.get("summary", text[:100])
    files = classification.get("files", [])
    agent_hint = classification.get("agent_hint")

    registry = load_registry(project_root)
    agent = agent_hint or classify_task(summary, list(registry.keys()))

    # Structure prompt for pi-class agents
    prompt = summary
    if agent == "pi":
        prompt = _structure_pi_prompt(summary, files or None)

    pane = create_worker_pane(
        project_root=project_root,
        prompt=prompt,
        agent=agent,
        permission_mode=permission_mode,
        session_root=session_root,
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
    ideas_path = Path(session_root) / ".dgov" / "ideas.jsonl"
    ideas_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "text": text,
        "summary": classification.get("summary", text[:100]),
    }
    with open(ideas_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    summary = classification.get("summary", text[:100])
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
    classification = classify(text)
    category = classification.get("category", "COMMAND").upper()

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
    from dgov.yapper import yap
    full_text = " ".join(text)
    result = yap(full_text, project_root, session_root)
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
- Unit test each handler independently
- Integration test `yap()` end-to-end with mocked backend
- Test classification fallback (malformed JSON → defaults to COMMAND)

## Open Questions

1. Should `yap` auto-wait on dispatched workers, or just fire-and-forget?
   → v1: fire-and-forget. User polls with `dgov pane list` or `dgov yap "what's running"`.

2. Should IDEA entries be searchable via `dgov yap "show ideas"`?
   → v1: no. Just `cat .dgov/ideas.jsonl`. v2: add `dgov ideas list`.

3. Should Yapper maintain conversation history across invocations?
   → v1: no. Each `dgov yap` is stateless. v2: optional `--context` flag.

4. Should Yapper be able to chain commands ("fix X then run tests")?
   → No. That's a mission/DAG. Yapper dispatches single workers.
