# Plumbing Audit

Scope: review of the recent long-command change in `src/dgov/tmux.py::send_command()` and its use from `src/dgov/lifecycle.py::_setup_and_launch_agent()`.

Method:
- Read the current implementations and recent commits affecting both paths.
- Traced all startup transports through `_setup_and_launch_agent()`.
- Ran two local tmux/zsh checks:
  - raw `tmux send-keys` preserved commands up to 820 characters on this host
  - two back-to-back sourced scripts executed in order, and the second saw env exported by the first even when the first slept for 1 second

## Findings

### High: `send_input()` no longer means "send literal terminal input" for long text

Severity: High

Evidence:
- `src/dgov/backend.py:48-50` defines `send_input()` as "Send text input to the worker (like typing in a terminal)."
- `src/dgov/backend.py:143-146` maps that API directly to `tmux.send_command()`.
- `src/dgov/tmux.py:66-83` now rewrites any input longer than 200 characters into `source <tempfile> && rm -f <tempfile>`.
- Non-startup callers use `send_input()` for literal runtime interaction, not shell commands:
  - `src/dgov/cli/pane.py:700-714`
  - `src/dgov/responder.py:148-150`
  - `src/dgov/waiter.py:391-414`

Why this matters:
- The new behavior is correct only while the pane is sitting at a shell prompt.
- Once an agent CLI is already running, a long message is no longer delivered as literal input. The foreground program receives the text `source /tmp/...` instead of the intended message.
- `_setup_and_launch_agent()` happens to be safe because its `send_input()` calls run before the interactive agent prompt is in control. The helper contract itself is still broken.

Recommendation:
- Split the API into two explicit operations:
  - `send_shell_command()` for startup/bootstrap shell commands
  - `send_text_input()` for literal runtime input to the foreground program
- Route `_setup_and_launch_agent()` through the shell-command path unconditionally.
- Keep `pane send`, autoresponder, nudges, and send-keys prompt delivery on the literal-input path.

### Medium: the 200-character threshold is arbitrary and brittle

Severity: Medium

Evidence:
- `src/dgov/tmux.py:72-83` uses `len(command) <= 200` as the branch point.
- No adjacent code or tests establish 200 as a tmux or zsh boundary.
- On this host, direct `tmux send-keys` worked with commands well above 200 characters, including 820-character commands.

Why this matters:
- The cutoff is a heuristic, not a correctness boundary.
- Edge behavior changes abruptly at 200 vs. 201 characters even though the underlying command semantics are identical.
- `len(command)` is counting Python characters, not bytes sent to the terminal.
- The env-export line in `_setup_and_launch_agent()` will often exceed 200, so whether it uses the script path depends on incidental path length and env size rather than intent.

Recommendation:
- Do not use a length threshold on the generic input API.
- For bootstrap shell commands, use the script-backed path explicitly regardless of length.
- If a threshold is kept temporarily, make it a named constant and add boundary tests for 199, 200, and 201 characters.

### Medium: the temp-file creation is mostly secure, but the shell invocation is not fully hardened

Severity: Medium

Evidence:
- `tempfile.NamedTemporaryFile(..., delete=False)` creates a randomized basename and, on this host, mode `0600`.
- That means the classic "predictable file in /tmp" problem is mostly mitigated.
- The shell line in `src/dgov/tmux.py:83` interpolates `script_path` without quoting.

Why this matters:
- `tempfile` honors `TMPDIR`. If the temp directory path contains spaces or shell metacharacters, the unquoted `source {script_path} && rm -f {script_path}` line can misparse or execute unintended shell syntax.
- There is still a close-to-use window where another process running as the same UID could replace the file before the shell sources it. That is not a strong cross-user TOCTOU, but it is still a real integrity gap.

Recommendation:
- Quote the path with `shlex.quote()` everywhere it is inserted into shell text.
- Prefer a private command-script directory under the session state tree, such as `.dgov/state/cmd/`, created with `0700`, rather than ambient temp directories.

