"""OpenAI-compatible provider wrapper with rate-limit backoff."""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlparse

from openai import OpenAI

RATE_LIMIT_BACKOFF_S = (5.0, 30.0, 90.0)
_JITTER_FRACTION = 0.2
_PROVIDER_TOKEN_LIMIT_MARKER = "Provider token limit exceeded"
CLAUDE_CODE_PROVIDER_SCHEME = "claude-code"
_DEFAULT_CLAUDE_CODE_RUNNER = (
    Path.home() / ".codex" / "skills" / "invoke-claude" / "scripts" / "run_claude_code.py"
)
_DEFAULT_CLAUDE_CODE_PROFILE = "daily"
_DEFAULT_CLAUDE_CODE_PRESET = "review"
_DEFAULT_CLAUDE_CODE_TIMEOUT_S = 600.0
_WORKTREE_PATTERNS = (
    re.compile(r"git worktree:\s*([^\)\n]+)"),
    re.compile(r"project root:\s*([^\)\n]+)"),
)


@dataclass(frozen=True)
class TokenLimitPolicy:
    """Configured provider token-limit headers for fail-fast 429 handling."""

    label: str
    prompt_header: str = ""
    generated_header: str = ""


class ProviderRateLimitError(Exception):
    """Raised when a request exceeds configured provider token limits."""

    def __init__(
        self,
        *,
        provider_label: str,
        limit_type: str,
        estimated_tokens: int,
        observed_limit: int,
        message: str | None = None,
    ) -> None:
        self.provider_label = provider_label
        self.limit_type = limit_type
        self.estimated_tokens = estimated_tokens
        self.observed_limit = observed_limit
        if message is None:
            message = self._build_message()
        super().__init__(message)

    def _build_message(self) -> str:
        actions = (
            "Suggested actions: shrink prompt/tool schema, reduce concurrency, "
            "use 'dgov run --continue' after cooldown, switch model/provider, "
            "or use an on-demand deployment."
        )
        return (
            f"{_PROVIDER_TOKEN_LIMIT_MARKER} ({self.provider_label}): "
            f"{self.limit_type} token limit exceeded. "
            f"Estimated {self.limit_type} tokens: {self.estimated_tokens}, "
            f"observed limit: {self.observed_limit}. {actions}"
        )


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    if isinstance(response_status, int):
        return response_status
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    if _status_code(exc) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "too many requests" in text


