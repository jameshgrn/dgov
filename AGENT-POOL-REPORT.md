# Agent Pool Expansion Report

Date: 2026-03-18

## 1. Current architecture analysis

`dgov` already has the plumbing needed to add these agents without touching Python code.

- `~/.dgov/agents.toml` is the user-global agent layer. In `load_registry()` (`src/dgov/agents.py`), registry load order is: built-ins -> `~/.dgov/agents.toml` -> `<project_root>/.dgov/agents.toml`.
- User-global config can define brand new agents with arbitrary `command`, `transport`, `default_flags`, `permissions`, `env`, `health_check`, and `max_concurrent`.
- Project-local `.dgov/agents.toml` is intentionally restricted. It can only override safe fields on existing agents; it cannot define new agents and cannot override `command`, `default_flags`, `health_check`, `health_fix`, or `env`.
- Existing `hunter` in `~/.dgov/agents.toml` proves the intended pattern already works: it is a `pi` wrapper with `default_flags = "-p --provider openrouter --model openrouter/hunter-alpha"`.

`AgentDef` and `build_launch_command()` are the key launch abstractions.

- `AgentDef` stores the agent command, transport mode, permission flags, retry/concurrency settings, environment, and done-strategy.
- For `pi`-style agents, the only fields that matter here are `command = "pi"`, `transport = "positional"`, and `default_flags` containing `--provider openrouter --model <model_id>`.
- `build_launch_command()` concatenates the base command in this order: `prompt_command` -> `default_flags` -> permission flags -> extra flags.
- For positional transports, it writes the prompt to a temp file, reads it into `DGOV_PROMPT_CONTENT`, deletes the temp file, and finally executes `pi ... "$DGOV_PROMPT_CONTENT"`.
- No OpenRouter-specific logic is needed in `build_launch_command()` because the `pi` CLI harness already accepts `--provider openrouter --model <model_id>`.

`src/dgov/lifecycle.py` resolves the registry entry and launches it, but it does not care which OpenRouter model is behind `pi`.

- `create_worker_pane()` loads the registry, picks the `AgentDef`, enforces `health_check` and `max_concurrent`, and injects agent env vars.
- `_setup_and_launch_agent()` rewrites absolute paths into the worker worktree, wraps the launch command with the done-signal helper, and sends the command to tmux.
- If `prompt_command == "pi"`, lifecycle adds `_pi_extension_flags(project_root)` before launch.
- That means any new `pi`-routed OpenRouter agent inherits the same worktree/path-rewrite/done-signal behavior as the existing `pi` and `hunter` agents.

`src/dgov/openrouter.py` is adjacent, not central, to worker launch.

- `openrouter.py` is a lightweight HTTP client for OpenRouter chat completions, key info, free-model listing, and status.
- Its current runtime job is task classification/fallback (`chat_completion()`, `chat_completion_local_first()`) and status introspection, not worker-pane launch.
- Worker-pane launch is command-driven from `agents.py` + `lifecycle.py`; `openrouter.py` is only involved when `dgov` itself calls the OpenRouter API.
- `_DEFAULT_MODEL` is currently `openrouter/hunter-alpha`; that affects the OpenRouter client default, not the `pi` worker command for explicitly modeled agents.

Conclusion: immediate rollout is a config expansion in `~/.dgov/agents.toml`, not a code change.

## 2. Proposed `agents.toml` entries

These are user-global entries for `~/.dgov/agents.toml`. They follow the same pattern as `hunter`: `pi` CLI, positional transport, explicit OpenRouter provider/model, conservative concurrency caps to limit spend, and the same plan-mode read-only tool restriction as `pi`/`hunter`.

```toml
[agents.qwen35-9b]
name = "Qwen 3.5 9B"
short_label = "q9"
command = "pi"
transport = "positional"
default_flags = "-p --provider openrouter --model qwen/qwen3.5-9b"
color = 36
max_concurrent = 4

[agents.qwen35-9b.permissions]
plan = "--tools read,grep,find,ls"

[agents.qwen35-flash]
name = "Qwen 3.5 Flash"
short_label = "qf"
command = "pi"
transport = "positional"
default_flags = "-p --provider openrouter --model qwen/qwen3.5-flash-02-23"
color = 44
max_concurrent = 4

[agents.qwen35-flash.permissions]
plan = "--tools read,grep,find,ls"

[agents.qwen35-35b]
name = "Qwen 3.5 35B A3B"
short_label = "q35"
command = "pi"
transport = "positional"
default_flags = "-p --provider openrouter --model qwen/qwen3.5-35b-a3b"
color = 70
max_concurrent = 3

[agents.qwen35-35b.permissions]
plan = "--tools read,grep,find,ls"

[agents.qwen35-122b]
name = "Qwen 3.5 122B A10B"
short_label = "q122"
command = "pi"
transport = "positional"
default_flags = "-p --provider openrouter --model qwen/qwen3.5-122b-a10b"
color = 111
max_concurrent = 2

[agents.qwen35-122b.permissions]
plan = "--tools read,grep,find,ls"

[agents.qwen35-397b]
name = "Qwen 3.5 397B A17B"
short_label = "q397"
command = "pi"
transport = "positional"
default_flags = "-p --provider openrouter --model qwen/qwen3.5-397b-a17b"
color = 172
max_concurrent = 1

[agents.qwen35-397b.permissions]
plan = "--tools read,grep,find,ls"

[agents.qwen3-max-thinking]
name = "Qwen 3 Max Thinking"
short_label = "qmt"
command = "pi"
transport = "positional"
default_flags = "-p --provider openrouter --model qwen/qwen3-max-thinking"
color = 196
max_concurrent = 1

[agents.qwen3-max-thinking.permissions]
plan = "--tools read,grep,find,ls"
```

