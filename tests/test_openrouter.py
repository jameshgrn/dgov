"""Tests for OpenRouter integration and LLM fallback chain."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


def _mock_response(content: str, model: str = "test-model") -> bytes:
    """Build a fake OpenAI-compatible JSON response."""
    return json.dumps(
        {
            "choices": [{"message": {"content": content}}],
            "model": model,
        }
    ).encode()


class TestOpenRouterRequest:
    def test_sends_correct_headers(self):
        from dgov.openrouter import _openrouter_request

        fake_resp = MagicMock()
        fake_resp.read.return_value = _mock_response("hello")
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("dgov.openrouter._get_api_key", return_value="sk-or-test"),
            patch("dgov.openrouter.urllib.request.urlopen", return_value=fake_resp) as mock_open,
        ):
            result = _openrouter_request([{"role": "user", "content": "hi"}])

        assert result["choices"][0]["message"]["content"] == "hello"
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer sk-or-test"
        assert req.get_header("Http-referer") == "https://github.com/jameshgrn/dgov"
        assert req.get_header("X-title") == "dgov"

    def test_raises_without_api_key(self):
        from dgov.openrouter import _openrouter_request

        with patch("dgov.openrouter._get_api_key", return_value=None):
            with pytest.raises(RuntimeError, match="No OpenRouter API key"):
                _openrouter_request([{"role": "user", "content": "hi"}])

    def test_handles_429_rate_limit(self):
        import urllib.request

        from dgov.openrouter import _openrouter_request

        err = urllib.request.HTTPError(
            url="https://openrouter.ai/api/v1/chat/completions",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=None,
        )

        with (
            patch("dgov.openrouter._get_api_key", return_value="sk-or-test"),
            patch("dgov.openrouter.urllib.request.urlopen", side_effect=err),
        ):
            with pytest.raises(RuntimeError, match="rate limited"):
                _openrouter_request([{"role": "user", "content": "hi"}])


class TestQwen4bRequest:
    def test_direct_localhost_succeeds(self):
        from dgov.openrouter import _qwen_4b_request

        fake_resp = MagicMock()
        fake_resp.read.return_value = _mock_response("ok")
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("dgov.openrouter.urllib.request.urlopen", return_value=fake_resp):
            result = _qwen_4b_request([{"role": "user", "content": "test"}])

        assert result["choices"][0]["message"]["content"] == "ok"

    def test_raises_on_localhost_failure(self):
        import urllib.error

        from dgov.openrouter import _qwen_4b_request

        with patch(
            "dgov.openrouter.urllib.request.urlopen",
            side_effect=urllib.error.URLError("refused"),
        ):
            with pytest.raises(RuntimeError, match="not reachable"):
                _qwen_4b_request([{"role": "user", "content": "test"}])


class TestChatCompletion:
    def test_openrouter_first_when_available(self):
        from dgov.openrouter import chat_completion

        with (
            patch(
                "dgov.openrouter._openrouter_request",
                return_value={"choices": [{"message": {"content": "or-ok"}}]},
            ) as mock_or,
            patch("dgov.openrouter._qwen_4b_request") as mock_qwen,
        ):
            result = chat_completion([{"role": "user", "content": "hi"}])

        assert result["choices"][0]["message"]["content"] == "or-ok"
        mock_or.assert_called_once()
        mock_qwen.assert_not_called()

    def test_falls_back_to_qwen_on_openrouter_failure(self):
        from dgov.openrouter import chat_completion

        with (
            patch("dgov.openrouter._openrouter_request", side_effect=RuntimeError("no key")),
            patch(
                "dgov.openrouter._qwen_4b_request",
                return_value={"choices": [{"message": {"content": "qwen-ok"}}]},
            ),
        ):
            result = chat_completion([{"role": "user", "content": "hi"}])

        assert result["choices"][0]["message"]["content"] == "qwen-ok"

    def test_raises_when_all_fail(self):
        from dgov.openrouter import chat_completion

        with (
            patch("dgov.openrouter._openrouter_request", side_effect=RuntimeError("fail")),
            patch("dgov.openrouter._qwen_4b_request", side_effect=RuntimeError("fail")),
        ):
            with pytest.raises(RuntimeError, match="All LLM providers failed"):
                chat_completion([{"role": "user", "content": "hi"}])


class TestClassifyTask:
    def test_classify_pi_via_openrouter(self):
        from dgov.strategy import classify_task

        with patch(
            "dgov.openrouter.chat_completion",
            return_value={"choices": [{"message": {"content": "pi"}}]},
        ):
            assert classify_task("rename variable x to y in main.py") == "pi"

    def test_classify_claude_via_openrouter(self):
        from dgov.strategy import classify_task

        with patch(
            "dgov.openrouter.chat_completion",
            return_value={"choices": [{"message": {"content": "claude"}}]},
        ):
            assert classify_task("debug why tests are flaky") == "claude"

    def test_classify_falls_back_to_claude_on_error(self):
        from dgov.strategy import classify_task

        with patch("dgov.openrouter.chat_completion", side_effect=RuntimeError("all failed")):
            assert classify_task("anything") == "claude"

    def test_classify_multi_agent(self):
        from dgov.strategy import classify_task

        with patch(
            "dgov.openrouter.chat_completion",
            return_value={"choices": [{"message": {"content": "codex"}}]},
        ):
            result = classify_task(
                "refactor all files", installed_agents=["pi", "claude", "codex", "gemini"]
            )
            assert result == "codex"


class TestGenerateSlug:
    def test_word_extraction(self):
        from dgov.strategy import _generate_slug

        slug = _generate_slug("fix login bug auth")
        assert "fix" in slug or "login" in slug or "bug" in slug

    def test_strips_stopwords(self):
        from dgov.strategy import _generate_slug

        slug = _generate_slug("fix the broken test in scheduler")
        assert "the" not in slug.split("-")
        assert "in" not in slug.split("-")

    def test_limits_max_words(self):
        from dgov.strategy import _generate_slug

        slug = _generate_slug("a b c d e f g h", max_words=3)
        assert len(slug.split("-")) <= 3


class TestConfig:
    def test_load_config_from_toml(self, tmp_path):
        from dgov.openrouter import _load_config

        config_path = tmp_path / ".dgov" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            '[openrouter]\napi_key = "sk-or-test"\ndefault_model = "custom/model:free"\n'
        )

        with patch("dgov.openrouter.Path.home", return_value=tmp_path):
            config = _load_config()

        assert config["api_key"] == "sk-or-test"
        assert config["default_model"] == "custom/model:free"

    def test_env_var_overrides_config(self, tmp_path, monkeypatch):
        from dgov.openrouter import _get_api_key

        config_path = tmp_path / ".dgov" / "config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text('[openrouter]\napi_key = "sk-or-from-config"\n')

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-from-env")
        with patch("dgov.openrouter.Path.home", return_value=tmp_path):
            key = _get_api_key()

        assert key == "sk-or-from-env"

    def test_no_config_returns_empty(self, tmp_path):
        from dgov.openrouter import _load_config

        with patch("dgov.openrouter.Path.home", return_value=tmp_path):
            config = _load_config()

        assert config == {}


class TestListFreeModels:
    def test_fetches_and_filters_free_models(self):
        import dgov.openrouter as mod

        # Reset cache
        mod._free_models_cache = None
        mod._free_models_cache_time = 0

        api_response = json.dumps(
            {
                "data": [
                    {"id": "meta-llama/llama-3.1-8b-instruct:free", "name": "Llama 3.1 8B"},
                    {"id": "openai/gpt-4o", "name": "GPT-4o"},
                    {"id": "qwen/qwen-2.5-7b-instruct:free", "name": "Qwen 2.5 7B"},
                ]
            }
        ).encode()

        fake_resp = MagicMock()
        fake_resp.read.return_value = api_response
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("dgov.openrouter._get_api_key", return_value="sk-or-test"),
            patch("dgov.openrouter.urllib.request.urlopen", return_value=fake_resp),
        ):
            models = mod.list_free_models()

        assert len(models) == 2
        assert models[0]["id"] == "meta-llama/llama-3.1-8b-instruct:free"
        assert models[1]["id"] == "qwen/qwen-2.5-7b-instruct:free"

    def test_uses_cache_on_second_call(self):
        import dgov.openrouter as mod

        mod._free_models_cache = [{"id": "cached:free", "name": "Cached"}]
        mod._free_models_cache_time = __import__("time").time()

        # Should not make any HTTP call
        models = mod.list_free_models()
        assert models == [{"id": "cached:free", "name": "Cached"}]

        # Cleanup
        mod._free_models_cache = None
        mod._free_models_cache_time = 0


class TestCheckStatus:
    def test_no_api_key(self):
        from dgov.openrouter import check_status

        with (
            patch("dgov.openrouter._get_api_key", return_value=None),
            patch("dgov.openrouter._get_default_model", return_value="test/model:free"),
        ):
            status = check_status()

        assert status["api_key_set"] is False
        assert status["api_reachable"] is False
        assert "No API key" in status["error"]

    def test_reachable(self):
        from dgov.openrouter import check_status

        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = MagicMock(return_value=False)

        with (
            patch("dgov.openrouter._get_api_key", return_value="sk-or-test"),
            patch("dgov.openrouter._get_default_model", return_value="test/model:free"),
            patch("dgov.openrouter.urllib.request.urlopen", return_value=fake_resp),
            patch("dgov.openrouter.get_key_info", return_value={"label": "test"}),
        ):
            status = check_status()

        assert status["api_key_set"] is True
        assert status["api_reachable"] is True
        assert status["error"] is None
