"""Structured span and tool-trace observability for dgov.

Two tables:
- spans: one row per lifecycle phase per pane (dispatch/wait/review/merge/close/retry/escalate)
- tool_traces: one row per tool call / reasoning step, ingested from pi transcripts

Design: wide table with typed columns (no JSON blobs for queryable data),
append-only (INSERT to open, one UPDATE to close), zero NULLs.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SPAN_KINDS = frozenset({"dispatch", "wait", "review", "merge", "close", "retry", "escalate"})
_OUTCOMES = frozenset({"pending", "success", "failure", "skipped"})
_MAX_TOOL_RESULT = 2000

CREATE_SPANS_SQL = """\
CREATE TABLE IF NOT EXISTS spans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        TEXT NOT NULL,
    span_kind       TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    ended_at        TEXT NOT NULL DEFAULT '',
    duration_ms     REAL NOT NULL DEFAULT -1,
    outcome         TEXT NOT NULL DEFAULT 'pending',
    agent           TEXT NOT NULL DEFAULT '',
    prompt_hash     TEXT NOT NULL DEFAULT '',
    base_sha        TEXT NOT NULL DEFAULT '',
    wait_method     TEXT NOT NULL DEFAULT '',
    verdict         TEXT NOT NULL DEFAULT '',
    commit_count    INTEGER NOT NULL DEFAULT 0,
    tests_passed    INTEGER NOT NULL DEFAULT -1,
    stale_files     TEXT NOT NULL DEFAULT '[]',
    files_changed   INTEGER NOT NULL DEFAULT 0,
    merge_strategy  TEXT NOT NULL DEFAULT '',
    transcript_captured INTEGER NOT NULL DEFAULT 0,
    from_agent      TEXT NOT NULL DEFAULT '',
    to_agent        TEXT NOT NULL DEFAULT '',
    route           TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT ''
)"""

CREATE_SPANS_IDX_TRACE = "CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id)"
CREATE_SPANS_IDX_KIND = (
    "CREATE INDEX IF NOT EXISTS idx_spans_kind_outcome ON spans(span_kind, outcome)"
)

CREATE_TOOL_TRACES_SQL = """\
CREATE TABLE IF NOT EXISTS tool_traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id        TEXT NOT NULL,
    seq             INTEGER NOT NULL,
    ts              TEXT NOT NULL,
    role            TEXT NOT NULL,
    action_type     TEXT NOT NULL,
    tool_name       TEXT NOT NULL DEFAULT '',
    tool_args       TEXT NOT NULL DEFAULT '{}',
    tool_result     TEXT NOT NULL DEFAULT '',
    tool_status     TEXT NOT NULL DEFAULT '',
    thinking        TEXT NOT NULL DEFAULT '',
    tokens_in       INTEGER NOT NULL DEFAULT 0,
    tokens_out      INTEGER NOT NULL DEFAULT 0,
    provider        TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    UNIQUE(trace_id, seq)
)"""

CREATE_TOOL_TRACES_IDX = "CREATE INDEX IF NOT EXISTS idx_traces_trace ON tool_traces(trace_id)"

CREATE_PROMPTS_SQL = """\
CREATE TABLE IF NOT EXISTS prompts (
    prompt_hash     TEXT PRIMARY KEY,
    prompt_text     TEXT NOT NULL,
    created_at      TEXT NOT NULL
)"""

CREATE_LEDGER_SQL = """\
CREATE TABLE IF NOT EXISTS ledger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    category        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    severity        TEXT NOT NULL DEFAULT 'info',
    status          TEXT NOT NULL DEFAULT 'open',
    linked_slugs    TEXT NOT NULL DEFAULT '[]',
    tags            TEXT NOT NULL DEFAULT '[]',
    source          TEXT NOT NULL DEFAULT 'governor'
)"""

CREATE_LEDGER_IDX = "CREATE INDEX IF NOT EXISTS idx_ledger_category ON ledger(category, status)"

CREATE_ARCHIVED_PANES_SQL = """\
CREATE TABLE IF NOT EXISTS archived_panes (
    slug                TEXT NOT NULL,
    archived_at         TEXT NOT NULL,
    prompt              TEXT NOT NULL DEFAULT '',
    agent               TEXT NOT NULL DEFAULT '',
    project_root        TEXT NOT NULL DEFAULT '',
    worktree_path       TEXT NOT NULL DEFAULT '',
    branch_name         TEXT NOT NULL DEFAULT '',
    base_sha            TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL DEFAULT '',
    final_state         TEXT NOT NULL DEFAULT '',
    landing             INTEGER NOT NULL DEFAULT 0,
    file_claims         TEXT NOT NULL DEFAULT '[]',
    commit_message      TEXT DEFAULT NULL,
    circuit_breaker     INTEGER NOT NULL DEFAULT 0,
    retried_from        TEXT DEFAULT NULL,
    superseded_by       TEXT DEFAULT NULL,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 0,
    monitor_reason      TEXT DEFAULT NULL,
    last_checkpoint     TEXT DEFAULT NULL,
    crash_log           TEXT DEFAULT NULL,
    PRIMARY KEY (slug, archived_at)
)"""

CREATE_TRANSCRIPTS_SQL = """\
CREATE TABLE IF NOT EXISTS transcripts (
    trace_id        TEXT PRIMARY KEY,
    raw_jsonl       TEXT NOT NULL,
    line_count      INTEGER NOT NULL DEFAULT 0,
    ingested_at     TEXT NOT NULL
)"""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SpanKind(StrEnum):
    DISPATCH = "dispatch"
    WAIT = "wait"
    REVIEW = "review"
    MERGE = "merge"
    CLOSE = "close"
    RETRY = "retry"
    ESCALATE = "escalate"


class SpanOutcome(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolTraceRow:
    trace_id: str
    seq: int
    ts: str
    role: str
    action_type: str
    tool_name: str = ""
    tool_args: str = "{}"
    tool_result: str = ""
    tool_status: str = ""
    thinking: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    provider: str = ""
    model: str = ""


@dataclass
class PaneTrajectory:
    trace_id: str
    prompt: str
    agent: str
    spans: list[dict] = field(default_factory=list)
    tool_trace: list[dict] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        return self.spans[-1].get("outcome", "") if self.spans else ""

    @property
    def total_duration_ms(self) -> float:
        return sum(s.get("duration_ms", 0.0) for s in self.spans)


# ---------------------------------------------------------------------------
# Span API
# ---------------------------------------------------------------------------

# Column names that can be set via **payload in open/close
_SPAN_COLUMNS = {
    "agent",
    "prompt_hash",
    "base_sha",
    "wait_method",
    "verdict",
    "commit_count",
    "tests_passed",
    "stale_files",
    "files_changed",
    "merge_strategy",
    "transcript_captured",
    "from_agent",
    "to_agent",
    "route",
    "error",
}


def _compute_route_from_dispatch(agent: str, from_agent: str) -> str:
    """Compute canonical route identity for a dispatch span.

    The canonical route is the logical backend name that was actually routed to,
    not the role name (worker/supervisor/manager) or physical backend name.

    Rules:
    - If from_agent is set (backend chosen by router), use its logical name
    - Otherwise, use agent's logical name via physical_to_logical mapping
    - Returns empty string if neither is available

    This ensures route identity is consistent across role names and physical backends.
    """
    if not agent and not from_agent:
        return ""

    from dgov.router import physical_to_logical

    # Priority 1: use from_agent (backend explicitly routed to)
    if from_agent:
        logical = physical_to_logical(from_agent)
        if logical and logical != from_agent:
            return logical

    # Priority 2: use agent's logical name
    if agent:
        logical = physical_to_logical(agent)
        if logical and logical != agent:
            return logical

    # Fallback: return as-is (already a canonical name or no mapping available)
    return from_agent or agent or ""


def _get_db(session_root: str) -> sqlite3.Connection:
    from dgov.persistence import _get_db as _persist_db

    return _persist_db(session_root)


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

_LEDGER_CATEGORIES = frozenset(
    {
        "bug",
        "fix",
        "rule",
        "pattern",
        "debt",
        "capability",
        "decision",
        "missive",
    }
)
_LEDGER_SEVERITIES = frozenset({"info", "low", "medium", "high"})
_LEDGER_STATUSES = frozenset({"open", "fixed", "accepted", "wontfix"})


def ledger_add(
    session_root: str,
    category: str,
    summary: str,
    *,
    detail: str = "",
    severity: str = "info",
    status: str = "open",
    linked_slugs: list[str] | None = None,
    tags: list[str] | None = None,
    source: str = "governor",
) -> int:
    """Add a ledger entry. Returns the entry id."""
    if category not in _LEDGER_CATEGORIES:
        raise ValueError(f"Invalid category: {category}. Valid: {sorted(_LEDGER_CATEGORIES)}")
    if severity not in _LEDGER_SEVERITIES:
        raise ValueError(f"Invalid severity: {severity}. Valid: {sorted(_LEDGER_SEVERITIES)}")
    if status not in _LEDGER_STATUSES:
        raise ValueError(f"Invalid status: {status}. Valid: {sorted(_LEDGER_STATUSES)}")

    now = datetime.now(timezone.utc).isoformat()
    conn = _get_db(session_root)
    cur = conn.execute(
        "INSERT INTO ledger (ts, category, summary, detail, severity, status, "
        "linked_slugs, tags, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now,
            category,
            summary,
            detail,
            severity,
            status,
            json.dumps(linked_slugs or []),
            json.dumps(tags or []),
            source,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def ledger_update(session_root: str, entry_id: int, *, status: str) -> None:
    """Update ledger entry status."""
    if status not in _LEDGER_STATUSES:
        raise ValueError(f"Invalid status: {status}. Valid: {sorted(_LEDGER_STATUSES)}")
    conn = _get_db(session_root)
    conn.execute("UPDATE ledger SET status = ? WHERE id = ?", (status, entry_id))
    conn.commit()


def ledger_query(
    session_root: str,
    *,
    category: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query ledger entries. Returns newest first."""
    conn = _get_db(session_root)
    clauses: list[str] = []
    vals: list[object] = []
    if category:
        clauses.append("category = ?")
        vals.append(category)
    if status:
        clauses.append("status = ?")
        vals.append(status)
    if tag:
        clauses.append("tags LIKE ?")
        vals.append(f"%{tag}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    vals.append(limit)
    rows = conn.execute(f"SELECT * FROM ledger{where} ORDER BY ts DESC LIMIT ?", vals).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM ledger LIMIT 0").description]
    return [dict(zip(cols, row)) for row in rows]


def store_prompt(session_root: str, prompt: str) -> str:
    """Store prompt text keyed by hash. Returns the hash. Idempotent."""
    h = prompt_hash(prompt)
    now = datetime.now(timezone.utc).isoformat()
    conn = _get_db(session_root)
    conn.execute(
        "INSERT OR IGNORE INTO prompts (prompt_hash, prompt_text, created_at) VALUES (?, ?, ?)",
        (h, prompt, now),
    )
    conn.commit()
    return h


def get_prompt(session_root: str, phash: str) -> str:
    """Retrieve prompt text by hash. Returns empty string if not found."""
    conn = _get_db(session_root)
    row = conn.execute(
        "SELECT prompt_text FROM prompts WHERE prompt_hash = ?", (phash,)
    ).fetchone()
    return row[0] if row else ""


def archive_pane(session_root: str, pane: dict, crash_log: str = "") -> None:
    """Snapshot a pane record before deletion. Idempotent per (slug, archived_at)."""
    now = datetime.now(timezone.utc).isoformat()
    meta = pane.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    file_claims = pane.get("file_claims", meta.get("file_claims", []))
    if isinstance(file_claims, list):
        file_claims = json.dumps(file_claims)
    commit_message = pane.get("commit_message")
    conn = _get_db(session_root)
    conn.execute(
        "INSERT OR IGNORE INTO archived_panes "
        "(slug, archived_at, prompt, agent, project_root, worktree_path, "
        "branch_name, base_sha, created_at, final_state, "
        "landing, file_claims, commit_message, circuit_breaker, retried_from, "
        "superseded_by, retry_count, max_retries, monitor_reason, "
        "last_checkpoint, crash_log) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            pane.get("slug", ""),
            now,
            pane.get("prompt", ""),
            pane.get("agent", ""),
            pane.get("project_root", ""),
            pane.get("worktree_path", ""),
            pane.get("branch_name", ""),
            pane.get("base_sha", ""),
            pane.get("created_at", ""),
            pane.get("state", ""),
            int(bool(meta.get("landing", False))),
            file_claims,
            commit_message,
            int(bool(meta.get("circuit_breaker", False))),
            meta.get("retried_from"),
            meta.get("superseded_by"),
            int(meta.get("retry_count", 0) or 0),
            int(meta.get("max_retries", 0) or 0),
            meta.get("monitor_reason"),
            meta.get("last_checkpoint"),
            crash_log,
        ),
    )
    conn.commit()


