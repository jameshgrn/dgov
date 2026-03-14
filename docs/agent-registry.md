# Agent registry

dgov provides a unified interface for AI coding agents. This registry handles the complexity of delivering prompts, managing permissions, and tracking concurrency limits.

## Built-in agents

dgov ships defaults for 11 built-in agents:

| ID | Name | CLI Command | Transport | Resume Support |
|----|------|-------------|-----------|----------------|
| `claude` | Claude Code | `claude` | positional | Yes |
| `codex` | Codex | `codex` | positional | Yes |
| `gemini` | Gemini CLI | `gemini` | option | Yes |
| `opencode` | OpenCode | `opencode` | option | No |
| `cline` | Cline CLI | `cline` | send-keys | No |
| `qwen` | Qwen CLI | `qwen` | option | Yes |
| `amp` | Amp CLI | `amp` | stdin | No |
| `pi` | pi CLI | `pi` | positional | Yes |
| `cursor` | Cursor CLI | `cursor-agent` | positional | No |
| `copilot` | Copilot CLI | `copilot` | option | Yes |
| `crush` | Crush CLI | `crush run` | send-keys | No |

All built-in agents can be overridden via user or project TOML configuration.

## AgentDef fields

Each agent is defined by an `AgentDef` with these fields:

**Required:**
- `command` (`prompt_command`): the CLI executable to invoke.
- `transport`: how the prompt is delivered — `positional`, `option`, `stdin`, or `send-keys`.

**Optional:**
- `prompt_option`: flag name for `option` transport (e.g., `--prompt`, `-i`).
- `no_prompt_command`: alternate command for launching without a prompt.
- `default_flags`: flags appended to every invocation.
- `color`: ANSI color code for terminal output (integer).
- `max_concurrent`: limit on simultaneous workers for this agent.
- `health_check`: shell command to verify the agent is available.
- `health_fix`: shell command to repair a failed health check.
- `env`: dict of environment variables set when launching the agent.
- `permission_flags`: maps permission modes to CLI flags (see below).
- `resume_template`: command template for resuming a session; `{permissions}` is replaced with resolved flags.
- `send_keys_pre_prompt`: tuple of keys to send before pasting the prompt (send-keys only).
- `send_keys_submit`: tuple of keys to send after the prompt (default: `("Enter",)`).
- `send_keys_post_paste_delay_ms`: delay after pasting prompt (send-keys only).
- `send_keys_ready_delay_ms`: delay to wait for agent readiness (send-keys only).
- `max_retries`: automatic retry count on failure.
- `retry_escalate_to`: agent ID to escalate to after exhausting retries.
- `source`: provenance label (`built-in`, `user`, or `project`).

## Transport types

dgov abstracts how the prompt is delivered to the agent:

- **positional**: prompt is passed as a trailing argument (`agent-cli "my prompt"`).
- **option**: prompt is passed via a specific CLI flag (`agent-cli --prompt "my prompt"`). Requires `prompt_option`.
- **stdin**: prompt is piped into the agent's standard input (`printf "my prompt" | agent-cli`).
- **send-keys**: prompt is typed into the agent's tmux pane using `tmux send-keys`. Supports `send_keys_pre_prompt`, `send_keys_submit`, and delay tuning.

## Permission modes

Each agent maps dgov's generic permission modes to their own CLI flags via `permission_flags`:

- **plan**: only allow reading and planning, no code execution.
- **acceptEdits** (default): allow writing code but prompt for confirmation.
- **bypassPermissions**: fully autonomous mode (YOLO).

Permission flags are interpolated into `resume_template` via the `{permissions}` placeholder.

## Listing agents

Use `dgov agents` to see all registered agents and whether they are installed on your current `PATH`.

```bash
dgov agents
```

## User configuration

Add custom agents or override built-ins by creating `~/.dgov/agents.toml` (global) or `.dgov/agents.toml` (per-project).

### Adding a custom agent

```toml
[agents.pi]
command = "pi"
transport = "positional"
default_flags = "--provider my-gpu-server"
color = 34
max_concurrent = 2
health_check = "curl -sf http://localhost:8080/health"

[agents.pi.permissions]
plan = "--tools read,grep,find,ls"

[agents.hunter]
command = "pi"
transport = "positional"
default_flags = "--provider openrouter --model openrouter/hunter-alpha"
color = 208
max_concurrent = 3

[agents.hunter.permissions]
plan = "--tools read,grep,find,ls"

[agents.qwen]
command = "qwen"
transport = "option"
prompt_option = "-i"
color = 34

[agents.qwen.permissions]
plan = "--approval-mode plan"
acceptEdits = "--approval-mode auto-edit"
bypassPermissions = "--approval-mode yolo"
```

### Overriding a built-in

```toml
[agents.claude]
default_flags = "--model claude-sonnet-4-20250514"
max_concurrent = 4
```

### Full field reference

```toml
[agents.myagent]
name = "My Custom Agent"
command = "myagent-cli"
transport = "positional"
prompt_option = "--prompt"          # required if transport = "option"
default_flags = "--verbose"
color = 45
max_concurrent = 2
health_check = "myagent-cli --version"
health_fix = "brew upgrade myagent-cli"
max_retries = 1
retry_escalate_to = "claude"

[agents.myagent.permissions]
plan = "--plan-mode"
acceptEdits = "--auto-accept"
bypassPermissions = "--yolo"

[agents.myagent.resume]
template = "myagent-cli --continue{permissions}"

[agents.myagent.env]
OPENAI_API_KEY = "sk-..."
```

## Merge layers

dgov merges agent configuration in priority order (later layers override earlier):

1. **Built-in defaults** — shipped with dgov.
2. **User global** (`~/.dgov/agents.toml`) — your personal overrides.
3. **Project local** (`.dgov/agents.toml`) — project-specific overrides.

Each layer can override any field. For security, `health_check` and `health_fix` are ignored in project-level config.

## User-defined agents vs built-ins

The built-in agents provide sensible defaults for popular CLIs. Many users define custom agents that wrap the same or different tools with tuned settings:

- **pi** — configured with a specific `--provider` pointing to a local GPU or remote endpoint.
- **hunter** — wraps `pi` with `--provider openrouter --model openrouter/hunter-alpha` for access to Hunter Alpha via OpenRouter.
- **qwen** — configured with `--approval-mode` flags mapped to dgov permission modes.

These appear in `~/.dgov/agents.toml` alongside the built-in definitions and override them via the merge layer system.

## Auto-classification

dgov can automatically choose the best agent for a task using a local Qwen 4B model.

```bash
# Mechanical tasks go to pi, complex ones to claude
dgov pane create -a auto -p "format all python files"
```

Use `dgov pane classify` to see what the model recommends without launching a pane:

```bash
dgov pane classify "debug the flaky scheduler test"
```

## Concurrency limits

You can set a `max_concurrent` limit per agent in your `agents.toml`. dgov will refuse to dispatch new workers for that agent if the limit is reached.
