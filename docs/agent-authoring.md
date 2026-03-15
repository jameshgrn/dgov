# Adding a custom agent

This guide walks through registering a new CLI tool as a dgov agent so you can dispatch it as a worker.

## Prerequisites

Your CLI tool must:

1. Be on `PATH` (dgov uses `shutil.which` to detect it).
2. Accept a task prompt via one of the four [transport types](#transport-types).
3. Exit when done (dgov monitors the tmux pane for exit).

## agents.toml format

Add entries to `~/.dgov/agents.toml` (user-global) or `<project>/.dgov/agents.toml` (per-project).

```toml
[agents.<id>]
name = "Human-readable Name"          # default: the agent ID
short_label = "xx"                     # 2-char label for UI (default: first 2 chars of ID)
command = "my-cli"                     # required — the executable to invoke
transport = "positional"               # required — how the prompt is delivered
prompt_option = "--prompt"             # required when transport = "option"
no_prompt_command = "my-cli"           # alternate command when no prompt is given
default_flags = "--verbose"            # appended to every invocation
color = 45                             # ANSI color code (0-255) for terminal UI
max_concurrent = 2                     # cap on simultaneous workers
health_check = "my-cli --version"     # shell command to verify availability
health_fix = "brew upgrade my-cli"    # shell command to repair a failed health check
max_retries = 1                        # auto-retry count on failure
retry_escalate_to = "claude"           # agent to escalate to after retries exhausted

[agents.<id>.permissions]
plan = "--read-only"                   # maps dgov "plan" mode
acceptEdits = "--auto-accept"          # maps dgov "acceptEdits" mode
bypassPermissions = "--yolo"           # maps dgov "bypassPermissions" mode

[agents.<id>.resume]
template = "my-cli --continue{permissions}"  # {permissions} is replaced with resolved flags

[agents.<id>.env]
MY_API_KEY = "sk-..."                  # environment variables set at launch
```

### Field details

| Field | Required | Description |
|-------|----------|-------------|
| `command` | Yes | CLI executable (first token is looked up on PATH) |
| `transport` | Yes | One of `positional`, `option`, `stdin`, `send-keys` |
| `prompt_option` | If `option` | The flag that accepts the prompt text |
| `name` | No | Display name (defaults to agent ID) |
| `short_label` | No | 2-char UI label (defaults to ID[:2]) |
| `no_prompt_command` | No | Command when launching without a prompt |
| `default_flags` | No | Always appended to the command |
| `color` | No | ANSI 256-color code for terminal output |
| `max_concurrent` | No | Max simultaneous workers for this agent |
| `health_check` | No | Shell command run before launch; must exit 0 |
| `health_fix` | No | Shell command run if health_check fails |
| `max_retries` | No | Auto-retry count (default: 0) |
| `retry_escalate_to` | No | Agent ID to use after retries exhausted |
| `permissions` | No | Sub-table mapping dgov modes to CLI flags |
| `resume.template` | No | Command template for resuming; `{permissions}` is interpolated |
| `env` | No | Sub-table of environment variables |

!!! warning "Security: project-level restrictions"
    `health_check` and `health_fix` are **ignored** in project-level `.dgov/agents.toml` files. These fields run with `shell=True` and could execute arbitrary code from a malicious repo. Define them only in `~/.dgov/agents.toml`.

## Transport types

The transport determines how dgov delivers the task prompt to your agent.

### positional

The prompt is passed as a trailing shell argument.

```
my-cli "the task prompt"
```

```toml
[agents.myagent]
command = "my-cli"
transport = "positional"
```

Best for CLIs that accept a prompt as their last positional argument (like `claude`, `codex`).

### option

The prompt is passed via a named flag.

```
my-cli --prompt "the task prompt"
```

```toml
[agents.myagent]
command = "my-cli"
transport = "option"
prompt_option = "--prompt"
```

Best for CLIs where the prompt is behind a flag (like `gemini --prompt-interactive`, `opencode --prompt`).

### stdin

The prompt is piped into standard input.

```
printf '%s\n' "the task prompt" | my-cli
```

```toml
[agents.myagent]
command = "my-cli"
transport = "stdin"
```

Best for CLIs that read from stdin (like `amp`).

### send-keys

The prompt is typed into the agent's tmux pane using `tmux send-keys`. Use this for interactive TUIs that don't accept prompts via arguments or stdin.

```toml
[agents.myagent]
command = "my-cli"
transport = "send-keys"
send_keys_pre_prompt = ["Escape", "Tab"]   # keys sent before the prompt
send_keys_submit = ["Enter"]               # keys sent after (default: ["Enter"])
send_keys_post_paste_delay_ms = 200        # delay after pasting
send_keys_ready_delay_ms = 1500            # delay for TUI to initialize
```

The `no_prompt_command` field is important here — it's used as the actual launch command since the prompt is delivered separately.

## How detection works

When you run `dgov agents`, dgov checks each agent's `command` field:

1. Takes the first token of `command` (e.g., `"crush run"` becomes `"crush"`).
2. Calls `shutil.which()` on that token.
3. If found on PATH, the agent is marked as **installed**.

This means your CLI must be on PATH. If it's a local script, add its directory to PATH or create a symlink.

## Worked example: adding "myagent"

Suppose you have a CLI tool called `myagent` that:

- Accepts prompts via `--instruction "..."`
- Has a `--auto` flag for autonomous mode
- Needs an API key in the environment

### 1. Verify it's on PATH

```bash
which myagent
# /usr/local/bin/myagent
```

### 2. Add the config

Create or edit `~/.dgov/agents.toml`:

```toml
[agents.myagent]
name = "My Agent"
command = "myagent"
transport = "option"
prompt_option = "--instruction"
color = 220
max_concurrent = 3
health_check = "myagent --version"
health_fix = "pip install --upgrade myagent"

[agents.myagent.permissions]
acceptEdits = "--auto"
bypassPermissions = "--auto --no-confirm"

[agents.myagent.resume]
template = "myagent --resume{permissions}"

[agents.myagent.env]
MYAGENT_API_KEY = "sk-abc123"
```

### 3. Verify registration

```bash
dgov agents
```

Output includes your agent with installation and health status:

```json
{
  "id": "myagent",
  "name": "My Agent",
  "installed": true,
  "transport": "option",
  "source": "user",
  "healthy": true
}
```

### 4. Dispatch a worker

```bash
dgov pane create -a myagent -p "refactor the parser module" -m bypassPermissions
```

dgov builds:

```
myagent --auto --no-confirm --instruction "refactor the parser module"
```

### 5. Set as default (optional)

In `~/.dgov/config.toml`:

```toml
[dgov]
default_agent = "myagent"
```

Now `dgov pane create -p "..."` uses myagent without `-a`.

## Overriding built-in agents

You don't need to redefine every field. Override only what you want to change:

```toml
[agents.claude]
default_flags = "--model claude-sonnet-4-20250514"
max_concurrent = 4
```

This merges with the built-in claude definition — transport, permissions, resume template, etc. are all preserved.

## Merge order

Configuration is layered (later wins):

1. **Built-in** — shipped with dgov
2. **User global** — `~/.dgov/agents.toml`
3. **Project local** — `.dgov/agents.toml`

Each layer merges field-by-field. Sub-tables (`permissions`, `env`) are replaced wholesale at each layer, not deep-merged — if you define `[agents.claude.permissions]` in your config, it replaces the entire built-in permissions map.

## Troubleshooting

**Agent not showing as installed**: Check that `which <command>` finds it. The `command` field's first token must be on PATH.

**Health check failing**: Run the `health_check` command manually to see the error. The health check must exit 0 to pass.

**Prompt not delivered**: Verify you picked the right transport. Test manually: run the command with your prompt the way dgov would build it.

**send-keys agent not receiving prompt**: Increase `send_keys_ready_delay_ms` — the TUI may need more time to initialize before it can accept input.
