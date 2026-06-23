"""Tests for LLM provider retry backoff and Claude Code streaming."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from dgov.workers.provider import (
    OpenAICompatibleProvider,
    ProviderRateLimitError,
    TokenLimitPolicy,
    _claude_code_command,
    _estimate_request_tokens,
    _estimate_tokens_from_length,
    _extract_retry_after,
    _extract_token_limits,
    _get_header,
    _get_headers_from_exc,
    _jittered_delay,
    call_with_rate_limit_backoff,
    create_provider,
)

pytestmark = pytest.mark.unit

_TOKEN_POLICY = TokenLimitPolicy(
    label="test provider token limits",
    prompt_header="x-ratelimit-limit-tokens-prompt",
    generated_header="x-ratelimit-limit-tokens-generated",
)


class _RateLimitError(Exception):
    status_code = 429


class _ResponseRateLimitError(Exception):
    def __init__(self) -> None:
        super().__init__("provider throttled")
        self.response = SimpleNamespace(status_code=429)


class _ProviderRateLimitError(Exception):
    """Simulates configured token-limit 429 with TPM limit headers."""

    def __init__(
        self,
        prompt_limit: int | None = None,
        generated_limit: int | None = None,
        retry_after: str | None = None,
    ) -> None:
        super().__init__("rate limit exceeded")
        headers: dict[str, str] = {}
        if prompt_limit is not None:
            headers["X-Ratelimit-Limit-Tokens-Prompt"] = str(prompt_limit)
        if generated_limit is not None:
            headers["X-Ratelimit-Limit-Tokens-Generated"] = str(generated_limit)
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        # The provider returns headers on the response object
        self.response = SimpleNamespace(status_code=429, headers=headers)


class _DirectHeadersRateLimitError(Exception):
    """Simulates exception with headers directly on exception (not response)."""

    def __init__(self) -> None:
        super().__init__("rate limit")
        self.status_code = 429
        self.headers = {"retry-after": "42"}


# ---------------------------------------------------------------------------
# Fake Popen for Claude Code provider tests
# ---------------------------------------------------------------------------


class _FakeWritable:
    def write(self, data: str) -> None:
        pass

    def close(self) -> None:
        pass


class _FakePopen:
    """Minimal subprocess.Popen stand-in for provider tests."""

    def __init__(
        self,
        stdout_text: str = "",
        returncode: int = 0,
        stderr_text: str = "",
    ) -> None:
        self.returncode = returncode
        self.stdin: Any = _FakeWritable()
        self.stdout: Any = io.StringIO(stdout_text)
        self.stderr: Any = io.StringIO(stderr_text)

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


def _result_line(result: str, usage: dict[str, int] | None = None) -> str:
    event: dict[str, Any] = {"type": "result", "subtype": "success", "result": result}
    if usage is not None:
        event["usage"] = usage
    return json.dumps(event)


def _assistant_line(text: str | None = None, tool_uses: list[dict[str, Any]] | None = None) -> str:
    content: list[dict[str, Any]] = []
    if text is not None:
        content.append({"type": "text", "text": text})
    for tu in tool_uses or []:
        content.append({"type": "tool_use", **tu})
    return json.dumps({"type": "assistant", "message": {"content": content}})


# ---------------------------------------------------------------------------
# Rate-limit backoff tests (unchanged behaviour)
# ---------------------------------------------------------------------------


def test_rate_limit_backoff_uses_slow_schedule_with_jitter_hook() -> None:
    calls = 0
    sleeps: list[float] = []

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _RateLimitError("rate limited")
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=sleeps.append,
        jitter_fn=lambda _lo, _hi: 0.0,
    )

    assert result == "ok"
    assert calls == 3
    assert sleeps == [5.0, 30.0]


def test_rate_limit_backoff_exhausts_after_90_second_slot() -> None:
    sleeps: list[float] = []

    with pytest.raises(_RateLimitError):
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(_RateLimitError("too many requests")),
            sleep_fn=sleeps.append,
            jitter_fn=lambda _lo, _hi: 0.0,
        )

    assert sleeps == [5.0, 30.0, 90.0]


def test_rate_limit_backoff_does_not_retry_non_rate_limit_errors() -> None:
    sleeps: list[float] = []

    with pytest.raises(RuntimeError):
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            sleep_fn=sleeps.append,
        )

    assert sleeps == []


def test_rate_limit_detection_accepts_response_status_code() -> None:
    calls = 0
    error = _ResponseRateLimitError()

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=lambda _delay: None,
        jitter_fn=lambda _lo, _hi: 0.0,
    )

    assert result == "ok"
    assert calls == 2


def test_jittered_delay_applies_bounded_twenty_percent_jitter() -> None:
    assert _jittered_delay(10.0, lambda _lo, hi: hi) == pytest.approx(12.0)
    assert _jittered_delay(10.0, lambda lo, _hi: lo) == pytest.approx(8.0)


def test_generic_429_still_retries_on_5_30_90_schedule() -> None:
    """Generic 429 without configured token-limit headers uses the standard backoff."""
    sleeps: list[float] = []
    calls = 0

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise _RateLimitError("rate limited")
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=sleeps.append,
        jitter_fn=lambda _lo, _hi: 0.0,
    )

    assert result == "ok"
    assert calls == 4
    assert sleeps == [5.0, 30.0, 90.0]


def test_token_limit_headers_are_ignored_without_policy() -> None:
    sleeps: list[float] = []
    calls = 0

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _ProviderRateLimitError(prompt_limit=1, generated_limit=1)
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=sleeps.append,
        jitter_fn=lambda _lo, _hi: 0.0,
        _kwargs_for_classification={
            "messages": [{"role": "user", "content": "x" * 100}],
            "max_tokens": 50,
        },
    )

    assert result == "ok"
    assert calls == 2
    assert sleeps == [5.0]


def test_retry_after_header_controls_first_sleep() -> None:
    """Retry-After header value is used instead of the first static backoff slot."""
    sleeps: list[float] = []
    calls = 0

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _ProviderRateLimitError(retry_after="15")
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=sleeps.append,
        jitter_fn=lambda _lo, _hi: 0.0,
        _kwargs_for_classification={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert result == "ok"
    assert calls == 2
    # First sleep uses Retry-After value (15) with jitter applied
    assert sleeps[0] == pytest.approx(15.0)


def test_token_prompt_limit_below_estimated_request_size_fails_fast() -> None:
    """When prompt limit is below estimated tokens, fail immediately without retry."""
    sleeps: list[float] = []

    # Create a message that will estimate to more than 100 tokens
    # Using 400+ chars to exceed 100 tokens at 4 chars/token
    long_content = "x" * 400  # 100 tokens at 4 chars/token

    error = _ProviderRateLimitError(prompt_limit=50)

    with pytest.raises(ProviderRateLimitError) as exc_info:
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(error),
            sleep_fn=sleeps.append,
            jitter_fn=lambda _lo, _hi: 0.0,
            _kwargs_for_classification={"messages": [{"role": "user", "content": long_content}]},
            token_limit_policy=_TOKEN_POLICY,
        )

    # Should fail immediately with no sleep
    assert len(sleeps) == 0
    assert exc_info.value.limit_type == "prompt"
    assert exc_info.value.estimated_tokens >= 100
    assert exc_info.value.observed_limit == 50
    assert "Provider token limit exceeded" in str(exc_info.value)
    assert "Suggested actions:" in str(exc_info.value)


def test_token_generated_limit_below_max_tokens_fails_fast() -> None:
    """When generated limit is below max_tokens, fail immediately without retry."""
    sleeps: list[float] = []

    error = _ProviderRateLimitError(generated_limit=100)

    with pytest.raises(ProviderRateLimitError) as exc_info:
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(error),
            sleep_fn=sleeps.append,
            jitter_fn=lambda _lo, _hi: 0.0,
            _kwargs_for_classification={
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 500,  # Exceeds 100 limit
            },
            token_limit_policy=_TOKEN_POLICY,
        )

    # Should fail immediately with no sleep
    assert len(sleeps) == 0
    assert exc_info.value.limit_type == "generated"
    assert exc_info.value.estimated_tokens == 500
    assert exc_info.value.observed_limit == 100
    assert "Provider token limit exceeded" in str(exc_info.value)


def test_header_lookup_works_case_insensitively() -> None:
    """Headers are looked up case-insensitively from response.headers."""
    headers = {
        "X-Ratelimit-Limit-Tokens-Prompt": "1000",
        "x-ratelimit-limit-tokens-generated": "500",
        "RETRY-AFTER": "30",
        "Content-Type": "application/json",
    }

    assert _get_header(headers, "x-ratelimit-limit-tokens-prompt") == "1000"
    assert _get_header(headers, "X-RateLimit-Limit-Tokens-Prompt") == "1000"
    assert _get_header(headers, "x-ratelimit-limit-tokens-generated") == "500"
    assert _get_header(headers, "retry-after") == "30"
    assert _get_header(headers, "Retry-After") == "30"
    assert _get_header(headers, "nonexistent") is None


def test_extract_token_limits_parses_headers() -> None:
    """_extract_token_limits correctly parses provider token limit headers."""
    headers = {
        "X-Ratelimit-Limit-Tokens-Prompt": "1000",
        "x-ratelimit-limit-tokens-generated": "500",
    }

    prompt_limit, generated_limit = _extract_token_limits(headers, _TOKEN_POLICY)

    assert prompt_limit == 1000
    assert generated_limit == 500


def test_extract_token_limits_returns_none_for_missing() -> None:
    """_extract_token_limits returns None for missing headers."""
    headers: dict[str, str] = {}

    prompt_limit, generated_limit = _extract_token_limits(headers, _TOKEN_POLICY)

    assert prompt_limit is None
    assert generated_limit is None


def test_extract_retry_after_parses_header() -> None:
    """_extract_retry_after correctly parses Retry-After header."""
    assert _extract_retry_after({"retry-after": "30"}) == 30.0
    assert _extract_retry_after({"Retry-After": "15.5"}) == 15.5
    assert _extract_retry_after({}) is None
    assert _extract_retry_after({"retry-after": "invalid"}) is None


def test_get_headers_from_exc_extracts_from_response() -> None:
    """_get_headers_from_exc extracts headers from exc.response.headers."""
    error = _ProviderRateLimitError(prompt_limit=100)
    headers = _get_headers_from_exc(error)

    assert "X-Ratelimit-Limit-Tokens-Prompt" in headers
    assert headers["X-Ratelimit-Limit-Tokens-Prompt"] == "100"


def test_get_headers_from_exc_extracts_from_exc_directly() -> None:
    """_get_headers_from_exc extracts headers from exc.headers when response is absent."""
    error = _DirectHeadersRateLimitError()
    headers = _get_headers_from_exc(error)

    assert headers.get("retry-after") == "42"


def test_estimate_tokens_from_length_rounds_up() -> None:
    """_estimate_tokens_from_length rounds up to nearest token."""
    assert _estimate_tokens_from_length("") == 0
    assert _estimate_tokens_from_length("a") == 1  # 1 char -> 1 token (rounded up)
    assert _estimate_tokens_from_length("abcd") == 1  # 4 chars -> 1 token
    assert _estimate_tokens_from_length("abcde") == 2  # 5 chars -> 2 tokens (rounded up)
    assert _estimate_tokens_from_length("x" * 100) == 25  # 100 chars -> 25 tokens
    assert _estimate_tokens_from_length("x" * 99) == 25  # 99 chars -> 25 tokens (rounded up)


def test_estimate_request_tokens_from_messages() -> None:
    """_estimate_request_tokens estimates tokens from message content."""
    kwargs = {
        "messages": [
            {"role": "system", "content": "x" * 40},  # 10 tokens
            {"role": "user", "content": "y" * 80},  # 20 tokens
        ]
    }

    prompt_tokens, generated_tokens = _estimate_request_tokens(kwargs)

    assert prompt_tokens == 30  # 10 + 20
    assert generated_tokens == 0  # No max_tokens


def test_estimate_request_tokens_with_tools() -> None:
    """_estimate_request_tokens includes tool schema in estimation."""
    tools = [
        {
            "type": "function",
            "function": {
                "name": "test",
                "description": "A test function",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    kwargs = {
        "messages": [{"role": "user", "content": "test"}],
        "tools": tools,
    }

    prompt_tokens, generated_tokens = _estimate_request_tokens(kwargs)

    # Should include both message content and tools JSON
    assert prompt_tokens > 10  # Tools JSON adds significant tokens
    assert generated_tokens == 0


def test_estimate_request_tokens_with_max_tokens() -> None:
    """_estimate_request_tokens extracts max_tokens as generated estimate."""
    kwargs = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 256,
    }

    _prompt_tokens, generated_tokens = _estimate_request_tokens(kwargs)

    assert generated_tokens == 256


def test_estimate_request_tokens_with_tool_calls_in_messages() -> None:
    """_estimate_request_tokens estimates tool_calls content in message history."""
    tool_call = {"id": "1", "type": "function", "function": {"name": "test", "arguments": "{}"}}
    kwargs = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [tool_call],
            }
        ]
    }

    prompt_tokens, _generated_tokens = _estimate_request_tokens(kwargs)

    assert prompt_tokens > 0  # Should count tool_calls JSON


def test_provider_create_chat_completion_passes_kwargs_for_classification() -> None:
    mock_client = MagicMock()
    provider = OpenAICompatibleProvider(mock_client, token_limit_policy=_TOKEN_POLICY)
    error = _ProviderRateLimitError(prompt_limit=10)
    mock_client.chat.completions.create.side_effect = error

    with pytest.raises(ProviderRateLimitError):
        provider.create_chat_completion(
            messages=[{"role": "user", "content": "x" * 100}],  # Will estimate > 10 tokens
            max_tokens=100,
        )

    mock_client.chat.completions.create.assert_called_once_with(
        messages=[{"role": "user", "content": "x" * 100}],
        max_tokens=100,
    )


def test_token_limit_exceeded_does_not_retry_other_limits() -> None:
    """When prompt limit exceeded but generated limit OK, only prompt error is raised."""
    sleeps: list[float] = []

    error = _ProviderRateLimitError(prompt_limit=10, generated_limit=1000)

    with pytest.raises(ProviderRateLimitError) as exc_info:
        call_with_rate_limit_backoff(
            lambda: (_ for _ in ()).throw(error),
            sleep_fn=sleeps.append,
            jitter_fn=lambda _lo, _hi: 0.0,
            _kwargs_for_classification={
                "messages": [{"role": "user", "content": "x" * 100}],  # ~25 tokens
                "max_tokens": 50,  # Under 1000 limit
            },
            token_limit_policy=_TOKEN_POLICY,
        )

    assert len(sleeps) == 0
    assert exc_info.value.limit_type == "prompt"


def test_token_limit_within_limits_allows_retry() -> None:
    """When request is within configured token limits, normal retry behavior occurs."""
    sleeps: list[float] = []
    calls = 0

    def _call() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _ProviderRateLimitError(prompt_limit=1000, generated_limit=1000)
        return "ok"

    result = call_with_rate_limit_backoff(
        _call,
        sleep_fn=sleeps.append,
        jitter_fn=lambda _lo, _hi: 0.0,
        _kwargs_for_classification={
            "messages": [{"role": "user", "content": "hi"}],  # Small prompt
            "max_tokens": 50,  # Small generation request
        },
        token_limit_policy=_TOKEN_POLICY,
    )

    assert result == "ok"
    assert calls == 2
    assert len(sleeps) == 1  # Retried once


# ---------------------------------------------------------------------------
# Claude Code provider tests (Popen-based)
# ---------------------------------------------------------------------------


def _make_popen(
    stream_events: list[dict[str, Any]],
    returncode: int = 0,
    stderr_text: str = "",
) -> _FakePopen:
    stdout_text = "\n".join(json.dumps(e) for e in stream_events) + "\n"
    return _FakePopen(stdout_text=stdout_text, returncode=returncode, stderr_text=stderr_text)


def test_claude_code_provider_requests_stream_json(tmp_path) -> None:
    """Command uses stream-json output format (not json + --extract-result)."""
    from dgov.workers.provider import _claude_code_settings

    settings = _claude_code_settings(
        f"claude-code://fast?preset=edit&runner={tmp_path / 'run.py'}"
    )
    command = _claude_code_command(settings=settings, model="haiku", cwd=None)

    assert "--output-format" in command
    idx = command.index("--output-format")
    assert command[idx + 1] == "stream-json"
    assert "--extract-result" not in command


def test_claude_code_provider_invokes_skill_runner(monkeypatch, tmp_path) -> None:
    runner = tmp_path / "run_claude_code.py"
    worktree = tmp_path / "repo"
    worktree.mkdir()
    captured: dict[str, object] = {}
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(runner))

    popen_instance = _make_popen([
        {"type": "result", "subtype": "success", "result": "changed x.py"}
    ])

    def _fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return popen_instance

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("dgov.workers.provider._emit_worker_event", lambda *_args: None)

    provider = create_provider(
        name="claude",
        base_url="claude-code://fast?preset=edit&timeout=12&max_turns=18",
        api_key="",
    )
    response = provider.create_chat_completion(
        model="haiku",
        messages=[
            {"role": "system", "content": f"Sandbox (a git worktree: {worktree})."},
            {"role": "user", "content": "touch x.py"},
        ],
        tools=[{"type": "function", "function": {"name": "done"}}],
    )

    command = cast(list[str], captured["command"])
    assert command[1] == str(runner)
    assert command[command.index("--model-profile") + 1] == "fast"
    assert command[command.index("--preset") + 1] == "edit"
    assert command[command.index("--model") + 1] == "haiku"
    assert command[command.index("--cwd") + 1] == str(worktree)
    assert command[command.index("--timeout-seconds") + 1] == "12"
    assert command[command.index("--max-turns") + 1] == "18"
    kwargs = cast(dict[str, Any], captured["kwargs"])
    assert kwargs.get("text") is True
    tool_call = response.choices[0].message.tool_calls[0]
    assert tool_call.function.name == "done"
    assert json.loads(tool_call.function.arguments) == {"summary": "changed x.py"}


def test_claude_code_provider_infers_edit_preset_for_worker_prompt(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(tmp_path / "run_claude_code.py"))
    captured: dict[str, object] = {}

    def _fake_popen(command, **_kwargs):
        captured["command"] = command
        return _make_popen([{"type": "result", "subtype": "success", "result": "changed x.py"}])

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("dgov.workers.provider._emit_worker_event", lambda *_args: None)
    provider = create_provider(
        name="claude",
        base_url="claude-code://daily?max_turns=32",
        api_key="",
    )

    provider.create_chat_completion(
        model="sonnet",
        messages=[{"role": "system", "content": "[DGOV_WORKER_PROMPT_V1.2.0]"}],
        tools=[{"type": "function", "function": {"name": "done"}}],
    )

    command = cast(list[str], captured["command"])
    assert command[command.index("--preset") + 1] == "edit"
    assert command[command.index("--max-turns") + 1] == "32"


def test_claude_code_provider_infers_review_preset_for_researcher_prompt(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(tmp_path / "run_claude_code.py"))
    captured: dict[str, object] = {}

    def _fake_popen(command, **_kwargs):
        captured["command"] = command
        return _make_popen([{"type": "result", "subtype": "success", "result": "reviewed"}])

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("dgov.workers.provider._emit_worker_event", lambda *_args: None)
    provider = create_provider(name="claude", base_url="claude-code://daily", api_key="")

    provider.create_chat_completion(
        model="sonnet",
        messages=[{"role": "system", "content": "[DGOV_RESEARCHER_PROMPT_V1.4.0]"}],
        tools=[{"type": "function", "function": {"name": "done"}}],
    )

    command = cast(list[str], captured["command"])
    assert command[command.index("--preset") + 1] == "review"


def test_claude_code_provider_reports_runner_failure(monkeypatch, tmp_path) -> None:
    runner = tmp_path / "run_claude_code.py"
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(runner))

    def _fake_popen(command, **_kwargs):
        return _FakePopen(
            stdout_text="partial\n",
            returncode=2,
            stderr_text="failed",
        )

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("dgov.workers.provider._emit_worker_event", lambda *_args: None)
    provider = create_provider(name="claude", base_url="claude-code://daily", api_key="")

    with pytest.raises(RuntimeError, match="Claude Code provider exited with status 2"):
        provider.create_chat_completion(
            model="sonnet",
            messages=[{"role": "user", "content": "review"}],
            tools=[{"type": "function", "function": {"name": "done"}}],
        )


def test_claude_code_provider_synthesizes_emit_plan_tool_call(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(tmp_path / "run_claude_code.py"))
    captured: dict[str, object] = {}

    plan_output = {
        "name": "plan-for-local",
        "summary": "Do a local-model task.",
        "tasks": [
            {
                "slug": "edit",
                "summary": "Edit one file",
                "prompt": "Orient:\nRead.\n\nEdit:\n1. Change.\n\nVerify:\n- Check.",
                "commit_message": "Edit one file",
                "files": {"edit": ["src/example.py"]},
            }
        ],
    }

    def _fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _make_popen([
            {"type": "result", "subtype": "success", "result": json.dumps(plan_output)}
        ])

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("dgov.workers.provider._emit_worker_event", lambda *_args: None)
    provider = create_provider(name="claude", base_url="claude-code://daily", api_key="")

    response = provider.create_chat_completion(
        model="sonnet",
        messages=[{"role": "user", "content": "plan"}],
        tools=[{"type": "function", "function": {"name": "emit_plan"}}],
    )

    command = cast(list[str], captured["command"])
    assert command[command.index("--preset") + 1] == "plan"
    tool_call = response.choices[0].message.tool_calls[0]
    assert tool_call.function.name == "emit_plan"
    assert json.loads(tool_call.function.arguments) == plan_output


def test_claude_code_provider_rejects_invalid_emit_plan_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(tmp_path / "run_claude_code.py"))

    def _fake_popen(command, **_kwargs):
        return _make_popen([{"type": "result", "subtype": "success", "result": "not json"}])

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("dgov.workers.provider._emit_worker_event", lambda *_args: None)
    provider = create_provider(
        name="claude", base_url="claude-code://daily?preset=plan", api_key=""
    )

    with pytest.raises(RuntimeError, match="valid emit_plan JSON"):
        provider.create_chat_completion(
            model="sonnet",
            messages=[{"role": "user", "content": "plan"}],
            tools=[{"type": "function", "function": {"name": "emit_plan"}}],
        )


def test_claude_code_provider_emits_thought_events(monkeypatch, tmp_path) -> None:
    """Stream assistant text blocks are emitted as WorkerEvent('thought', ...)."""
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(tmp_path / "run_claude_code.py"))

    stream = [
        json.loads(_assistant_line(text="Thinking about the task.")),
        json.loads(_assistant_line(text="Now I will edit the file.")),
        {"type": "result", "subtype": "success", "result": "done"},
    ]

    def _fake_popen(command, **_kwargs):
        return _make_popen(stream)

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)

    emitted: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        "dgov.workers.provider._emit_worker_event",
        lambda event_type, content: emitted.append((event_type, content)),
    )

    provider = create_provider(name="claude", base_url="claude-code://daily", api_key="")
    provider.create_chat_completion(
        model="sonnet",
        messages=[{"role": "user", "content": "do work"}],
        tools=[{"type": "function", "function": {"name": "done"}}],
    )

    thought_events = [(t, c) for t, c in emitted if t == "thought"]
    assert len(thought_events) == 2
    assert thought_events[0][1] == "Thinking about the task."
    assert thought_events[1][1] == "Now I will edit the file."


def test_claude_code_provider_emits_call_events(monkeypatch, tmp_path) -> None:
    """Stream tool_use blocks are emitted as WorkerEvent('call', ...) with expected keys."""
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(tmp_path / "run_claude_code.py"))

    tool_use = {"id": "tu_1", "name": "read_file", "input": {"path": "src/foo.py"}}
    stream = [
        json.loads(_assistant_line(tool_uses=[tool_use])),
        {"type": "result", "subtype": "success", "result": "done"},
    ]

    def _fake_popen(command, **_kwargs):
        return _make_popen(stream)

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)

    emitted: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        "dgov.workers.provider._emit_worker_event",
        lambda event_type, content: emitted.append((event_type, content)),
    )

    provider = create_provider(name="claude", base_url="claude-code://daily", api_key="")
    provider.create_chat_completion(
        model="sonnet",
        messages=[{"role": "user", "content": "do work"}],
        tools=[{"type": "function", "function": {"name": "done"}}],
    )

    call_events = [(t, c) for t, c in emitted if t == "call"]
    assert len(call_events) == 1
    payload = call_events[0][1]
    assert payload["tool"] == "read_file"
    assert payload["args"] == {"path": "src/foo.py"}
    assert payload["role"] == "assistant"
    assert "turn_index" in payload
    assert "tool_index" in payload


def test_claude_code_provider_maps_usage_tokens(monkeypatch, tmp_path) -> None:
    """Result event usage maps to prompt/completion tokens on response.usage."""
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(tmp_path / "run_claude_code.py"))

    stream = [
        {
            "type": "result",
            "subtype": "success",
            "result": "all done",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 10,
            },
        }
    ]

    def _fake_popen(command, **_kwargs):
        return _make_popen(stream)

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("dgov.workers.provider._emit_worker_event", lambda *_args: None)

    provider = create_provider(name="claude", base_url="claude-code://daily", api_key="")
    response = provider.create_chat_completion(
        model="sonnet",
        messages=[{"role": "user", "content": "do work"}],
        tools=[{"type": "function", "function": {"name": "done"}}],
    )

    assert response.usage is not None
    # 100 input + 20 cache_read + 10 cache_creation = 130 prompt tokens
    assert response.usage.prompt_tokens == 130
    assert response.usage.completion_tokens == 50
    assert response.usage.total_tokens == 180


def test_claude_code_provider_reports_nonzero_exit_with_stderr(monkeypatch, tmp_path) -> None:
    """Non-zero exit includes both stdout and stderr in the error message."""
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(tmp_path / "run_claude_code.py"))

    def _fake_popen(command, **_kwargs):
        return _FakePopen(
            stdout_text='{"type": "system"}\n',
            returncode=1,
            stderr_text="API error: timeout",
        )

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("dgov.workers.provider._emit_worker_event", lambda *_args: None)
    provider = create_provider(name="claude", base_url="claude-code://daily", api_key="")

    with pytest.raises(RuntimeError) as exc_info:
        provider.create_chat_completion(
            model="sonnet",
            messages=[{"role": "user", "content": "work"}],
            tools=[{"type": "function", "function": {"name": "done"}}],
        )

    msg = str(exc_info.value)
    assert "exited with status 1" in msg
    assert "API error: timeout" in msg


def test_claude_code_provider_fallback_to_plain_stdout(monkeypatch, tmp_path) -> None:
    """If runner emits no result event, falls back to raw stdout lines as result."""
    monkeypatch.setenv("DGOV_CLAUDE_CODE_RUNNER", str(tmp_path / "run_claude_code.py"))

    def _fake_popen(command, **_kwargs):
        return _FakePopen(stdout_text="plain text output\n", returncode=0)

    monkeypatch.setattr("dgov.workers.provider.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("dgov.workers.provider._emit_worker_event", lambda *_args: None)
    provider = create_provider(name="claude", base_url="claude-code://daily", api_key="")

    response = provider.create_chat_completion(
        model="sonnet",
        messages=[{"role": "user", "content": "work"}],
        tools=[{"type": "function", "function": {"name": "done"}}],
    )

    tool_call = response.choices[0].message.tool_calls[0]
    assert json.loads(tool_call.function.arguments) == {"summary": "plain text output"}
