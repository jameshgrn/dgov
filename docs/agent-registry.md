# Agent registry

dgov provides a unified interface for 11+ AI coding agents. This registry handles the complexity of delivering prompts, managing permissions, and tracking concurrency limits.

## Built-in agents

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
| `cursor` | Cursor CLI | `cursor-agent`| positional | No |
| `copilot` | Copilot CLI | `copilot` | option | Yes |
| `crush` | Crush CLI | `crush run` | send-keys | No |

## Transport types

dgov abstracts how the prompt is delivered to the agent:

- **positional**: prompt is passed as a trailing argument (`agent-cli "my prompt"`).
- **option**: prompt is passed via a specific CLI flag (`agent-cli --prompt "my prompt"`).
- **stdin**: prompt is piped into the agent's standard input (`printf "my prompt" | agent-cli`).
- **send-keys**: prompt is typed into the agent's tmux pane using `tmux send-keys`.

## Permission modes

Each agent maps dgov's generic permission modes to their own CLI flags:

- **plan**: only allow reading and planning, no code execution.
- **acceptEdits** (default): allow writing code but prompt for confirmation.
- **bypassPermissions**: fully autonomous mode (YOLO).

## Listing agents

Use `dgov agents` to see all registered agents and whether they are installed on your current `PATH`.

```bash
dgov agents
```

## User configuration

You can add custom agents or override built-ins by creating `~/.dgov/agents.toml`.

```toml
[agents.myagent]
name = "My Custom Agent"
command = "myagent-cli"
transport = "positional"
color = 45
max_concurrent = 2

[agents.myagent.permissions]
acceptEdits = "--auto-accept"
bypassPermissions = "--yolo"

[agents.myagent.resume]
template = "myagent-cli --continue{permissions}"
```

## Project configuration

Project-specific agents can be defined in `.dgov/agents.toml` inside your repository. For security, `health_check` and `health_fix` fields are ignored in project-level config files.

## Merging layers

dgov merges configuration in the following order:
1. **Built-in defaults**
2. **User global** (`~/.dgov/agents.toml`)
3. **Project local** (`.dgov/agents.toml`)

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