def store_transcript(session_root: str, trace_id: str, raw_jsonl: str) -> None:
    """Store raw transcript JSONL in DB. Idempotent."""
    now = datetime.now(timezone.utc).isoformat()
    line_count = sum(1 for line in raw_jsonl.splitlines() if line.strip())
    conn = _get_db(session_root)
    conn.execute(
        "INSERT OR IGNORE INTO transcripts "
        "(trace_id, raw_jsonl, line_count, ingested_at) VALUES (?, ?, ?, ?)",
        (trace_id, raw_jsonl, line_count, now),
    )
    conn.commit()


def open_span(
    session_root: str,
    trace_id: str,
    kind: SpanKind | str,
    **payload: str | int | float,
) -> int:
    """INSERT a pending span. Returns the span row id."""
    kind_str = str(kind)
    if kind_str not in _SPAN_KINDS:
        raise ValueError(f"Invalid span kind: {kind_str}")

    now = datetime.now(timezone.utc).isoformat()
    cols = ["trace_id", "span_kind", "started_at"]
    vals: list[object] = [trace_id, kind_str, now]

    for key, val in payload.items():
        if key in _SPAN_COLUMNS:
            cols.append(key)
            vals.append(val)

    placeholders = ", ".join("?" for _ in vals)
    col_names = ", ".join(cols)

    conn = _get_db(session_root)
    cur = conn.execute(f"INSERT INTO spans ({col_names}) VALUES ({placeholders})", vals)
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def close_span(
    session_root: str,
    span_id: int,
    outcome: SpanOutcome | str,
    **payload: str | int | float,
) -> None:
    """UPDATE a pending span with outcome and payload. No-op if already closed."""
    outcome_str = str(outcome)
    if outcome_str not in _OUTCOMES:
        raise ValueError(f"Invalid outcome: {outcome_str}")

    now = datetime.now(timezone.utc).isoformat()
    conn = _get_db(session_root)

    # Get started_at for duration calculation
    row = conn.execute("SELECT started_at, outcome FROM spans WHERE id = ?", (span_id,)).fetchone()
    if row is None:
        logger.warning("close_span: span %d not found", span_id)
        return
    if row[1] != "pending":
        return  # already closed

    started_at = row[0]
    try:
        start_dt = datetime.fromisoformat(started_at)
        end_dt = datetime.fromisoformat(now)
        duration = (end_dt - start_dt).total_seconds() * 1000
    except (ValueError, TypeError):
        duration = -1

    sets = ["ended_at = ?", "duration_ms = ?", "outcome = ?"]
    vals: list[object] = [now, duration, outcome_str]

    for key, val in payload.items():
        if key in _SPAN_COLUMNS:
            sets.append(f"{key} = ?")
            vals.append(val)

    vals.append(span_id)
    conn.execute(f"UPDATE spans SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()


def get_spans(session_root: str, trace_id: str) -> list[dict]:
    """All spans for a trace, ordered by started_at."""
    conn = _get_db(session_root)
    cur = conn.execute("SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at", (trace_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def close_orphaned_spans(session_root: str, *, max_age_hours: float = 2.0) -> int:
    """Close pending spans older than max_age_hours with outcome='failure'.

    Returns count of orphaned spans closed.
    """
    conn = _get_db(session_root)
    cutoff = datetime.now(timezone.utc)
    rows = conn.execute("SELECT id, started_at FROM spans WHERE outcome = 'pending'").fetchall()
    closed = 0
    now_iso = cutoff.isoformat()
    for span_id, started_at in rows:
        try:
            start_dt = datetime.fromisoformat(started_at)
            age_hours = (cutoff - start_dt).total_seconds() / 3600
            if age_hours >= max_age_hours:
                duration = age_hours * 3600 * 1000  # ms
                conn.execute(
                    "UPDATE spans SET outcome = 'failure', ended_at = ?, "
                    "duration_ms = ?, error = 'orphaned span (never closed)' "
                    "WHERE id = ? AND outcome = 'pending'",
                    (now_iso, duration, span_id),
                )
                closed += 1
        except (ValueError, TypeError):
            continue
    if closed:
        conn.commit()
        logger.info("Closed %d orphaned spans older than %.1fh", closed, max_age_hours)
    return closed


# ---------------------------------------------------------------------------
# Transcript ingest
# ---------------------------------------------------------------------------


def pi_session_dir_for_worktree(worktree_path: str) -> Path | None:
    """Return the Pi session directory for a worker worktree."""
    if not worktree_path:
        return None
    session_dir_name = f"--{worktree_path.lstrip('/').replace('/', '-')}--"
    return Path.home() / ".pi" / "agent" / "sessions" / session_dir_name


def latest_pi_transcript_path(worktree_path: str) -> Path | None:
    """Return the newest Pi transcript file for a worker worktree."""
    session_dir = pi_session_dir_for_worktree(worktree_path)
    if session_dir is None or not session_dir.exists():
        return None
    try:
        jsonl_files = [path for path in session_dir.glob("*.jsonl") if path.is_file()]
    except OSError:
        return None
    if not jsonl_files:
        return None
    return max(jsonl_files, key=lambda path: path.stat().st_mtime)


def ingest_transcript(session_root: str, trace_id: str, transcript_path: str) -> int:
    """Parse a pi JSONL transcript and INSERT tool_trace rows. Returns row count."""
    path = Path(transcript_path)
    if not path.exists():
        logger.warning("ingest_transcript: %s not found", transcript_path)
        return 0

    rows: list[ToolTraceRow] = []
    seq = 0
    current_provider = ""
    current_model = ""

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        entry_type = entry.get("type", "")
        ts = entry.get("timestamp", "")

        if entry_type == "model_change":
            current_provider = entry.get("provider", current_provider)
            current_model = entry.get("modelId", current_model)
            continue

        if entry_type != "message":
            continue

        raw_msg = entry.get("message", entry)
        msg = raw_msg if isinstance(raw_msg, dict) else {}
        role = msg.get("role", "")
        content_items = msg.get("content", [])
        if isinstance(content_items, str):
            content_items = [{"type": "text", "text": content_items}]

        # Extract usage from message level
        usage = {}
        for item in content_items:
            if isinstance(item, dict) and "usage" in item:
                item_usage = item["usage"]
                usage = item_usage if isinstance(item_usage, dict) else {}
                break
        if not usage:
            msg_usage = msg.get("usage", {}) or {}
            usage = msg_usage if isinstance(msg_usage, dict) else {}

        tokens_in = usage.get("input", usage.get("inputTokens", 0)) or 0
        tokens_out = usage.get("output", usage.get("outputTokens", 0)) or 0

        for item in content_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type", "")

            if item_type == "thinking":
                seq += 1
                rows.append(
                    ToolTraceRow(
                        trace_id=trace_id,
                        seq=seq,
                        ts=ts,
                        role="assistant",
                        action_type="thinking",
                        thinking=item.get("thinking", ""),
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        provider=current_provider,
                        model=current_model,
                    )
                )
            elif item_type in ("toolCall", "tool_use"):
                seq += 1
                args = item.get("arguments", item.get("input", {}))
                rows.append(
                    ToolTraceRow(
                        trace_id=trace_id,
                        seq=seq,
                        ts=ts,
                        role="assistant",
                        action_type="tool_call",
                        tool_name=item.get("name", ""),
                        tool_args=json.dumps(args, default=str)[:_MAX_TOOL_RESULT],
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        provider=current_provider,
                        model=current_model,
                    )
                )
            elif item_type == "text" and role == "assistant":
                text = item.get("text", "")
                if text.strip():
                    seq += 1
                    rows.append(
                        ToolTraceRow(
                            trace_id=trace_id,
                            seq=seq,
                            ts=ts,
                            role="assistant",
                            action_type="text",
                            tool_result=text[:_MAX_TOOL_RESULT],
                            tokens_in=tokens_in,
                            tokens_out=tokens_out,
                            provider=current_provider,
                            model=current_model,
                        )
                    )
            elif item_type == "text" and role == "toolResult":
                seq += 1
                text = item.get("text", "")
                status = "error" if entry.get("isError") else "success"
                rows.append(
                    ToolTraceRow(
                        trace_id=trace_id,
                        seq=seq,
                        ts=ts,
                        role="tool_result",
                        action_type="tool_result",
                        tool_result=text[:_MAX_TOOL_RESULT],
                        tool_status=status,
                        provider=current_provider,
                        model=current_model,
                    )
                )

    if not rows:
        return 0

    conn = _get_db(session_root)
    conn.executemany(
        "INSERT OR IGNORE INTO tool_traces "
        "(trace_id, seq, ts, role, action_type, tool_name, tool_args, "
        "tool_result, tool_status, thinking, tokens_in, tokens_out, "
        "provider, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                r.trace_id,
                r.seq,
                r.ts,
                r.role,
                r.action_type,
                r.tool_name,
                r.tool_args,
                r.tool_result,
                r.tool_status,
                r.thinking,
                r.tokens_in,
                r.tokens_out,
                r.provider,
                r.model,
            )
            for r in rows
        ],
    )
    conn.commit()
    return len(rows)


def get_tool_trace(session_root: str, trace_id: str) -> list[dict]:
    """All tool trace rows for a trace, ordered by seq."""
    conn = _get_db(session_root)
    cur = conn.execute("SELECT * FROM tool_traces WHERE trace_id = ? ORDER BY seq", (trace_id,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_trajectory(session_root: str, trace_id: str) -> dict:
    """Build the full trajectory dict for one pane."""
    spans = get_spans(session_root, trace_id)
    tool_trace = get_tool_trace(session_root, trace_id)

    # Determine overall outcome from spans
    outcomes = [s["outcome"] for s in spans]
    if "failure" in outcomes:
        outcome = "failure"
    elif all(o == "success" for o in outcomes if o != "skipped"):
        outcome = "success"
    elif "pending" in outcomes:
        outcome = "pending"
    else:
        outcome = "mixed"

    total_ms = sum(s["duration_ms"] for s in spans if s["duration_ms"] > 0)

    # Get agent from dispatch span
    dispatch = next((s for s in spans if s["span_kind"] == "dispatch"), None)
    agent = dispatch["agent"] if dispatch else ""

    # Get prompt: try prompts table first, fall back to pane record
    prompt = ""
    if dispatch and dispatch.get("prompt_hash"):
        prompt = get_prompt(session_root, dispatch["prompt_hash"])
    if not prompt:
        try:
            from dgov.persistence import get_pane

            pane = get_pane(session_root, trace_id)
            if pane:
                prompt = pane.get("prompt", "")
        except Exception:
            pass

    return {
        "trace_id": trace_id,
        "prompt": prompt,
        "agent": agent,
        "spans": spans,
        "tool_trace": tool_trace,
        "outcome": outcome,
        "total_duration_ms": total_ms,
    }


def export_all_trajectories(session_root: str, *, outcome: str | None = None) -> list[dict]:
    """Export all trajectories, optionally filtered by outcome."""
    conn = _get_db(session_root)
    cur = conn.execute("SELECT DISTINCT trace_id FROM spans ORDER BY trace_id")
    trace_ids = [row[0] for row in cur.fetchall()]

    trajectories = []
    for tid in trace_ids:
        traj = export_trajectory(session_root, tid)
        if outcome is None or traj["outcome"] == outcome:
            trajectories.append(traj)
    return trajectories


def trajectory_to_training_messages(trajectory: dict) -> list[dict]:
    """Convert a pane trajectory into OpenAI chat-format training messages."""
    prompt = trajectory.get("prompt", "")
    tool_trace = trajectory.get("tool_trace", [])
    outcome = trajectory.get("outcome", "unknown")

    if not prompt or not tool_trace:
        return []

    system_msg = (
        "You are a coding agent that uses tools to complete tasks. "
        "You have access to: Read, Edit, Write, Bash, Glob, Grep. "
        f"(Task outcome: {outcome})"
    )
    messages: list[dict] = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": prompt},
    ]

    call_counter = 0
    for item in tool_trace:
        action = item.get("action_type", "")
        if action == "thinking":
            continue
        elif action == "tool_call":
            call_counter += 1
            call_id = f"call_{call_counter:03d}"
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": item.get("tool_name", ""),
                                "arguments": item.get("tool_args", "{}"),
                            },
                        }
                    ],
                }
            )
        elif action == "tool_result":
            call_id = f"call_{call_counter:03d}"
            messages.append(
                {
                    "role": "tool",
                    "content": item.get("tool_result", ""),
                    "tool_call_id": call_id,
                }
            )
        elif action == "text" and item.get("role") == "assistant":
            text = item.get("tool_result", "")
            if text.strip():
                messages.append({"role": "assistant", "content": text})

    return messages


