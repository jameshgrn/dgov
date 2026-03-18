# PI-AUDIT: Stale "pi" Agent References

**Date:** 2026-03-18
**Branch:** audit-pi-refs
**Scope:** `src/dgov/**/*.py`

## Background

`pi` is the CLI harness (the binary you call), not an agent. Actual agents are
`river-35b`, `qwen35-35b`, etc. References that treat `"pi"` as an agent ID or
agent tier name are stale and should use a real agent ID.

**Excluded from findings:**
- `prompt_command="pi"` — correct, refers to the CLI binary
- `agent_def.prompt_command == "pi"` — correct, checks CLI binary
- `"pi"` in process-detection sets (`done.py`, `status.py`) — correct, matches the binary name in `ps`

---

## Findings

### 1. `src/dgov/agents.py:174-187` — Agent definition with `id="pi"`

```python
"pi": AgentDef(
    id="pi",
    name="Qwen 35B (River)",
    ...
)
```

**Issue:** The registered agent ID is `"pi"` (the harness name) rather than the
model it runs (`"river-35b"` or `"qwen35"`). `name="Qwen 35B (River)"` already
says what it is — the ID should match.
**Should be:** `id="river-35b"` (or `"qwen35"`), dict key updated to match.

---

### 2. `src/dgov/agents.py:480` — `_DEFAULT_AGENT_CHAIN`

```python
_DEFAULT_AGENT_CHAIN = ("pi", "claude", "codex", "gemini")
```

**Issue:** `"pi"` is the first-tier default agent ID in the fallback chain.
When the agent ID is renamed this will silently fall through to `"claude"`.
**Should be:** `("river-35b", "claude", "codex", "gemini")` (or whatever ID is chosen).

---

### 3. `src/dgov/lifecycle.py:262-264` — `agent_id == "pi"` for prompt structuring

```python
# 5. Auto-structure pi prompts
if agent_id == "pi" and not skip_auto_structure:
    prompt = _structure_pi_prompt(prompt)
```

**Issue:** Prompt auto-structuring is a feature of the `pi` CLI format, not
specific to the `"pi"` agent ID. Agents `pi-claude`, `pi-codex`, `pi-gemini`,
and `pi-openrouter` all use `prompt_command="pi"` but are excluded by this check.
**Should be:** `if agent_def.prompt_command == "pi" and not skip_auto_structure:`

---

### 4. `src/dgov/strategy.py:21` — `classify_task` default agent list

```python
agents = installed_agents or ["pi", "claude"]
```

**Issue:** `"pi"` used as a default agent ID when no agents are installed.
**Should be:** real agent ID (e.g. `["river-35b", "claude"]`).

---

### 5. `src/dgov/strategy.py:40-48` — LLM classification prompt uses `"pi"` as tier label

```python
system_msg = (
    "Classify this task as either 'pi' or 'claude'.\n"
    "pi = mechanical: run a command, edit a specific line, ..."
    ...
    "Reply with ONLY 'pi' or 'claude', nothing else."
)
```

**Issue:** The LLM is instructed to return `"pi"` as a category name. The
returned string is then matched directly against agent IDs. Using the harness
name as a tier label conflates the CLI tool with the agent tier.
**Should be:** use the actual agent ID in the prompt (e.g. `'river-35b'`), or
keep `"pi"` as a stable routing token and document it explicitly as a routing
alias rather than an agent ID.

---

### 6. `src/dgov/templates.py:33,56,68` — `default_agent="pi"` in built-in templates

```python
# bug-fix template (line 33)
default_agent="pi",

# refactor template (line 56)
default_agent="pi",

# write-tests template (line 68)
default_agent="pi",
```

**Issue:** All three built-in task templates hardcode `"pi"` as the default agent.
**Should be:** real agent ID (e.g. `"river-35b"`).

---

### 7. `src/dgov/yapper.py:240` — `"default_agent": "pi"` in yapper dispatch

```python
"default_agent": "pi",
```

**Issue:** When yapper dispatches tasks it hardcodes `"pi"` as the agent.
**Should be:** real agent ID.

---

### 8. `src/dgov/cli/templates.py:75` — Scaffolded TOML example uses `"pi"`

```python
'default_agent = "pi"\n'
```

**Issue:** The scaffolded template file that `dgov template scaffold` generates
teaches users to write `default_agent = "pi"`.
**Should be:** real agent ID (e.g. `"river-35b"`).

---

