"""Optional LLM integration — only needed for AI commit messages.

If distributary is installed, delegates to distributary.llm.
Otherwise, returns None (caller falls back to static commit messages).
"""

from __future__ import annotations


def call_qwen(*args, **kwargs):
    try:
        from distributary.llm import call_qwen as _call_qwen

        return _call_qwen(*args, **kwargs)
    except ImportError:
        return None


def pick_gpu(*args, **kwargs):
    try:
        from distributary.llm import pick_gpu as _pick_gpu

        return _pick_gpu(*args, **kwargs)
    except ImportError:
        return None