def export_training_jsonl(
    session_root: str,
    *,
    outcome: str | None = None,
    min_tool_calls: int = 1,
) -> list[dict]:
    """Export all trajectories as training examples."""
    trajectories = export_all_trajectories(session_root, outcome=outcome)
    examples = []
    for traj in trajectories:
        messages = trajectory_to_training_messages(traj)
        if not messages:
            continue
        tool_calls = [m for m in messages if m.get("tool_calls")]
        if len(tool_calls) < min_tool_calls:
            continue
        examples.append(
            {
                "messages": messages,
                "metadata": {
                    "trace_id": traj["trace_id"],
                    "agent": traj["agent"],
                    "outcome": traj["outcome"],
                    "total_duration_ms": traj["total_duration_ms"],
                    "tool_call_count": len(tool_calls),
                },
            }
        )
    return examples


def backfill_empty_routes(session_root: str) -> int:
    """Backfill route column for existing spans with empty route.

    Computes canonical route identity from agent/from_agent fields for rows
    where route is still empty. Returns count of rows updated.
    """
    from datetime import datetime, timezone

    conn = _get_db(session_root)

    # Find spans with empty routes and non-empty agent or from_agent
    cursor = conn.execute(
        "SELECT id, agent, from_agent FROM spans "
        "WHERE route = '' AND (agent <> '' OR from_agent <> '')"
    )
    rows = cursor.fetchall()

    if not rows:
        return 0

    updated = 0
    now = datetime.now(timezone.utc).isoformat()

    for span_id, agent, from_agent in rows:
        route = _compute_route_from_dispatch(agent, from_agent)
        if route:
            conn.execute(
                "UPDATE spans SET route = ?, ended_at = ? WHERE id = ?",
                (route, now, span_id),
            )
            updated += 1

    if updated > 0:
        conn.commit()
        logger.info("Backfilled route identity for %d spans", updated)

    return updated


