"""Run-origin provenance helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

DGOV_RUN_SOURCE_ENV = "DGOV_RUN_SOURCE"
DEFAULT_RUN_SOURCE = "manual"
_RUN_SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")


def normalize_run_source(value: str | None) -> str:
    """Normalize and validate a run-origin label."""
    source = (value or "").strip().lower() or DEFAULT_RUN_SOURCE
    if not _RUN_SOURCE_RE.fullmatch(source):
        raise ValueError(
            f"Invalid {DGOV_RUN_SOURCE_ENV}={value!r}; use 1-64 lowercase letters, "
            "digits, '.', '_', ':', or '-', starting with a letter or digit"
        )
    return source


def current_run_source(environ: Mapping[str, str] | None = None) -> str:
    """Return the current run-origin label from the environment."""
    env = os.environ if environ is None else environ
    return normalize_run_source(env.get(DGOV_RUN_SOURCE_ENV))


__all__ = [
    "DEFAULT_RUN_SOURCE",
    "DGOV_RUN_SOURCE_ENV",
    "current_run_source",
    "normalize_run_source",
]
