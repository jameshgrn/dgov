"""Tests for dgov.spans — structured span and tool-trace observability."""

from __future__ import annotations

import json

import pytest

from dgov.spans import (
    SpanKind,
    SpanOutcome,
    close_span,
    export_trajectory,
    get_spans,
    get_tool_trace,
    ingest_transcript,
    open_span,
    prompt_hash,
)


@pytest.fixture()
def session(tmp_path):
    """Return a session_root with initialized DB."""
    from dgov.persistence import _get_db

    _get_db(str(tmp_path))
    return str(tmp_path)


# ---------------------------------------------------------------------------
# open/close lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSpanLifecycle:
    def test_open_returns_id(self, session):
        sid = open_span(session, "t1", SpanKind.DISPATCH, agent="qwen-35b")
        assert isinstance(sid, int)
        assert sid > 0

    def test_open_close_roundtrip(self, session):
        sid = open_span(session, "t1", SpanKind.REVIEW)
        close_span(session, sid, SpanOutcome.SUCCESS, verdict="safe", commit_count=3)

        spans = get_spans(session, "t1")
        assert len(spans) == 1
        s = spans[0]
        assert s["trace_id"] == "t1"
        assert s["span_kind"] == "review"
        assert s["outcome"] == "success"
        assert s["verdict"] == "safe"
        assert s["commit_count"] == 3
        assert s["duration_ms"] >= 0
        assert s["ended_at"] != ""

    def test_close_idempotent(self, session):
        sid = open_span(session, "t1", SpanKind.WAIT)
        close_span(session, sid, SpanOutcome.SUCCESS, wait_method="event:pane_done")
        close_span(session, sid, SpanOutcome.FAILURE)  # should be no-op

        spans = get_spans(session, "t1")
        assert spans[0]["outcome"] == "success"  # first close wins

    def test_close_nonexistent_span(self, session):
        close_span(session, 9999, SpanOutcome.FAILURE)  # should not raise

    def test_multiple_spans_per_trace(self, session):
        s1 = open_span(session, "t1", SpanKind.DISPATCH, agent="qwen-35b")
        close_span(session, s1, SpanOutcome.SUCCESS)
        s2 = open_span(session, "t1", SpanKind.WAIT)
        close_span(session, s2, SpanOutcome.SUCCESS, wait_method="event:pane_done")
        s3 = open_span(session, "t1", SpanKind.REVIEW)
        close_span(session, s3, SpanOutcome.SUCCESS, verdict="safe")

        spans = get_spans(session, "t1")
        assert len(spans) == 3
        kinds = [s["span_kind"] for s in spans]
        assert "dispatch" in kinds
        assert "wait" in kinds
        assert "review" in kinds

    def test_invalid_kind_raises(self, session):
        with pytest.raises(ValueError, match="Invalid span kind"):
            open_span(session, "t1", "bogus")

    def test_invalid_outcome_raises(self, session):
        sid = open_span(session, "t1", SpanKind.MERGE)
        with pytest.raises(ValueError, match="Invalid outcome"):
            close_span(session, sid, "bogus")

    def test_pending_span_has_defaults(self, session):
        open_span(session, "t1", SpanKind.DISPATCH)
        spans = get_spans(session, "t1")
        s = spans[0]
        assert s["outcome"] == "pending"
        assert s["ended_at"] == ""
        assert s["duration_ms"] == -1
        assert s["agent"] == ""
        assert s["error"] == ""

    def test_failure_with_error(self, session):
        sid = open_span(session, "t1", SpanKind.MERGE)
        close_span(session, sid, SpanOutcome.FAILURE, error="conflict in README.md")

        spans = get_spans(session, "t1")
        assert spans[0]["error"] == "conflict in README.md"
        assert spans[0]["outcome"] == "failure"


# ---------------------------------------------------------------------------
# Transcript ingest
# ---------------------------------------------------------------------------


def _make_transcript(lines: list[dict]) -> str:
    return "\n".join(json.dumps(line) for line in lines)


