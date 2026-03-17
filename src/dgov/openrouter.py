# mission test marker
"""OpenRouter API client with local Qwen 4B fallback.

Lightweight HTTP client using only urllib.request (zero new deps).
Provides LLM completions for task classification and slug generation.

Fallback chain: OpenRouter -> local Qwen 4B -> caller-provided default.
"""

from __future__ import annotations

import json
import logging
import os
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/auth/key"
_OPENROUTER_TIMEOUT = 10
_DEFAULT_MODEL = "openrouter/hunter-alpha"
_REFERER = "https://github.com/jameshgrn/dgov"
_TITLE = "dgov"

# Local Qwen 4B settings (fallback)
_QWEN_4B_URL = "http://localhost:8082/v1/chat/completions"
_QWEN_4B_TIMEOUT = 5

# Session-level cache for free models list
_free_models_cache: list[dict] | None = None
_free_models_cache_time: float = 0
_FREE_MODELS_CACHE_TTL = 300  # 5 minutes


def _load_config() -> dict:
    """Load OpenRouter config from ~/.dgov/config.toml [openrouter] section."""
    config_path = Path.home() / ".dgov" / "config.toml"
    if not config_path.is_file():
        return {}
    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        return data.get("openrouter", {})
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError) as exc:
        logger.warning("Malformed TOML in %s: %s", config_path, exc)
        return {}


def _get_api_key() -> str | None:
    """Get API key: env var takes priority, then config.toml."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    config = _load_config()
    return config.get("api_key")


def _get_default_model() -> str:
    """Get default model from config or use built-in default."""
    config = _load_config()
    return config.get("default_model", _DEFAULT_MODEL)


def _openrouter_request(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 20,
    temperature: float = 0,
) -> dict:
    """Send a chat completion request to OpenRouter.

    Returns the parsed JSON response dict.
    Raises on failure (caller should catch).
    """

    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("No OpenRouter API key configured")

    body = json.dumps(
        {
            "model": model or _get_default_model(),
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    ).encode()

    req = urllib.request.Request(
        _OPENROUTER_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": _REFERER,
            "X-Title": _TITLE,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_OPENROUTER_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        # Handle 429 rate limit specifically
        if hasattr(exc, "code") and exc.code == 429:
            logger.warning("OpenRouter rate limited (429), falling back")
            raise RuntimeError("OpenRouter rate limited") from exc
        raise


def _qwen_4b_request(messages: list[dict], max_tokens: int = 20, temperature: float = 0) -> dict:
    """Send a request to local Qwen 4B on localhost.

    Returns the parsed JSON response dict.
    Raises RuntimeError on failure.
    """

    body = json.dumps(
        {
            "model": "qwen",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    ).encode()

    try:
        req = urllib.request.Request(
            _QWEN_4B_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_QWEN_4B_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        raise RuntimeError("Local Qwen 4B not reachable") from exc


def chat_completion(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 20,
    temperature: float = 0,
) -> dict:
    """Send a chat completion with fallback chain.

    Tries: OpenRouter -> local Qwen 4B.
    Returns the parsed JSON response dict.
    Raises RuntimeError if all providers fail.
    """
    # Try OpenRouter first
    try:
        return _openrouter_request(
            messages, model=model, max_tokens=max_tokens, temperature=temperature
        )
    except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        logger.debug("OpenRouter request failed, trying Qwen 4B fallback")

    # Try local Qwen 4B
    try:
        return _qwen_4b_request(messages, max_tokens=max_tokens, temperature=temperature)
    except (RuntimeError, OSError):
        logger.debug("Qwen 4B fallback also failed")

    raise RuntimeError("All LLM providers failed (OpenRouter + Qwen 4B)")


def chat_completion_local_first(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 20,
    temperature: float = 0,
) -> dict:
    """Send a chat completion with local-first fallback chain.

    Tries: local Qwen 4B -> OpenRouter.
    Returns the parsed JSON response dict.
    Raises RuntimeError if all providers fail.
    """
    # Try local Qwen 4B first (sub-100ms)
    try:
        return _qwen_4b_request(messages, max_tokens=max_tokens, temperature=temperature)
    except (RuntimeError, OSError):
        logger.debug("Qwen 4B local request failed, trying OpenRouter fallback")

    # Fall back to OpenRouter
    try:
        return _openrouter_request(
            messages, model=model, max_tokens=max_tokens, temperature=temperature
        )
    except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        logger.debug("OpenRouter fallback also failed")

    raise RuntimeError("All LLM providers failed (Qwen 4B + OpenRouter)")


def list_free_models() -> list[dict]:
    """Fetch and cache available free models from OpenRouter API.

    Returns list of dicts with 'id' and 'name' keys.
    Caches for the session (5 min TTL).
    """

    global _free_models_cache, _free_models_cache_time

    now = time.time()
    if _free_models_cache is not None and (now - _free_models_cache_time) < _FREE_MODELS_CACHE_TTL:
        return _free_models_cache

    api_key = _get_api_key()
    headers = {
        "HTTP-Referer": _REFERER,
        "X-Title": _TITLE,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        req = urllib.request.Request(_OPENROUTER_MODELS_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=_OPENROUTER_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        logger.warning("Failed to fetch free models: %s", exc)
        return []

    models = []
    for m in data.get("data", []):
        model_id = m.get("id", "")
        pricing = m.get("pricing", {})
        prompt_cost = str(pricing.get("prompt", "1"))
        completion_cost = str(pricing.get("completion", "1"))
        is_free = model_id.endswith(":free") or (prompt_cost == "0" and completion_cost == "0")
        if is_free:
            models.append(
                {
                    "id": model_id,
                    "name": m.get("name", model_id),
                    "context_length": m.get("context_length", 0),
                }
            )

    _free_models_cache = models
    _free_models_cache_time = now
    return models


def get_key_info() -> dict:
    """Fetch account info for the current API key.

    Returns dict with 'label', 'usage', 'limit', 'credits', 'rate_limit', etc.
    Returns empty dict with 'error' on failure.
    """
    api_key = _get_api_key()
    if not api_key:
        return {"error": "No API key configured"}

    req = urllib.request.Request(
        _OPENROUTER_KEY_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": _REFERER,
            "X-Title": _TITLE,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_OPENROUTER_TIMEOUT) as resp:
            return json.loads(resp.read()).get("data", {})
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        OSError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        return {"error": str(exc)}


def check_status() -> dict:
    """Verify API key is set and API is reachable, including account info.

    Returns dict with 'api_key_set', 'api_reachable', 'default_model',
    'account', 'error'.
    """

    api_key = _get_api_key()
    result: dict[str, object] = {
        "api_key_set": bool(api_key),
        "default_model": _get_default_model(),
        "api_reachable": False,
        "account": None,
        "error": None,
    }

    if not api_key:
        result["error"] = "No API key (set OPENROUTER_API_KEY or add to ~/.dgov/config.toml)"
        return result

    try:
        req = urllib.request.Request(
            _OPENROUTER_MODELS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": _REFERER,
                "X-Title": _TITLE,
            },
        )
        with urllib.request.urlopen(req, timeout=_OPENROUTER_TIMEOUT) as resp:
            if resp.status == 200:
                result["api_reachable"] = True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as exc:
        result["error"] = str(exc)

    # Fetch account/key info
    key_info = get_key_info()
    if "error" not in key_info:
        result["account"] = key_info

    return result