Notes:

- No `[done]` section is required; TOML-defined agents default to `DoneStrategy(type="api")` in `_agent_def_from_toml()`.
- No `health_check` is required because the dependency is still the local `pi` CLI; remote model availability is an operational concern, not a local binary concern.
- I would keep `hunter` as-is. It remains a distinct OpenRouter escalation tier above the new Qwen pool.

## 3. Agent selection guide

| Task shape | Recommended agent | Why | Next escalation |
|---|---|---|---|
| Single-file mechanical edits, exact structured prompts, cheap bulk dispatch | `pi` | Free local River path; lowest marginal cost | `qwen35-9b` |
| Fast triage, prompt cleanup, low-risk boilerplate, cheap fan-out | `qwen35-flash` | Lowest latency OpenRouter step with very low cost | `qwen35-9b` |
| Single-file or two-file structured work where local River misses | `qwen35-9b` | Cheapest paid model; good fit for the same task class as `pi` | `qwen35-35b` |
| Default paid Qwen escalation for multi-file reasoning across 2-4 files | `qwen35-35b` | Same family as River's local quantized 35B, but full-precision OpenRouter serving | `qwen35-122b` |
| Harder debugging, cross-file reasoning, more fragile tests or refactors | `qwen35-122b` | Better reasoning margin before jumping to expensive frontier agents | `hunter` or `qwen35-397b` |
| Long-context reads or high-capability Qwen step before Anthropic/OpenAI/Google | `qwen35-397b` | Strongest multimodal Qwen 3.5 model currently exposed here, with 262,144-token context on OpenRouter | `qwen3-max-thinking` or `hunter` |
| Hardest reasoning where latency is acceptable and you still want Qwen before Claude/Codex/Gemini | `qwen3-max-thinking` | Highest-cost Qwen reasoning tier in this set | `claude`, `codex`, or `gemini` |
| Frontier escalation after Qwen pool failure | `claude`, `codex`, `gemini` | Use when the failure mode is not just "needs a larger Qwen" but "needs a different model family/tooling stack" | N/A |

Manual routing chain for now: `pi` -> `qwen35-flash` or `qwen35-9b` -> `qwen35-35b` -> `qwen35-122b` -> `qwen35-397b` or `qwen3-max-thinking` -> `hunter` / `claude` / `codex` / `gemini`.

## 4. Implementation DAG

There are two different rollout modes here, and they should not be conflated.

- Immediate personal rollout: no repo code changes. Add the TOML entries above to `~/.dgov/agents.toml` and start routing to them manually.
- Repo-managed rollout: only needed if you want these aliases shipped as first-class built-ins inside `dgov` instead of remaining personal config.

That distinction matters because a pure `~/.dgov/agents.toml` change is not a good `dgov batch` target: batch tasks operate on a git worktree and require repo-relative file specs, while new agent definitions are explicitly blocked in project-local `.dgov/agents.toml` for security.

Code-change answer by file:

- `src/dgov/agents.py`: no change required for immediate rollout. Optional change only if you want built-in aliases instead of user-global config.
- `src/dgov/openrouter.py`: no change required. It is not in the worker launch path for `pi --provider openrouter --model ...`.
- `src/dgov/lifecycle.py`: no change required. It already launches arbitrary `pi`-routed agents from the registry.
- Tests: no repo test changes are required for the immediate user-config rollout. If you upstream built-in aliases, add tests in `tests/test_dgov_agents.py`; `tests/test_openrouter.py` and lifecycle tests do not need new coverage because the generic path is already covered.

Batch-consumable TOML for the optional repo-managed built-in rollout:

```toml
[dag]
version = 1
name = "openrouter-qwen35-builtins"
project_root = "."
session_root = "."
default_permission_mode = "bypassPermissions"
default_timeout_s = 900
default_max_retries = 1
merge_resolve = "skip"
merge_squash = true

[tasks.T0a]
summary = "Add built-in Qwen OpenRouter aliases"
agent = "pi"
escalation = ["claude"]
depends_on = []
prompt = "Edit src/dgov/agents.py to add built-in pi-routed OpenRouter aliases for qwen35-9b, qwen35-flash, qwen35-35b, qwen35-122b, qwen35-397b, and qwen3-max-thinking. Reuse the existing pi-openrouter/hunter pattern: command pi, positional transport, default_flags -p --provider openrouter --model <model_id>, and api done strategy. Do not change lifecycle or openrouter client code."
commit_message = "Add Qwen 3.5 OpenRouter agent aliases"

[tasks.T0a.files]
edit = ["src/dgov/agents.py"]

[tasks.T1a]
summary = "Test built-in Qwen aliases"
agent = "pi"
escalation = ["claude"]
depends_on = ["T0a"]
prompt = "Update tests/test_dgov_agents.py to cover the new built-in aliases: registry membership, default_flags, transport, and a build_launch_command smoke test for one representative alias. Do not add tests in openrouter.py or lifecycle.py unless the implementation actually touches those modules."
commit_message = "Test Qwen 3.5 agent aliases"
post_merge_check = "uv run pytest tests/test_dgov_agents.py -q -m unit"

[tasks.T1a.files]
edit = ["tests/test_dgov_agents.py"]
```

