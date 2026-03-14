"""OpenRouter API client with local Qwen 4B fallback.

Lightweight HTTP client using only urllib.request (zero new deps).
Provides LLM completions for task classification and slug generation.

Fallback chain: OpenRouter -> local Qwen 4B -> caller-provided default.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import tomllib
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_TIMEOUT = 10
_DEFAULT_MODEL = "meta-llama/llama-3.1-8b-instruct:free"
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
    except Exception:
        logger.debug("Failed to load config.toml")
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
    except Exception as exc:
        # Handle 429 rate limit specifically
        if hasattr(exc, "code") and exc.code == 429:
            logger.warning("OpenRouter rate limited (429), falling back")
            raise RuntimeError("OpenRouter rate limited") from exc
        raise


def _qwen_4b_request(messages: list[dict], max_tokens: int = 20, temperature: float = 0) -> dict:
    """Send a request to local Qwen 4B, trying localhost first then SSH tunnel.

    Returns the parsed JSON response dict.
    Raises on failure (caller should catch).
    """

    body = json.dumps(
        {
            "model": "qwen",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    ).encode()

    # Try 1: direct localhost (tunnel is up locally)
    try:
        req = urllib.request.Request(
            _QWEN_4B_URL,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_QWEN_4B_TIMEOUT) as resp:
            return json.loads(resp.read())
    except Exception:
        logger.debug("Qwen 4B direct request failed, trying SSH fallback")

    # Try 2: SSH to river and curl from there
    json_str = json.dumps(
        {
            "model": "qwen",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    )
    curl_cmd = (
        f"curl -s --max-time {_QWEN_4B_TIMEOUT} -X POST "
        f"-H 'Content-Type: application/json' "
        f"-d @- 'http://localhost:8082/v1/chat/completions' <<'__JSON__'\n{json_str}\n__JSON__"
    )
    script = f"ssh river 'bash -l' <<'HEREDOC'\n{curl_cmd}\nHEREDOC"
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=_QWEN_4B_TIMEOUT + 30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"SSH curl to river failed (exit {result.returncode})")
    return json.loads(result.stdout)


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
    except Exception:
        logger.debug("OpenRouter request failed, trying Qwen 4B fallback")

    # Try local Qwen 4B
    try:
        return _qwen_4b_request(messages, max_tokens=max_tokens, temperature=temperature)
    except Exception:
        logger.debug("Qwen 4B fallback also failed")

    raise RuntimeError("All LLM providers failed (OpenRouter + Qwen 4B)")


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

    req = urllib.request.Request(_OPENROUTER_MODELS_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=_OPENROUTER_TIMEOUT) as resp:
        data = json.loads(resp.read())

    models = []
    for m in data.get("data", []):
        model_id = m.get("id", "")
        if model_id.endswith(":free"):
            models.append({"id": model_id, "name": m.get("name", model_id)})

    _free_models_cache = models
    _free_models_cache_time = now
    return models


def check_status() -> dict:
    """Verify API key is set and API is reachable.

    Returns dict with 'api_key_set', 'api_reachable', 'default_model', 'error'.
    """

    api_key = _get_api_key()
    result = {
        "api_key_set": bool(api_key),
        "default_model": _get_default_model(),
        "api_reachable": False,
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
    except Exception as exc:
        result["error"] = str(exc)

    return result