def agent_reliability_stats(
    session_root: str,
    *,
    min_dispatches: int = 3,
) -> dict[str, dict]:
    """Compute per-agent reliability metrics from spans.

    Returns {agent_name: {pass_rate, dispatch_count, review_count,
    retry_count, avg_wait_ms, avg_review_ms, last_seen}}.
    Only includes agents with >= min_dispatches dispatch spans.

    Prefs canonical route identity over alias reconstruction:
    - Uses typed `route` column when available (most reliable)
    - Falls back to agent/from_agent only for legacy rows with empty route
    """
    from dgov.router import physical_to_logical

    conn = _get_db(session_root)

    def compute_route_from_aliases(agent: str, backend_agent: str = "") -> str:
        """Compute route when typed route field is empty."""
        if backend_agent:
            backend_logical = physical_to_logical(backend_agent)
            if backend_logical != backend_agent:
                return backend_logical
        return physical_to_logical(agent) if agent else agent

    # Priority 1: use typed route column (backfilled or new rows)
    # Count dispatches per canonical route from route column first
    dispatch_rows_with_route = conn.execute(
        "SELECT DISTINCT(route) FROM spans WHERE span_kind = 'dispatch' AND route != ''"
    ).fetchall()

    # Filter to routes with enough dispatch counts
    dispatch_counts: dict[str, int] = {}
    for (route,) in dispatch_rows_with_route:
        count = conn.execute(
            "SELECT COUNT(*) FROM spans WHERE span_kind = 'dispatch' AND route = ?",
            (route,),
        ).fetchone()[0]
        if count >= min_dispatches:
            dispatch_counts[route] = count

    # Priority 2: legacy rows with empty route need reconstruction from agent/from_agent
    dispatch_rows_legacy = conn.execute(
        "SELECT agent, from_agent FROM spans WHERE span_kind = 'dispatch' AND route = ''"
    ).fetchall()

    for agent, backend_agent in dispatch_rows_legacy:
        logical_route = compute_route_from_aliases(agent, backend_agent)
        if not logical_route:
            continue
        # Add to counts (may overlap with typed routes - we merge by canonical identity)
        dispatch_counts[logical_route] = dispatch_counts.get(logical_route, 0) + 1

    # Filter to agents with enough data
    qualifying = {a for a, c in dispatch_counts.items() if c >= min_dispatches}
    if not qualifying:
        return {}

    # Review stats: prefer typed route over alias reconstruction
    # First: review spans with non-empty route (new rows)
    review_rows_typed = conn.execute(
        "SELECT route, verdict, COUNT(*) FROM spans "
        "WHERE span_kind = 'review' AND route != '' "
        "GROUP BY route, verdict"
    ).fetchall()

    # Legacy: review spans with empty route need reconstruction from agent
    review_rows_legacy = conn.execute(
        "SELECT agent, verdict, COUNT(*) FROM spans "
        "WHERE span_kind = 'review' AND route = '' AND agent != '' "
        "GROUP BY agent, verdict"
    ).fetchall()

    # Merge: use typed rows when available, fall back to reconstructed for legacy
    review_rows_typed_dict: dict[str, dict[str, int]] = {}
    for route, verdict, count in review_rows_typed:
        if route not in review_rows_typed_dict:
            review_rows_typed_dict[route] = {}
        review_rows_typed_dict[route][verdict] = (
            review_rows_typed_dict[route].get(verdict, 0) + count
        )

    # Add legacy rows (reconstructed from agent names)
    review_rows_legacy_dict: dict[str, dict[str, int]] = {}
    for agent, verdict, count in review_rows_legacy:
        logical_route = compute_route_from_aliases(agent, "")
        if not logical_route or logical_route not in qualifying:
            continue
        if logical_route not in review_rows_legacy_dict:
            review_rows_legacy_dict[logical_route] = {}
        review_rows_legacy_dict[logical_route][verdict] = (
            review_rows_legacy_dict[logical_route].get(verdict, 0) + count
        )

    # Retry counts - prefer typed route from to_agent
    retry_rows_typed = conn.execute(
        "SELECT route, COUNT(*) FROM spans "
        "WHERE span_kind = 'retry' AND route != '' "
        "GROUP BY route"
    ).fetchall()
    retry_counts: dict[str, int] = {route: count for route, count in retry_rows_typed}

    # Legacy retry rows with empty route need reconstruction from to_agent
    retry_rows_legacy = conn.execute(
        "SELECT to_agent, COUNT(*) FROM spans "
        "WHERE span_kind = 'retry' AND route = '' AND to_agent != '' "
        "GROUP BY to_agent"
    ).fetchall()
    for agent, count in retry_rows_legacy:
        logical_route = compute_route_from_aliases(agent, "")
        if logical_route in qualifying:
            retry_counts[logical_route] = retry_counts.get(logical_route, 0) + count

    # Average durations - prefer typed route over agent names
    duration_rows_typed = conn.execute(
        "SELECT route, span_kind, AVG(duration_ms) FROM spans "
        "WHERE route != '' AND duration_ms > 0 "
        "AND span_kind IN ('wait', 'review') "
        "GROUP BY route, span_kind"
    ).fetchall()

    # Legacy durations need reconstruction from agent
    duration_rows_legacy = conn.execute(
        "SELECT agent, span_kind, AVG(duration_ms) FROM spans "
        "WHERE agent != '' AND route = '' AND duration_ms > 0 "
        "AND span_kind IN ('wait', 'review') "
        "GROUP BY agent, span_kind"
    ).fetchall()

    # Merge typed and legacy durations
    duration_rows: dict[str, dict[str, float]] = {}
    for route, kind, avg_ms in duration_rows_typed:
        if route not in duration_rows:
            duration_rows[route] = {"wait": 0.0, "review": 0.0}
        duration_rows[route][kind] = avg_ms or 0.0

    for agent, kind, avg_ms in duration_rows_legacy:
        logical_route = compute_route_from_aliases(agent, "")
        if logical_route in qualifying and logical_route not in duration_rows:
            duration_rows[logical_route] = {"wait": 0.0, "review": 0.0}
        if logical_route in qualifying and kind in duration_rows.get(logical_route, {}):
            duration_rows[logical_route][kind] = avg_ms or 0.0

    # Last seen - prefer typed route over agent reconstruction
    last_seen_typed = conn.execute(
        "SELECT route, MAX(started_at) FROM spans WHERE route != '' GROUP BY route"
    ).fetchall()

    # Legacy last seen needs reconstruction from agent/from_agent
    last_seen_legacy = conn.execute(
        "SELECT agent, from_agent, MAX(started_at) FROM spans "
        "WHERE (agent != '' OR from_agent != '') AND route = '' "
        "GROUP BY agent, from_agent"
    ).fetchall()

    # Merge typed and legacy last seen
    last_seen: dict[str, str] = {route: ts for route, ts in last_seen_typed}
    for agent, backend_agent, ts in last_seen_legacy:
        logical_route = compute_route_from_aliases(agent, backend_agent)
        if logical_route and logical_route in qualifying:
            last_seen[logical_route] = max(last_seen.get(logical_route, ""), ts)

    # Build stats per agent using typed route as primary
    stats: dict[str, dict] = {}
    for agent in qualifying:
        # Aggregate review stats from both typed and legacy sources
        review_dict_typed = review_rows_typed_dict.get(agent, {})
        review_dict_legacy = review_rows_legacy_dict.get(agent, {})
        safe = review_dict_typed.get("safe", 0) + review_dict_legacy.get("safe", 0)
        # Total reviews = sum of ALL verdict counts (not just safe/unsafe/stuck)
        total_reviews = sum(review_dict_typed.values()) + sum(review_dict_legacy.values())

        pass_rate = safe / total_reviews if total_reviews > 0 else 0.0

        # Get duration stats from typed route dict or legacy reconstruction
        duration_dict = duration_rows.get(agent, {"wait": 0.0, "review": 0.0})
        avg_wait = duration_dict.get("wait", 0.0)
        avg_review = duration_dict.get("review", 0.0)

        stats[agent] = {
            "pass_rate": pass_rate,
            "dispatch_count": dispatch_counts.get(agent, 0),
            "review_count": total_reviews,
            "retry_count": retry_counts.get(agent, 0),
            "avg_wait_ms": avg_wait,
            "avg_review_ms": avg_review,
            "last_seen": last_seen.get(agent, ""),
        }

    return stats