## 5. Cost analysis

Live model metadata was fetched from `https://openrouter.ai/api/v1/models` on 2026-03-18. OpenRouter reports pricing in dollars per token. For a typical dispatch with 500 input tokens and 2000 output tokens:

| Model | Prompt $/token | Completion $/token | Math | Cost / dispatch |
|---|---:|---:|---|---:|
| `qwen/qwen3.5-9b` | 0.00000005 | 0.00000015 | `500*0.00000005 + 2000*0.00000015` | $0.000325 |
| `qwen/qwen3.5-flash-02-23` | 0.000000065 | 0.00000026 | `500*0.000000065 + 2000*0.00000026` | $0.0005525 |
| `qwen/qwen3.5-35b-a3b` | 0.0000001625 | 0.0000013 | `500*0.0000001625 + 2000*0.0000013` | $0.00268125 |
| `qwen/qwen3.5-122b-a10b` | 0.00000026 | 0.00000208 | `500*0.00000026 + 2000*0.00000208` | $0.00429 |
| `qwen/qwen3.5-397b-a17b` | 0.00000039 | 0.00000234 | `500*0.00000039 + 2000*0.00000234` | $0.004875 |
| `qwen/qwen3-max-thinking` | 0.00000078 | 0.0000039 | `500*0.00000078 + 2000*0.0000039` | $0.00819 |

Practical reading:

- `qwen35-9b` and `qwen35-flash` are cheap enough for broad routine use.
- `qwen35-35b` is still cheap enough to be a default paid escalation tier.
- `qwen35-122b`, `qwen35-397b`, and `qwen3-max-thinking` are cheap in absolute terms per dispatch, but they will dominate spend if used as the default for every task.

## 6. Risk assessment

### Rate limits and spend

- OpenRouter rate limits are external to `dgov`; `openrouter.py` already handles HTTP 429 for the direct API client, but `pi --provider openrouter` failures will surface through the `pi` CLI path instead.
- `max_concurrent` in the proposed TOML blocks should be treated as a spend/rate-limit guardrail, not proof that OpenRouter will honor that concurrency.
- If the governor fans out many paid panes at once, the manual routing policy needs to remain conservative.

### Provider routing and model identity

- These agents depend on stable OpenRouter model ids such as `qwen/qwen3.5-35b-a3b`. If OpenRouter retires, renames, or silently retargets aliases, the config keeps launching but behavior can drift.
- OpenRouter also exposes canonical dated slugs behind some aliases. Example: `qwen/qwen3.5-9b` currently reports canonical slug `qwen/qwen3.5-9b-20260310`. That is useful for auditability, but you probably still want the stable alias in `agents.toml`.
- The current design intentionally leaves routing manual. That avoids accidental spend, but it means the governor prompting layer needs a clear selection rubric.

### Availability and context-window mismatch

- Operational truth should come from the OpenRouter models API, not marketing copy.
- On 2026-03-18, OpenRouter reports `qwen/qwen3-max-thinking` with `context_length = 262144` and `top_provider.max_completion_tokens = 32768`, not a 995M-token operational context. Treat 262,144 as the usable ceiling in `dgov` until the provider metadata changes.
- `qwen/qwen3.5-397b-a17b` currently matches the expected ~262K context (`262144`).
- `qwen/qwen3.5-flash-02-23` currently advertises a 1,000,000-token context on OpenRouter, which makes it attractive for large reads, but it is still a flash-tier model, not a substitute for the higher-capability reasoning tiers.
- `qwen/qwen3.5-9b` currently reports `top_provider.max_completion_tokens = 0`, which is effectively "unspecified" in the API payload. Do not assume long completions are safe there without a real smoke test.

### Modality mismatch

- Most of the new Qwen 3.5 models advertise `text+image+video->text` on OpenRouter.
- `dgov` worker launch currently passes text prompts through CLI transports. There is no first-class image/video attachment path in `agents.py` or `lifecycle.py` for these workers.
- So the multimodal/vision capability is a provider-side property, not a currently exposed `dgov` feature. It should not drive routing decisions until the worker transport supports non-text inputs.

## Recommendation

Adopt the new Qwen agents as user-global `pi` wrappers first. That gets you the routing pool you want now, with zero Python changes and zero repo-test churn. Only add built-in aliases to `src/dgov/agents.py` if you decide these models should ship as first-class `dgov` agents for everyone rather than remain private infra in `~/.dgov/agents.toml`.