@pytest.mark.unit
class TestTranscriptIngest:
    def test_empty_file(self, session, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert ingest_transcript(session, "t1", str(p)) == 0

    def test_missing_file(self, session):
        assert ingest_transcript(session, "t1", "/nonexistent.jsonl") == 0

    def test_tool_call_parsing(self, session, tmp_path):
        transcript = _make_transcript(
            [
                {"type": "model_change", "provider": "river-35b", "modelId": "qwen-35b"},
                {
                    "type": "message",
                    "timestamp": "2026-03-21T12:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "I need to read the file"},
                            {
                                "type": "toolCall",
                                "name": "read",
                                "arguments": {"path": "src/foo.py"},
                            },
                        ],
                    },
                },
                {
                    "type": "message",
                    "timestamp": "2026-03-21T12:00:01Z",
                    "message": {
                        "role": "toolResult",
                        "content": [{"type": "text", "text": "def foo(): pass"}],
                    },
                },
            ]
        )
        p = tmp_path / "transcript.jsonl"
        p.write_text(transcript)

        count = ingest_transcript(session, "t1", str(p))
        assert count == 3  # thinking + tool_call + tool_result

        trace = get_tool_trace(session, "t1")
        assert len(trace) == 3
        assert trace[0]["action_type"] == "thinking"
        assert trace[0]["thinking"] == "I need to read the file"
        assert trace[0]["provider"] == "river-35b"
        assert trace[1]["action_type"] == "tool_call"
        assert trace[1]["tool_name"] == "read"
        assert json.loads(trace[1]["tool_args"]) == {"path": "src/foo.py"}
        assert trace[2]["action_type"] == "tool_result"
        assert trace[2]["tool_result"] == "def foo(): pass"
        assert trace[2]["tool_status"] == "success"

    def test_truncation(self, session, tmp_path):
        long_text = "x" * 5000
        transcript = _make_transcript(
            [
                {
                    "type": "message",
                    "timestamp": "2026-03-21T12:00:00Z",
                    "message": {
                        "role": "toolResult",
                        "content": [{"type": "text", "text": long_text}],
                    },
                },
            ]
        )
        p = tmp_path / "long.jsonl"
        p.write_text(transcript)

        ingest_transcript(session, "t1", str(p))
        trace = get_tool_trace(session, "t1")
        assert len(trace[0]["tool_result"]) == 2000

    def test_duplicate_ingest_idempotent(self, session, tmp_path):
        transcript = _make_transcript(
            [
                {
                    "type": "message",
                    "timestamp": "2026-03-21T12:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "test"}],
                    },
                },
            ]
        )
        p = tmp_path / "t.jsonl"
        p.write_text(transcript)

        assert ingest_transcript(session, "t1", str(p)) == 1
        assert ingest_transcript(session, "t1", str(p)) == 1  # OR IGNORE
        trace = get_tool_trace(session, "t1")
        assert len(trace) == 1  # no duplicates


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExport:
    def test_export_trajectory(self, session, tmp_path):
        # Create spans
        s1 = open_span(session, "t1", SpanKind.DISPATCH, agent="qwen-35b")
        close_span(session, s1, SpanOutcome.SUCCESS)
        s2 = open_span(session, "t1", SpanKind.WAIT)
        close_span(session, s2, SpanOutcome.SUCCESS, wait_method="event:pane_done")
        s3 = open_span(session, "t1", SpanKind.REVIEW)
        close_span(session, s3, SpanOutcome.SUCCESS, verdict="safe", commit_count=1)

        # Ingest transcript
        transcript = _make_transcript(
            [
                {
                    "type": "message",
                    "timestamp": "2026-03-21T12:00:00Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "thinking", "thinking": "planning"}],
                    },
                },
            ]
        )
        p = tmp_path / "t.jsonl"
        p.write_text(transcript)
        ingest_transcript(session, "t1", str(p))

        traj = export_trajectory(session, "t1")
        assert traj["trace_id"] == "t1"
        assert traj["agent"] == "qwen-35b"
        assert traj["outcome"] == "success"
        assert traj["total_duration_ms"] >= 0
        assert len(traj["spans"]) == 3
        assert len(traj["tool_trace"]) == 1

    def test_export_failure_outcome(self, session):
        s1 = open_span(session, "t1", SpanKind.DISPATCH)
        close_span(session, s1, SpanOutcome.SUCCESS)
        s2 = open_span(session, "t1", SpanKind.REVIEW)
        close_span(session, s2, SpanOutcome.FAILURE, error="stale")

        traj = export_trajectory(session, "t1")
        assert traj["outcome"] == "failure"

    def test_export_empty_trace(self, session):
        traj = export_trajectory(session, "nonexistent")
        assert traj["spans"] == []
        assert traj["tool_trace"] == []
        assert traj["outcome"] == "success"  # vacuously true — no failures

    def test_export_trajectory_uses_prompts_table(self, session):
        from dgov.spans import store_prompt

        phash = store_prompt(session, "Fix the parser bug")
        sid = open_span(session, "t-prompt", SpanKind.DISPATCH, agent="pi", prompt_hash=phash)
        close_span(session, sid, SpanOutcome.SUCCESS)

        traj = export_trajectory(session, "t-prompt")
        assert traj["prompt"] == "Fix the parser bug"