def _get_headers_from_exc(exc: Exception) -> Mapping[str, str]:
    """Extract headers from exception response, case-insensitively."""
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers is not None:
            return headers
    # Some clients put headers directly on the exception
    headers = getattr(exc, "headers", None)
    if headers is not None:
        return headers
    return {}


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Get header value case-insensitively."""
    name_lower = name.lower()
    for key, value in headers.items():
        if key.lower() == name_lower:
            return value
    return None


def _extract_retry_after(headers: Mapping[str, str]) -> float | None:
    """Extract Retry-After header value in seconds."""
    value = _get_header(headers, "retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _extract_token_limits(
    headers: Mapping[str, str],
    policy: TokenLimitPolicy,
) -> tuple[int | None, int | None]:
    """Extract configured prompt/generated token limits from headers.

    Returns:
        Tuple of (prompt_limit, generated_limit) or (None, None) if not present.
    """
    import contextlib

    prompt_limit = _get_header(headers, policy.prompt_header) if policy.prompt_header else None
    generated_limit = (
        _get_header(headers, policy.generated_header) if policy.generated_header else None
    )

    prompt_val: int | None = None
    generated_val: int | None = None

    if prompt_limit is not None:
        with contextlib.suppress(ValueError, TypeError):
            prompt_val = int(prompt_limit)

    if generated_limit is not None:
        with contextlib.suppress(ValueError, TypeError):
            generated_val = int(generated_limit)

    return (prompt_val, generated_val)


def _estimate_tokens_from_length(text: str) -> int:
    """Estimate tokens from text length using 4 chars per token heuristic."""
    if not text:
        return 0
    return (len(text) + 3) // 4  # Round up division


def _estimate_request_tokens(kwargs: dict[str, Any]) -> tuple[int, int]:
    """Estimate prompt and requested generated tokens from chat completion kwargs.

    Returns:
        Tuple of (estimated_prompt_tokens, estimated_generated_tokens).
        estimated_generated_tokens will be 0 if max_tokens is not specified.
    """
    return (_estimate_prompt_tokens(kwargs), _estimate_generated_tokens(kwargs))


def _estimate_prompt_tokens(kwargs: dict[str, Any]) -> int:
    estimated_prompt = _estimate_message_tokens(kwargs.get("messages", []))
    tools = kwargs.get("tools", [])
    if tools:
        estimated_prompt += _estimate_tokens_from_length(json.dumps(tools))
    return estimated_prompt


def _estimate_message_tokens(messages: object) -> int:
    estimated_prompt = 0
    if not isinstance(messages, list):
        return estimated_prompt
    for message in messages:
        estimated_prompt += _estimate_message_token_count(message)
    return estimated_prompt


def _estimate_message_token_count(message: object) -> int:
    if not isinstance(message, dict):
        return 0
    message = cast("dict[str, Any]", message)
    estimated = 0
    content = message.get("content", "")
    if content:
        estimated += _estimate_tokens_from_length(str(content))
    tool_calls = message.get("tool_calls", [])
    if tool_calls:
        estimated += _estimate_tokens_from_length(json.dumps(tool_calls))
    return estimated


def _estimate_generated_tokens(kwargs: dict[str, Any]) -> int:
    max_tokens = kwargs.get("max_tokens")
    if max_tokens is None:
        return 0
    try:
        return int(max_tokens)
    except (ValueError, TypeError):
        return 0


def _check_token_limits(
    exc: Exception,
    kwargs: dict[str, Any],
    policy: TokenLimitPolicy | None,
) -> None:
    """Check configured provider token limits and raise if exceeded.

    This function extracts limits from response headers and compares against
    estimated request tokens. It raises immediately if limits are exceeded
    to avoid wasteful retries.
    """
    if policy is None:
        return
    headers = _get_headers_from_exc(exc)
    prompt_limit, generated_limit = _extract_token_limits(headers, policy)

    if prompt_limit is None and generated_limit is None:
        return  # No configured limits detected, let normal retry handle it

    estimated_prompt, estimated_generated = _estimate_request_tokens(kwargs)

    if prompt_limit is not None and estimated_prompt > prompt_limit:
        raise ProviderRateLimitError(
            provider_label=policy.label,
            limit_type="prompt",
            estimated_tokens=estimated_prompt,
            observed_limit=prompt_limit,
        )

    if generated_limit is not None and estimated_generated > generated_limit:
        raise ProviderRateLimitError(
            provider_label=policy.label,
            limit_type="generated",
            estimated_tokens=estimated_generated,
            observed_limit=generated_limit,
        )


def _jittered_delay(
    base_delay_s: float,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> float:
    jitter_span = base_delay_s * _JITTER_FRACTION
    return max(0.0, base_delay_s + jitter_fn(-jitter_span, jitter_span))


def _token_limit_retry_decision(
    headers: Mapping[str, Any],
    kwargs: dict[str, Any],
    policy: TokenLimitPolicy | None,
) -> tuple[bool, float | None] | None:
    if policy is None:
        return None
    prompt_limit, generated_limit = _extract_token_limits(headers, policy)
    if prompt_limit is None and generated_limit is None:
        return None

    estimated_prompt, estimated_generated = _estimate_request_tokens(kwargs)
    if prompt_limit is not None and estimated_prompt > prompt_limit:
        return (False, None)
    if generated_limit is not None and estimated_generated > generated_limit:
        return (False, None)
    return None


def _retry_after_decision(headers: Mapping[str, Any]) -> tuple[bool, float | None] | None:
    retry_after = _extract_retry_after(headers)
    if retry_after is not None and retry_after > 0:
        return (True, retry_after)
    return None


def _classify_rate_limit_error(
    exc: Exception,
    kwargs: dict[str, Any],
    token_limit_policy: TokenLimitPolicy | None,
) -> tuple[bool, float | None]:
    """Classify a rate limit error and determine retry strategy.

    Returns:
        Tuple of (should_retry, delay_seconds).
        - should_retry: True if the error should be retried
        - delay_seconds: Custom delay to use (from Retry-After), or None to use default backoff
    """
    if not _is_rate_limit_error(exc):
        return (False, None)

    # Check configured token limits first - fail fast if exceeded.
    headers = _get_headers_from_exc(exc)
    decision = _token_limit_retry_decision(headers, kwargs, token_limit_policy)
    if decision is not None:
        return decision

    # Check for Retry-After header (preferred over static backoff)
    decision = _retry_after_decision(headers)
    if decision is not None:
        return decision

    # Generic rate limit - use default backoff
    return (True, None)


def call_with_rate_limit_backoff[T](
    fn: Callable[[], T],
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    jitter_fn: Callable[[float, float], float] = random.uniform,
    backoff_s: tuple[float, ...] = RATE_LIMIT_BACKOFF_S,
    _kwargs_for_classification: dict[str, Any] | None = None,
    token_limit_policy: TokenLimitPolicy | None = None,
) -> T:
    """Call ``fn`` with slow retries for provider 429/rate-limit failures."""
    backoff_iter = iter(backoff_s)

    while True:
        try:
            return fn()
        except Exception as exc:
            delay_s = _retry_delay_for_rate_limit(
                exc,
                kwargs_for_classification=_kwargs_for_classification,
                backoff_iter=backoff_iter,
                token_limit_policy=token_limit_policy,
            )
            sleep_fn(_jittered_delay(delay_s, jitter_fn))


def _retry_delay_for_rate_limit(
    exc: Exception,
    *,
    kwargs_for_classification: dict[str, Any] | None,
    backoff_iter: Iterator[float],
    token_limit_policy: TokenLimitPolicy | None,
) -> float:
    should_retry, custom_delay = _classify_rate_limit_error(
        exc,
        kwargs_for_classification or {},
        token_limit_policy,
    )
    if not should_retry:
        _raise_unretryable_provider_error(exc, kwargs_for_classification, token_limit_policy)
    if custom_delay is not None:
        return custom_delay
    return _next_backoff_delay(backoff_iter, exc)


def _raise_unretryable_provider_error(
    exc: Exception,
    kwargs_for_classification: dict[str, Any] | None,
    token_limit_policy: TokenLimitPolicy | None,
) -> None:
    if kwargs_for_classification is not None:
        _check_token_limits(exc, kwargs_for_classification, token_limit_policy)
    raise exc


def _next_backoff_delay(backoff_iter: Iterator[float], exc: Exception) -> float:
    try:
        return next(backoff_iter)
    except StopIteration:
        raise exc from None


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    client: Any
    name: str = ""
    token_limit_policy: TokenLimitPolicy | None = None

    def create_chat_completion(self, **kwargs: Any) -> Any:
        return call_with_rate_limit_backoff(
            lambda: self.client.chat.completions.create(**kwargs),
            _kwargs_for_classification=kwargs,
            token_limit_policy=self.token_limit_policy,
        )


@dataclass(frozen=True)
class _ToolFunctionCall:
    name: str
    arguments: str


@dataclass(frozen=True)
class _ToolCall:
    id: str
    function: _ToolFunctionCall
    type: str = "function"


@dataclass(frozen=True)
class _AssistantMessage:
    content: str | None
    tool_calls: list[_ToolCall]

    def model_dump(self, exclude_none: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {"role": "assistant"}
        if self.content is not None or not exclude_none:
            data["content"] = self.content
        if self.tool_calls:
            data["tool_calls"] = [
                {
                    "id": call.id,
                    "type": call.type,
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in self.tool_calls
            ]
        return data


@dataclass(frozen=True)
class _ClaudeCodeSettings:
    model_profile: str
    preset: str
    runner: Path
    timeout_seconds: float
    passthrough: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class ClaudeCodeProvider:
    """Provider adapter for the local invoke-claude skill wrapper."""

    settings: _ClaudeCodeSettings
    name: str = ""

    def create_chat_completion(self, **kwargs: Any) -> Any:
        terminal_tool = _terminal_tool_name(kwargs.get("tools"))
        if terminal_tool != "done":
            raise RuntimeError(
                "claude-code providers currently require a worker/researcher role "
                "that exposes the terminal `done` tool"
            )
        prompt = _claude_code_prompt(kwargs)
        output = _run_claude_code(
            settings=self.settings,
            prompt=prompt,
            model=str(kwargs.get("model") or "").strip(),
            cwd=_worktree_from_messages(kwargs.get("messages", [])),
        )
        message = _AssistantMessage(
            content=None,
            tool_calls=[
                _ToolCall(
                    id="call_claude_code_done",
                    function=_ToolFunctionCall(
                        name="done",
                        arguments=json.dumps({"summary": output}),
                    ),
                )
            ],
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            usage=None,
        )


def is_claude_code_provider_url(base_url: str) -> bool:
    return base_url.strip().lower().startswith(f"{CLAUDE_CODE_PROVIDER_SCHEME}:")


def _terminal_tool_name(tools: object) -> str:
    names = _tool_names(tools)
    if "done" in names:
        return "done"
    if "emit_plan" in names:
        return "emit_plan"
    return ""


def _tool_names(tools: object) -> set[str]:
    if not isinstance(tools, list):
        return set()
    names: set[str] = set()
    for item in tools:
        if not isinstance(item, dict):
            continue
        item_map = cast("dict[str, Any]", item)
        function = item_map.get("function")
        if not isinstance(function, dict):
            continue
        function_map = cast("dict[str, Any]", function)
        name = function_map.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _claude_code_prompt(kwargs: Mapping[str, Any]) -> str:
    messages = kwargs.get("messages", [])
    sections = [
        "DGOV provider adapter instructions:",
        "- You are running under Claude Code, not dgov's OpenAI-compatible tool-call transport.",
        "- Treat dgov tool instructions as workflow guidance; do not emit tool-call JSON.",
        "- Use Claude Code's allowed local tools according to the selected preset.",
        "- Finish with a concise governor-facing summary of edits, verification, or blockers.",
        "",
        "DGOV conversation:",
        _render_messages(messages),
    ]
    return "\n".join(sections).strip() + "\n"


def _render_messages(messages: object) -> str:
    if not isinstance(messages, list):
        return str(messages)
    rendered: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            rendered.append(str(message))
            continue
        message_map = cast("dict[str, Any]", message)
        role = str(message_map.get("role", "unknown"))
        content = _message_content_as_text(message_map.get("content", ""))
        rendered.append(f"## {role}\n{content}".rstrip())
        tool_calls = message_map.get("tool_calls")
        if tool_calls:
            rendered.append(f"tool_calls: {json.dumps(tool_calls, sort_keys=True)}")
    return "\n\n".join(rendered)


def _message_content_as_text(content: object) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, sort_keys=True)


def _worktree_from_messages(messages: object) -> Path | None:
    if not isinstance(messages, list):
        return None
    for message in messages:
        if not isinstance(message, dict):
            continue
        message_map = cast("dict[str, Any]", message)
        content = message_map.get("content")
        if not isinstance(content, str):
            continue
        path = _worktree_from_text(content)
        if path is not None:
            return path
    return None


def _worktree_from_text(text: str) -> Path | None:
    for pattern in _WORKTREE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(1).strip().rstrip(".")
        if value:
            return Path(value)
    return None


def _run_claude_code(
    *,
    settings: _ClaudeCodeSettings,
    prompt: str,
    model: str,
    cwd: Path | None,
) -> str:
    command = _claude_code_command(settings=settings, model=model, cwd=cwd)
    completed = subprocess.run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=settings.timeout_seconds + 5,
    )
    if completed.returncode != 0:
        raise RuntimeError(_claude_code_error(completed))
    return completed.stdout.strip()


def _claude_code_command(
    *,
    settings: _ClaudeCodeSettings,
    model: str,
    cwd: Path | None,
) -> list[str]:
    command = [
        sys.executable,
        str(settings.runner),
        "--preset",
        settings.preset,
        "--model-profile",
        settings.model_profile,
        "--output-format",
        "json",
        "--extract-result",
        "--timeout-seconds",
        _format_timeout(settings.timeout_seconds),
    ]
    if model:
        command.extend(["--model", model])
    if cwd is not None:
        command.extend(["--cwd", str(cwd)])
    for option, values in settings.passthrough.items():
        for value in values:
            command.extend([option, value])
    return command


def _format_timeout(timeout_seconds: float) -> str:
    if timeout_seconds.is_integer():
        return str(int(timeout_seconds))
    return str(timeout_seconds)


def _claude_code_error(completed: subprocess.CompletedProcess[str]) -> str:
    parts = [f"Claude Code provider exited with status {completed.returncode}."]
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    return "\n".join(parts)


def _claude_code_settings(base_url: str) -> _ClaudeCodeSettings:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query, keep_blank_values=False)
    return _ClaudeCodeSettings(
        model_profile=_query_one(query, "profile") or _profile_from_url(parsed),
        preset=_query_one(query, "preset") or _DEFAULT_CLAUDE_CODE_PRESET,
        runner=_runner_from_query(query),
        timeout_seconds=_timeout_from_query(query),
        passthrough=_passthrough_options(query),
    )


def _profile_from_url(parsed: Any) -> str:
    profile = parsed.netloc or parsed.path.strip("/")
    return unquote(profile) if profile else _DEFAULT_CLAUDE_CODE_PROFILE


def _runner_from_query(query: Mapping[str, list[str]]) -> Path:
    configured = _query_one(query, "runner") or os.environ.get("DGOV_CLAUDE_CODE_RUNNER", "")
    return Path(configured).expanduser() if configured else _DEFAULT_CLAUDE_CODE_RUNNER


def _timeout_from_query(query: Mapping[str, list[str]]) -> float:
    raw = _query_one(query, "timeout")
    if not raw:
        return _DEFAULT_CLAUDE_CODE_TIMEOUT_S
    try:
        timeout = float(raw)
    except ValueError as exc:
        raise ValueError("claude-code provider timeout must be numeric") from exc
    if timeout <= 0:
        raise ValueError("claude-code provider timeout must be > 0")
    return timeout


def _passthrough_options(query: Mapping[str, list[str]]) -> Mapping[str, tuple[str, ...]]:
    allowed = {
        "add_dir": "--add-dir",
        "max_budget_usd": "--max-budget-usd",
        "tools": "--tools",
        "disallowed_tools": "--disallowed-tools",
    }
    options: dict[str, tuple[str, ...]] = {}
    for query_key, option in allowed.items():
        values = tuple(value for value in query.get(query_key, []) if value)
        if values:
            options[option] = values
    return options


def _query_one(query: Mapping[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    return values[-1].strip() if values else ""


def _token_limit_policy_from_config(
    *,
    label: str,
    prompt_header: str,
    generated_header: str,
) -> TokenLimitPolicy | None:
    prompt_header = prompt_header.strip()
    generated_header = generated_header.strip()
    label = label.strip()
    if not prompt_header and not generated_header:
        return None
    return TokenLimitPolicy(
        label=label or "configured provider token limit",
        prompt_header=prompt_header,
        generated_header=generated_header,
    )


def create_provider(
    *,
    base_url: str,
    api_key: str,
    name: str = "",
    token_limit_label: str = "",
    prompt_token_limit_header: str = "",
    generated_token_limit_header: str = "",
) -> OpenAICompatibleProvider | ClaudeCodeProvider:
    if is_claude_code_provider_url(base_url):
        return ClaudeCodeProvider(_claude_code_settings(base_url), name=name)
    client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0)
    token_limit_policy = _token_limit_policy_from_config(
        label=token_limit_label,
        prompt_header=prompt_token_limit_header,
        generated_header=generated_token_limit_header,
    )
    return OpenAICompatibleProvider(client, name=name, token_limit_policy=token_limit_policy)