### Low: temp-script cleanup is best-effort only

Severity: Low

Evidence:
- `src/dgov/tmux.py:83` deletes the file only on `source ... && rm -f ...`.
- Cleanup is skipped if `source` fails due to shell parse error, missing file, unsupported shell semantics, or interruption before the chained `rm` runs.

Why this matters:
- Launch scripts usually do get removed because `_wrap_done_signal()` in `src/dgov/done.py:28-31` returns success in both the `then` and `else` branches, so agent nonzero exit does not block the trailing `rm`.
- Parse failures and source failures still leak temp files.
- The env-export script can also leak if any generated shell line fails before the `&& rm -f` executes.

Recommendation:
- Use unconditional cleanup while preserving status, for example:
  - `source "$p"; status=$?; rm -f "$p"; [ "$status" -eq 0 ]`
- Alternatively, write the cleanup into the generated script with a trap.

### Low: no extra timing gap is needed between env setup and launch in the current lifecycle path

Severity: Low

Evidence:
- `_setup_and_launch_agent()` sends env setup at `src/dgov/lifecycle.py:195-213` and then sends the launch command at:
  - `src/dgov/lifecycle.py:254-270` for `send-keys`
  - `src/dgov/lifecycle.py:271-282` for all other transports
- The env step consists only of `unset` and `export` builtins.
- In a live tmux check, a second sourced command queued immediately after a first sourced command still ran after the first completed and observed its exported environment.

Why this matters:
- The current code does not need a sleep between the two `send_input()` calls.
- A timing hack would hide the real dependency rather than model it.

Recommendation:
- No timing gap is needed for the current code.
- If future env setup ever includes commands that read stdin or change terminal state, merge env setup and launch into a single shell script or single shell command instead of adding sleeps.

## Direct Answers

1. Is the 200-char threshold correct?

No. It is a heuristic, not a verified boundary. The edge at 200/201 is arbitrary, and local tmux/zsh checks did not reproduce a break at that size. The safer design is to choose transport by intent:
- shell bootstrap commands: explicit script/file path
- literal runtime input: literal keystrokes/buffer paste

2. Could the launch `source` arrive before the env `source` finishes?

Not in the current lifecycle code. Both are queued to the same pane in order, and the env script only runs shell builtins. No extra timing gap is needed.

3. Any cleanup failure modes?

Yes. Temp files are leaked if `source` itself fails, if the pane is interrupted before the trailing `rm`, or if the shell cannot parse the generated line. Agent nonzero exit is not the main issue because the done-signal wrapper still returns success overall.

4. Does this break any transport type in `_setup_and_launch_agent()`?

Startup behavior is intact for:
- `positional`
- `option`
- `stdin`
- `send-keys`

Reason:
- positional/option/stdin already externalize prompt content into a separate prompt file in `src/dgov/agents.py:579-614`
- send-keys agents send only the base launch command through `send_input()`, then deliver the prompt via `send_prompt_via_buffer()`

The real break is broader than startup: long literal runtime input now stops being literal input.

5. Is there a predictable-name `/tmp` TOCTOU risk?

Not in the classic form. `NamedTemporaryFile` gives a randomized name and secure permissions. The remaining risks are:
- same-UID replacement between close and `source`
- unquoted path interpolation in shell text

6. Should the env export path also use script files?

Yes, but explicitly, not accidentally via the 200-character threshold. `_setup_and_launch_agent()` is sending shell bootstrap commands, so it should always use a shell-command path independent of string length.

7. Is `send_prompt_via_buffer()` still using paste-buffer for send-keys agents consistent?

Yes. That function is sending literal prompt text to an already-running interactive agent UI. It should not be routed through a shell-script/source mechanism.

8. Any impact on the done-signal wrapper?

No new semantic break from the wrapper itself. Wrapped launch commands still behave the same, and because the wrapper returns success even on agent failure, it usually allows temp-script cleanup to run. The remaining cleanup failures are parse/source failures, not done-wrapper failures.
