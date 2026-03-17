## P0 (will crash or corrupt)

- **None found for interactive TUI launch path**: The reviewed code paths (`build_launch_command`, `_setup_and_launch_agent`, `resume_worker_pane`) do not contain deterministic crashes or state corruption under normal operation. Failures are mostly in the category of leaks, fragility, and race-prone behavior rather than hard crashes or data loss.

## P1 (incorrect behavior)

- **Interactive agents write unused prompt files that are never cleaned up**
  - In `build_launch_command`, when `agent.interactive` is `True` and a `prompt` is provided, the function calls `_write_prompt_file(project_root, slug, prompt)` and then immediately returns `agent.no_prompt_command or base` without incorporating the file path into the command or any cleanup snippet.
  - In `_setup_and_launch_agent`, the interactive branch (`if agent_def.interactive:`) passes `rewritten_prompt` into `build_launch_command`. This causes `_write_prompt_file` to run (creating `.dgov/prompts/<slug>--<ts>-<rand>.txt` in the worktree) even though the agent is launched without consuming that file, and the prompt is later re-sent via `backend.send_prompt_via_buffer`.
  - Result: every interactive launch (including resumes) leaks an orphaned prompt file on disk that is never read or removed. This is incorrect relative to the documented behavior in `build_launch_command` (where prompt files are normally read-then-deleted via the shell snippet) and will accumulate garbage over time.

- **Prompt-file write on interactive resume path is also incorrect**
  - `resume_worker_pane` calls `_setup_and_launch_agent` with `agent_def` and `prompt=full_prompt`. For interactive agents, this again takes the `agent_def.interactive` path, which calls `build_launch_command` with a non-empty prompt.
  - The same `_write_prompt_file` call occurs, again with no corresponding read/delete, so each resume of an interactive agent leaks a new, unused prompt file.
  - This is functionally wrong (no consumer of the file), and it compounds the leak behavior over the lifetime of a long-running project.

- **Hard-coded ready delays can cause prompts to be sent to the wrong context**
  - For interactive agents, `_setup_and_launch_agent` does:
    - `wrapped_cmd = _wrap_done_signal(base_cmd, done_signal)`
    - `backend.send_shell_command(pane_id, wrapped_cmd)`
    - `ready_delay = agent_def.send_keys_ready_delay_ms or 2000`
    - `time.sleep(ready_delay / 1000)`
    - `backend.send_prompt_via_buffer(pane_id, rewritten_prompt)`
  - If the agent TUI is slower than `send_keys_ready_delay_ms` (3s for `claude`, 5s for `codex`/`cursor`, or the 2s fallback), the pasted prompt may arrive while the shell is still at a regular shell prompt or in an unexpected phase of the CLI startup. That can lead to the prompt being interpreted as shell commands or partially dropped, which is **incorrect behavior** from the caller’s perspective (prompt not reliably delivered to the agent).
  - There is no readiness handshake, retry, or verification — just a fixed sleep — so on slower or loaded machines this is a real behavioral bug, not just a cosmetic issue.

## P2 (missing coverage / fragility)

- **Prompt file lifecycle for interactive agents is inconsistent with non-interactive paths**
  - Non-interactive paths in `build_launch_command` construct a shell snippet via `_prompt_read_and_delete_snippet` that both reads and deletes the prompt file, ensuring no persistent artifacts.
  - Interactive paths currently write the file but never read or delete it, which indicates missing design coverage for how prompt files should behave in pure send-keys/TUI flows. At minimum, interactive mode should either:
    - Avoid writing the file entirely (since the prompt is delivered via buffer), or
    - Use the same read-then-delete snippet if future interactive agents decide to consume prompts from disk.
  - The current behavior suggests the interactive branch was bolted on without updating the prompt-file lifecycle design.

- **Cursor trust 'a' keystroke is brittle to upstream changes and trust state**
  - After launching the agent (any transport), `_setup_and_launch_agent` does:
    - `if agent_id in ("cursor", "cursor-auto") or agent_def.prompt_command == "cursor-agent":`
    - `    time.sleep(3)`
    - `    backend.send_keys(pane_id, ["a"])`
  - This assumes that:
    - The Cursor CLI will always present a workspace trust dialog on first run, and
    - Pressing `'a'` is the correct action key to accept trust.
  - If the workspace is already trusted (no dialog appears), that `'a'` becomes a stray keystroke in whatever prompt field or shell context is active. If `cursor-agent` changes its trust UX (different keybinding, different timing, different dialog), the `'a'` may become inert or actively harmful (e.g., entering a literal `a` into the command line, triggering an unknown command).
  - There is no detection that a trust dialog is present, no conditional logic, and no configuration to turn this off — making this path fragile with respect to upstream changes and user environment, i.e., missing robustness/coverage rather than a well-specified behavior.

- **send_keys_ready_delay_ms lacks adaptive readiness checks**
  - Both interactive (`agent_def.interactive`) and generic `send-keys` transports rely purely on sleep-based timing (`send_keys_ready_delay_ms` or a 2s default) before sending the prompt and any `send_keys_pre_prompt` sequences.
  - There is no attempt to:
    - Inspect tmux pane output for a known-ready pattern,
    - Retry if the pasted prompt does not appear in the expected UI, or
    - Detect that the agent process failed to start.
  - This means transient slowness, resource contention, or CLI internal changes can cause silent prompt misdelivery. That is missing defensive coverage for a key UX path (interactive TUI launches).

- **Resume path reuses interactive launch behavior without specialized handling**
  - `resume_worker_pane` reuses `_setup_and_launch_agent` with `prompt=full_prompt` and `hook_prompt=original_prompt`, but does not consider whether interactive agents might require a different resume UX (e.g., showing prior context differently, or avoiding double-injecting large prompts into a running TUI).
  - Functionally this works, but it inherits all the interactive-path fragility (prompt-file leak, timing-only readiness, cursor trust auto-accept) without any additional guards or adjustments for the resume scenario. This is more of a coverage/design gap than a concrete crash/logic bug.