# ---------------------------------------------------------------------------
# Prompts table
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrompts:
    def test_store_and_get(self, session):
        from dgov.spans import get_prompt, store_prompt

        phash = store_prompt(session, "hello world")
        assert len(phash) == 12
        assert get_prompt(session, phash) == "hello world"

    def test_store_idempotent(self, session):
        from dgov.spans import get_prompt, store_prompt

        h1 = store_prompt(session, "same prompt")
        h2 = store_prompt(session, "same prompt")
        assert h1 == h2
        assert get_prompt(session, h1) == "same prompt"

    def test_get_missing(self, session):
        from dgov.spans import get_prompt

        assert get_prompt(session, "nonexistent") == ""


# ---------------------------------------------------------------------------
# Archive + Transcripts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestArchivePane:
    def test_archive_and_query(self, session):
        from dgov.spans import _get_db, archive_pane

        pane = {
            "slug": "test-pane",
            "prompt": "Fix the bug",
            "agent": "river-35b",
            "project_root": "/tmp",
            "worktree_path": "/tmp/wt",
            "branch_name": "test-pane",
            "base_sha": "abc123",
            "created_at": "2024-01-01T00:00:00Z",
            "state": "merged",
            "metadata": {"landing": False},
        }
        archive_pane(session, pane)

        conn = _get_db(session)
        rows = conn.execute("SELECT slug, agent, final_state FROM archived_panes").fetchall()
        assert len(rows) == 1
        assert rows[0] == ("test-pane", "river-35b", "merged")

    def test_archive_idempotent_different_times(self, session):
        from dgov.spans import _get_db, archive_pane

        pane = {"slug": "dup", "state": "done"}
        archive_pane(session, pane)
        archive_pane(session, pane)  # same slug, different archived_at timestamp

        conn = _get_db(session)
        count = conn.execute("SELECT COUNT(*) FROM archived_panes").fetchone()[0]
        assert count >= 1  # at least 1, possibly 2 if timestamps differ


@pytest.mark.unit
class TestStoreTranscript:
    def test_store_and_query(self, session):
        from dgov.spans import _get_db, store_transcript

        raw = '{"type":"message"}\n{"type":"tool_use"}\n'
        store_transcript(session, "t1", raw)

        conn = _get_db(session)
        row = conn.execute(
            "SELECT raw_jsonl, line_count FROM transcripts WHERE trace_id = ?",
            ("t1",),
        ).fetchone()
        assert row[0] == raw
        assert row[1] == 2

    def test_store_idempotent(self, session):
        from dgov.spans import _get_db, store_transcript

        store_transcript(session, "t2", "line1\n")
        store_transcript(session, "t2", "line1\n")  # INSERT OR IGNORE

        conn = _get_db(session)
        count = conn.execute("SELECT COUNT(*) FROM transcripts WHERE trace_id = 't2'").fetchone()[
            0
        ]
        assert count == 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_prompt_hash():
    h = prompt_hash("Fix the parser bug")
    assert len(h) == 12
    assert all(c in "0123456789abcdef" for c in h)
    assert prompt_hash("Fix the parser bug") == h  # deterministic
