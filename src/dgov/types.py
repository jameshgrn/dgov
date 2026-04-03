"""Core type definitions for dgov — minimal dependencies version."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple


class PaneState(StrEnum):
    ACTIVE = "active"
    DONE = "done"
    FAILED = "failed"
    REVIEWED_PASS = "reviewed_pass"
    REVIEWED_FAIL = "reviewed_fail"
    MERGED = "merged"
    TIMED_OUT = "timed_out"
    SUPERSEDED = "superseded"
    CLOSED = "closed"
    ABANDONED = "abandoned"


class WorkerPhase(StrEnum):
    STARTING = "starting"
    WORKING = "working"
    TESTING = "testing"
    COMMITTING = "committing"
    DONE = "done"
    FAILED = "failed"
    IDLE = "idle"
    STUCK = "stuck"
    WAITING_INPUT = "waiting_input"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


class PaneInfo(NamedTuple):
    slug: str
    task_slug: str
    pane_id: str
    agent: str | None
    state: PaneState = PaneState.ACTIVE


@dataclass(frozen=True)
class WorkerObservation:
    slug: str
    phase: WorkerPhase
    summary: str
    alive: bool
    done: bool
    duration_s: int
    current_command: str = ""
    last_output: str = ""
    progress: dict | None = None
    exit_code: int | None = None


# -- ANSI stripping --

_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"  # CSI sequences
    r"|\x1b\].*?(?:\x07|\x1b\\)"  # OSC sequences
    r"|\x1bk.*?\x1b\\"  # tmux title-setting
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return _ANSI_RE.sub("", text)


# -- Signal extraction --

_SIGNAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?:Read|Reading)\s+(.+)"), "Reading {0}"),
    (re.compile(r"(?:Edit|Editing)\s+(.+)"), "Editing {0}"),
    (re.compile(r"(?:Write|Writing|Creating)\s+(.+)"), "Writing {0}"),
    (re.compile(r"(?:Running|Ran)\s+(ruff\b.*)"), "Linting: {0}"),
    (re.compile(r"(?:Running|Ran)\s+(pytest\b.*)"), "Testing: {0}"),
    (re.compile(r"(?:Running|Ran)\s+(uv\b.*)"), "Running: {0}"),
    (re.compile(r"(?:Running|Ran)\s+(git\b.*)"), "Git: {0}"),
    (re.compile(r"git add\s+(.+)"), "Staging: {0}"),
    (re.compile(r"git commit\s+(.*)"), "Committing"),
    (re.compile(r"(\d+)\s+passed"), "{0} tests passed"),
    (re.compile(r"All checks passed|no issues found", re.IGNORECASE), "Lint clean"),
    (re.compile(r"(\d+)\s+files?\s+changed"), "{0} files changed"),
]


def match_signal(line: str) -> str | None:
    """Try to match *line* against known signal patterns, return formatted string or None."""
    for pat, fmt in _SIGNAL_PATTERNS:
        m = pat.search(line)
        if m:
            groups = m.groups()
            if groups:
                formatted = fmt.format(*(g[:60] if g else "" for g in groups))
            else:
                formatted = fmt
            return formatted[:80]
    return None


# -- Phase computation --


def compute_phase(
    state: str,
    alive: bool,
    done: bool,
    duration_s: int,
    summary: str,
) -> WorkerPhase:
    """Derive a human-readable phase from worker state and summary."""
    if not alive and not done:
        return WorkerPhase.STUCK

    if done:
        return WorkerPhase.DONE

    s = summary.lower()
    if "test" in s or "pytest" in s:
        return WorkerPhase.TESTING
    if "commit" in s or "git commit" in s:
        return WorkerPhase.COMMITTING
    if "read" in s or "edit" in s or "write" in s:
        return WorkerPhase.WORKING

    if state == "active":
        return WorkerPhase.WORKING

    return WorkerPhase.UNKNOWN


# -- Summary extraction --


def extract_summary_from_log(log_text: str) -> str:
    """Extract a one-line summary of the current activity from agent logs."""
    if not log_text:
        return "starting..."

    lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
    if not lines:
        return "starting..."

    for line in reversed(lines):
        clean = _strip_ansi(line)
        sig = match_signal(clean)
        if sig:
            return sig

    return _strip_ansi(lines[-1])[:80]


# -- Noise filtering --

_NOISE_RE = [
    re.compile(r"^\s*$"),
    re.compile(r"^[\u2500-\u257f\u2580-\u259f\u2800-\u28ff\s]+$"),
    re.compile(r"(?i)(?:type your message|bypass permissions|MCP servers)"),
]


def is_noise_line(line: str) -> bool:
    """Return True if line is TUI chrome or noise."""
    return any(pat.search(line) for pat in _NOISE_RE)