### 9. `src/dgov/cli/pane.py:263` — Docstring TOML example uses `agent = "pi"`

```
[tasks.fix-parser]
agent = "pi"
prompt = "Fix the parser bug in..."
```

**Issue:** User-facing help text shows `"pi"` as the agent value.
**Should be:** real agent ID in the example.

---

### 10. `src/dgov/cli/pane.py:538,592` — `suggest_escalate` only fires for `agent == "pi"`

```python
# line 538 (pane wait)
if exc.agent == "pi":
    timeout_result["suggest_escalate"] = True

# line 592 (pane wait-all)
if p["agent"] == "pi":
    timeout_result["suggest_escalate"] = True
```

**Issue:** Escalation suggestion is tied to the exact string `"pi"`. Panes
running `pi-claude`, `pi-codex`, `pi-gemini`, or `pi-openrouter` (all of which
also use the pi harness) never get `suggest_escalate`.
**Should be:** check `agent_def.prompt_command == "pi"` or enumerate all pi-based
agent IDs.

---

### 11. `src/dgov/recovery.py:37` — Escalation chain keyed by `"pi"`

```python
ESCALATION_CHAIN: dict[str, str] = {
    "pi": "claude",
    ...
}
```

**Issue:** Only `"pi"` maps to an escalation target. `pi-claude`, `pi-codex`,
`pi-gemini`, and `pi-openrouter` are absent, so `escalate_worker_pane` falls
back to the default `target_agent="claude"` silently instead of using the chain.
**Should be:** agent ID updated to match renamed ID; consider adding entries for
`pi-*` variants or keying by `prompt_command`.

---

### 12. `src/dgov/tmux.py:291` — Color map keyed by `"pi"`

```python
_AGENT_COLORS: dict[str, int] = {
    "claude": 39,
    "pi": 34,   # green
    ...
}
```

**Issue:** Color lookup uses agent ID `"pi"`. After a rename the color will
silently fall back to the default. `pi-*` agents also get no color.
**Should be:** updated to match renamed agent ID; `pi-*` agents likely need
entries or a prefix-lookup fallback.

---

### 13. `src/dgov/terrain.py:456-457` — Pi avatar keyed by `"pi"`

```python
# Pi -- green, flower on head, nature spirit
"pi": [
    ...
]
```

**Issue:** Terrain avatar sprite is keyed by agent ID `"pi"`. After a rename
the avatar lookup will fail silently and the pane will render without a character.
**Should be:** dict key updated to match renamed agent ID.

---

## Summary Table

| # | File | Lines | Kind | Severity |
|---|------|-------|------|----------|
| 1 | `agents.py` | 174-187 | Agent ID definition `id="pi"` | High — root cause |
| 2 | `agents.py` | 480 | `_DEFAULT_AGENT_CHAIN` first entry | High |
| 3 | `lifecycle.py` | 262-264 | `agent_id == "pi"` gate for prompt structuring | High — excludes pi-* |
| 10 | `cli/pane.py` | 538, 592 | `suggest_escalate` only for `"pi"` | High — excludes pi-* |
| 11 | `recovery.py` | 37 | Escalation chain entry | Medium |
| 4 | `strategy.py` | 21 | Default agent list | Medium |
| 5 | `strategy.py` | 40-48 | LLM classification prompt label | Medium |
| 6 | `templates.py` | 33, 56, 68 | Template `default_agent` | Medium |
| 7 | `yapper.py` | 240 | Yapper `default_agent` | Medium |
| 12 | `tmux.py` | 291 | Agent color map key | Low — cosmetic |
| 13 | `terrain.py` | 456-457 | Avatar sprite dict key | Low — cosmetic |
| 8 | `cli/templates.py` | 75 | Scaffolded TOML example | Low — docs |
| 9 | `cli/pane.py` | 263 | Docstring TOML example | Low — docs |

## Recommended Fix Order

1. Rename `id="pi"` in `agents.py` to `"river-35b"` (or `"qwen35"`) and update
   the dict key. This is the root change everything else follows from.
2. Update `_DEFAULT_AGENT_CHAIN`, `ESCALATION_CHAIN`, `_AGENT_COLORS`, terrain
   avatar, and all `default_agent="pi"` references to use the new ID.
3. Fix `lifecycle.py:263` to check `prompt_command` instead of `agent_id`.
4. Fix `cli/pane.py:538,592` to cover all pi-harness agents.
5. Update docstrings and scaffolded examples last (lowest risk).
