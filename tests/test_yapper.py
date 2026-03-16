"""Tests for dgov.yapper — conversational front-end."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from dgov.yapper import (
    YapperResult,
    _handle_chatter,
    _handle_command,
    _handle_idea,
    _handle_question,
    _validate_classification,
    classify,
    yap,
)

pytestmark = pytest.mark.unit

MOCK_REGISTRY = {
    "claude": MagicMock(),
    "pi": MagicMock(),
    "codex": MagicMock(),
}


# -- _validate_classification --


class TestValidateClassification:
    def test_valid_input(self):
        raw = {
            "category": "COMMAND",
            "agent_hint": "claude",
            "files": ["src/foo.py"],
            "urgency": "now",
            "summary": "fix the bug",
        }
        result = _validate_classification(raw, MOCK_REGISTRY)
        assert result["category"] == "COMMAND"
        assert result["agent_hint"] == "claude"
        assert result["files"] == ["src/foo.py"]
        assert result["summary"] == "fix the bug"

    def test_unknown_category_defaults_to_chatter(self):
        raw = {"category": "BANANA", "summary": "wat"}
        result = _validate_classification(raw)
        assert result["category"] == "CHATTER"

    def test_unknown_agent_cleared(self):
        raw = {
            "category": "COMMAND",
            "agent_hint": "nonexistent",
            "summary": "x",
        }
        result = _validate_classification(raw, MOCK_REGISTRY)
        assert result["agent_hint"] is None

    def test_unsafe_file_paths_stripped(self):
        raw = {
            "category": "COMMAND",
            "files": [
                "src/ok.py",
                "/etc/passwd",
                "../../escape",
                "valid/path.ts",
                "has space.py",
            ],
            "summary": "x",
        }
        result = _validate_classification(raw)
        assert result["files"] == ["src/ok.py", "valid/path.ts"]

    def test_summary_truncated(self):
        raw = {"category": "COMMAND", "summary": "x" * 300}
        result = _validate_classification(raw)
        assert len(result["summary"]) == 200

    def test_missing_fields_get_defaults(self):
        result = _validate_classification({})
        assert result["category"] == "CHATTER"
        assert result["agent_hint"] is None
        assert result["files"] == []
        assert result["urgency"] == "now"
        assert result["summary"] == ""


# -- classify --


class TestClassify:
    @patch("dgov.openrouter.chat_completion")
    def test_valid_response(self, mock_cc):
        mock_cc.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "category": "COMMAND",
                                "agent_hint": None,
                                "files": [],
                                "urgency": "now",
                                "summary": "fix tests",
                            }
                        )
                    }
                }
            ]
        }
        result = classify("fix the flaky tests")
        assert result["category"] == "COMMAND"
        assert result["summary"] == "fix tests"
        assert "_fallback" not in result

    @patch("dgov.openrouter.chat_completion")
    def test_markdown_fences_stripped(self, mock_cc):
        mock_cc.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '```json\n{"category": "IDEA",'
                            ' "agent_hint": null, "files": [],'
                            ' "urgency": "now",'
                            ' "summary": "add caching"}\n```'
                        )
                    }
                }
            ]
        }
        result = classify("what if we added caching")
        assert result["category"] == "IDEA"

    @patch("dgov.openrouter.chat_completion")
    def test_json_decode_error_falls_back(self, mock_cc):
        mock_cc.return_value = {"choices": [{"message": {"content": "not json"}}]}
        result = classify("hello there")
        assert result["category"] == "CHATTER"
        assert result["_fallback"] is True

    @patch("dgov.openrouter.chat_completion")
    def test_runtime_error_falls_back(self, mock_cc):
        mock_cc.side_effect = RuntimeError("API down")
        result = classify("fix something")
        assert result["category"] == "CHATTER"
        assert result["_fallback"] is True


# -- _handle_command --


class TestHandleCommand:
    @patch("dgov.persistence.emit_event")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.strategy.classify_task", return_value="pi")
    @patch("dgov.agents.load_registry", return_value=MOCK_REGISTRY)
    def test_dispatches_worker(self, _reg, _ct, mock_create, mock_emit):
        mock_pane = MagicMock()
        mock_pane.slug = "fix-tests"
        mock_create.return_value = mock_pane

        classification = {
            "category": "COMMAND",
            "summary": "fix tests",
            "agent_hint": None,
        }
        result = _handle_command("fix the tests", classification, "/repo", "/repo")

        assert result.action == "dispatched"
        assert result.slug == "fix-tests"
        assert result.agent == "pi"
        mock_create.assert_called_once()
        mock_emit.assert_called_once()

    @patch("dgov.persistence.emit_event")
    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.strategy.classify_task")
    @patch("dgov.agents.load_registry", return_value=MOCK_REGISTRY)
    def test_respects_agent_hint(self, _reg, mock_ct, mock_create, _emit):
        mock_pane = MagicMock()
        mock_pane.slug = "debug-merger"
        mock_create.return_value = mock_pane

        classification = {
            "category": "COMMAND",
            "summary": "debug merger",
            "agent_hint": "claude",
        }
        result = _handle_command(
            "have claude debug the merger",
            classification,
            "/repo",
            "/repo",
        )

        assert result.agent == "claude"
        mock_ct.assert_not_called()

    @patch("dgov.lifecycle.create_worker_pane")
    @patch("dgov.strategy.classify_task", return_value="pi")
    @patch("dgov.agents.load_registry", return_value=MOCK_REGISTRY)
    def test_dispatch_failure_returns_error(self, _reg, _ct, mock_create):
        mock_create.side_effect = RuntimeError("tmux not found")

        classification = {
            "category": "COMMAND",
            "summary": "fix tests",
            "agent_hint": None,
        }
        result = _handle_command("fix tests", classification, "/repo", "/repo")

        assert result.action == "error"
        assert "tmux not found" in result.reply


# -- _handle_idea --


class TestHandleIdea:
    @patch("dgov.persistence.emit_event")
    def test_writes_to_ideas_jsonl(self, mock_emit, tmp_path):
        session_root = str(tmp_path)
        classification = {"category": "IDEA", "summary": "add caching layer"}
        result = _handle_idea("idea: add caching layer", classification, session_root)

        assert result.action == "noted"
        assert "caching" in result.reply

        ideas_file = tmp_path / ".dgov" / "ideas.jsonl"
        assert ideas_file.exists()
        entry = json.loads(ideas_file.read_text().strip())
        assert entry["summary"] == "add caching layer"
        assert "ts" in entry

        mock_emit.assert_called_once()


# -- _handle_question --


class TestHandleQuestion:
    @patch("dgov.status.list_worker_panes")
    def test_status_query_with_panes(self, mock_list):
        mock_list.return_value = [
            {"slug": "fix-parser", "agent": "claude", "state": "active"},
            {"slug": "add-tests", "agent": "pi", "state": "done"},
        ]
        classification = {
            "category": "QUESTION",
            "summary": "what is running",
        }
        result = _handle_question("what's running?", classification, "/repo", "/repo")

        assert result.action == "answered"
        assert "2 pane(s)" in result.reply
        assert "fix-parser" in result.reply

    @patch("dgov.status.list_worker_panes")
    def test_status_query_no_panes(self, mock_list):
        mock_list.return_value = []
        classification = {"category": "QUESTION", "summary": "status"}
        result = _handle_question("status?", classification, "/repo", "/repo")

        assert "No active panes" in result.reply

    def test_non_status_question(self):
        classification = {
            "category": "QUESTION",
            "summary": "how does X work",
        }
        result = _handle_question("how does the merger work?", classification, "/repo", "/repo")

        assert "governor directly" in result.reply


# -- _handle_chatter --


class TestHandleChatter:
    def test_thanks(self):
        result = _handle_chatter("thanks!", {"summary": "thanks"})
        assert result.reply == "You got it."

    def test_greeting(self):
        result = _handle_chatter("hey", {"summary": "hey"})
        assert result.reply == "Ready. What do you need?"

    def test_acknowledgment(self):
        result = _handle_chatter("ok cool", {"summary": "ok cool"})
        assert result.reply == "Standing by."

    def test_default(self):
        result = _handle_chatter("hmm", {"summary": "hmm"})
        assert result.reply == "Copy."


# -- yap (end-to-end routing) --


class TestYap:
    @patch("dgov.yapper.classify")
    @patch("dgov.agents.load_registry", return_value=MOCK_REGISTRY)
    @patch("dgov.yapper._handle_command")
    def test_routes_command(self, mock_cmd, _reg, mock_classify):
        mock_classify.return_value = {
            "category": "COMMAND",
            "summary": "fix it",
        }
        mock_cmd.return_value = YapperResult(
            category="COMMAND", action="dispatched", summary="fix it"
        )
        result = yap("fix the tests", "/repo")
        assert result.action == "dispatched"
        mock_cmd.assert_called_once()

    @patch("dgov.yapper.classify")
    @patch("dgov.agents.load_registry", return_value=MOCK_REGISTRY)
    @patch("dgov.yapper._handle_idea")
    def test_routes_idea(self, mock_idea, _reg, mock_classify):
        mock_classify.return_value = {
            "category": "IDEA",
            "summary": "caching",
        }
        mock_idea.return_value = YapperResult(category="IDEA", action="noted", summary="caching")
        result = yap("idea: add caching", "/repo")
        assert result.action == "noted"

    @patch("dgov.yapper.classify")
    @patch("dgov.agents.load_registry", return_value=MOCK_REGISTRY)
    @patch("dgov.yapper._handle_question")
    def test_routes_question(self, mock_q, _reg, mock_classify):
        mock_classify.return_value = {
            "category": "QUESTION",
            "summary": "status",
        }
        mock_q.return_value = YapperResult(
            category="QUESTION", action="answered", summary="status"
        )
        result = yap("what's running?", "/repo")
        assert result.action == "answered"

    @patch("dgov.yapper.classify")
    @patch("dgov.agents.load_registry", return_value=MOCK_REGISTRY)
    def test_routes_chatter(self, _reg, mock_classify):
        mock_classify.return_value = {
            "category": "CHATTER",
            "summary": "thanks",
        }
        result = yap("thanks", "/repo")
        assert result.action == "ack"

    @patch("dgov.yapper.classify")
    @patch("dgov.agents.load_registry", return_value=MOCK_REGISTRY)
    def test_fallback_notification(self, _reg, mock_classify):
        mock_classify.return_value = {
            "category": "CHATTER",
            "summary": "hello",
            "_fallback": True,
        }
        result = yap("hello", "/repo")
        assert result.action == "ack"
        assert "(classification failed)" in result.reply
