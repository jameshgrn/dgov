"""Tests for dgov config: project config loading and prompt rendering."""

from pathlib import Path

import pytest

from dgov.config import ProjectConfig, load_project_config
from dgov.tool_policy import ToolPolicy
from dgov.workers.config import AtomicConfig, ProviderConfig

pytestmark = pytest.mark.unit

_NAMED_PROVIDER_REGISTRY_TOML = """
[project]
provider = "openai"

[providers.openai]
default_agent = "gpt-test"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[providers.openrouter]
default_agent = "openrouter/test"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
max_tokens = 8192
token_limit_label = "OpenRouter token policy"
prompt_token_limit_header = "x-prompt-limit"
generated_token_limit_header = "x-generated-limit"
"""


def _project_with_provider(
    name: str,
    *,
    base_url: str = "https://provider.test/v1",
    api_key_env: str = "TEST_PROVIDER_API_KEY",
    default_agent: str = "",
    max_tokens: int | None = None,
    token_limit_label: str = "",
    prompt_token_limit_header: str = "",
    generated_token_limit_header: str = "",
    **kwargs,
) -> ProjectConfig:
    return ProjectConfig(
        llm_provider=name,
        llm_base_url=base_url,
        llm_api_key_env=api_key_env,
        default_agent=default_agent,
        providers={
            name: ProviderConfig(
                name=name,
                base_url=base_url,
                api_key_env=api_key_env,
                default_agent=default_agent,
                max_tokens=max_tokens,
                token_limit_label=token_limit_label,
                prompt_token_limit_header=prompt_token_limit_header,
                generated_token_limit_header=generated_token_limit_header,
            )
        },
        **kwargs,
    )


def _write_named_provider_registry(root: Path) -> None:
    dgov_dir = root / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(_NAMED_PROVIDER_REGISTRY_TOML)


def _assert_openrouter_provider(pc: ProjectConfig) -> None:
    assert pc.llm_runtime_settings("openrouter") == (
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
    )
    assert pc.provider_config("openrouter").max_tokens == 8192
    openrouter = pc.provider_config("openrouter")
    assert openrouter.token_limit_label == "OpenRouter token policy"
    assert openrouter.prompt_token_limit_header == "x-prompt-limit"
    assert openrouter.generated_token_limit_header == "x-generated-limit"
    payload = pc.to_worker_payload("openrouter")
    assert payload["llm_max_tokens"] == 8192
    assert payload["llm_token_limit_label"] == "OpenRouter token policy"
    assert payload["llm_prompt_token_limit_header"] == "x-prompt-limit"
    assert payload["llm_generated_token_limit_header"] == "x-generated-limit"


class TestProjectConfigDefaults:
    def test_defaults(self):
        pc = ProjectConfig()
        assert pc.language == "python"
        assert pc.src_dir == "src/"
        assert pc.test_dir == "tests/"
        assert pc.default_agent == ""
        assert pc.llm_provider == ""
        assert pc.llm_base_url == ""
        assert pc.llm_api_key_env == ""
        assert "pytest" in pc.test_cmd
        assert "ruff check" in pc.lint_cmd
        assert pc.worker_iteration_budget == 50
        assert pc.worker_iteration_warn_at == 40
        assert pc.worker_tree_max_lines == 80
        assert pc.bootstrap_timeout == 300

    def test_resolve_test_cmd(self):
        pc = ProjectConfig(test_cmd="pytest {test_dir} -q", test_dir="tests/")
        assert pc.resolve_test_cmd() == "pytest tests/ -q"

    def test_resolve_test_cmd_with_file(self):
        pc = ProjectConfig(test_cmd="pytest {test_dir} -q", test_dir="tests/")
        result = pc.resolve_test_cmd("tests/test_foo.py")
        assert "test_foo.py" in result

    def test_resolve_lint_cmd(self):
        pc = ProjectConfig(lint_cmd="ruff check {file}", src_dir="src/")
        assert pc.resolve_lint_cmd() == "ruff check src/"
        assert pc.resolve_lint_cmd("foo.py") == "ruff check foo.py"

    def test_resolve_format_cmd(self):
        pc = ProjectConfig(format_cmd="ruff format {file}")
        assert pc.resolve_format_cmd("foo.py") == "ruff format foo.py"


class TestPromptSection:
    def test_basic_rendering(self):
        pc = ProjectConfig()
        section = pc.to_prompt_section()
        assert "Language: python" in section
        assert "Source: src/" in section
        assert "Tests: tests/" in section
        assert "LLM provider:" in section
        assert "LLM base URL:" in section
        assert "LLM API key env:" in section

    def test_includes_markers(self):
        pc = ProjectConfig(test_markers=("unit", "integration"))
        section = pc.to_prompt_section()
        assert "unit" in section
        assert "integration" in section

    def test_includes_worker_prompt_settings(self):
        pc = ProjectConfig(
            worker_iteration_budget=75,
            worker_iteration_warn_at=60,
            worker_tree_max_lines=0,
        )
        section = pc.to_prompt_section()
        assert "Worker iteration budget: 75" in section
        assert "Worker iteration warn at: 60" in section
        assert "Worker tree max lines: 0" in section

    def test_includes_conventions(self):
        pc = ProjectConfig(conventions={"style": "google", "imports": "isort"})
        section = pc.to_prompt_section()
        assert "style: google" in section
        assert "imports: isort" in section

    def test_includes_tool_policy(self):
        pc = ProjectConfig(
            tool_policy=ToolPolicy(
                restrict_run_bash=True,
                require_uv_run=True,
                require_wrapped_verify_tools=True,
            )
        )
        section = pc.to_prompt_section()
        assert "run_bash is restricted" in section
        assert "must use 'uv run'" in section


class TestWorkerPayload:
    def test_worker_payload_round_trip_preserves_worker_fields(self):
        pc = _project_with_provider(
            "fireworks",
            base_url="https://api.fireworks.ai/inference/v1",
            api_key_env="CUSTOM_FIREWORKS_API_KEY",
            type_check_cmd="uv run ty check",
            worker_iteration_budget=75,
            worker_iteration_warn_at=60,
            worker_tree_max_lines=0,
            line_length=120,
            max_tokens=8192,
            token_limit_label="Fireworks adaptive serverless TPM",
            prompt_token_limit_header="x-ratelimit-limit-tokens-prompt",
            generated_token_limit_header="x-ratelimit-limit-tokens-generated",
            verify_commands={"rating": "uv run pytest tests/test_rating.py -q"},
            test_markers=("unit",),
            conventions={"imports": "absolute"},
            tool_policy=ToolPolicy(require_uv_run=True),
        )

        round_tripped = ProjectConfig.from_worker_payload(pc.to_worker_payload())

        assert round_tripped.llm_runtime_settings() == (
            "https://api.fireworks.ai/inference/v1",
            "CUSTOM_FIREWORKS_API_KEY",
        )
        assert round_tripped.llm_provider == "fireworks"
        assert round_tripped.type_check_cmd == "uv run ty check"
        assert round_tripped.line_length == 120
        assert round_tripped.verify_commands == {"rating": "uv run pytest tests/test_rating.py -q"}
        assert round_tripped.test_markers == ("unit",)
        assert round_tripped.conventions == {"imports": "absolute"}
        assert round_tripped.tool_policy.require_uv_run is True
        assert round_tripped.llm_max_tokens == 8192
        assert round_tripped.llm_token_limit_label == "Fireworks adaptive serverless TPM"
        assert round_tripped.llm_prompt_token_limit_header == "x-ratelimit-limit-tokens-prompt"
        assert (
            round_tripped.llm_generated_token_limit_header == "x-ratelimit-limit-tokens-generated"
        )
        provider = round_tripped.provider_config("fireworks")
        assert provider.max_tokens == 8192
        assert provider.token_limit_label == "Fireworks adaptive serverless TPM"
        assert provider.prompt_token_limit_header == "x-ratelimit-limit-tokens-prompt"
        assert provider.generated_token_limit_header == "x-ratelimit-limit-tokens-generated"

    def test_to_atomic_config_preserves_type_check_and_line_length(self):
        pc = _project_with_provider(
            "test-provider",
            type_check_cmd="uv run ty check",
            line_length=120,
        )

        atomic = pc.to_atomic_config()

        assert isinstance(atomic, AtomicConfig)
        assert atomic.llm_provider == "test-provider"
        assert atomic.type_check_cmd == "uv run ty check"
        assert atomic.line_length == 120

    def test_to_atomic_config_preserves_verify_commands(self):
        pc = _project_with_provider(
            "test-provider",
            verify_commands={"lint": "uv run ruff check src/dgov"},
        )

        atomic = pc.to_atomic_config()

        assert atomic.verify_commands == {"lint": "uv run ruff check src/dgov"}

    def test_to_worker_payload_can_select_named_provider(self):
        pc = ProjectConfig(
            llm_provider="openai",
            llm_base_url="https://api.openai.com/v1",
            llm_api_key_env="OPENAI_API_KEY",
            providers={
                "openai": ProviderConfig(
                    name="openai",
                    base_url="https://api.openai.com/v1",
                    api_key_env="OPENAI_API_KEY",
                    default_agent="gpt-test",
                )
            },
        )

        payload = pc.to_worker_payload("openai")

        assert payload["llm_provider"] == "openai"
        assert payload["llm_base_url"] == "https://api.openai.com/v1"
        assert payload["llm_api_key_env"] == "OPENAI_API_KEY"


class TestLoadProjectConfig:
    def test_missing_file_returns_defaults(self, tmp_path):
        pc = load_project_config(tmp_path)
        assert pc.language == "python"
        assert pc.llm_provider == ""

    def test_malformed_project_toml_raises(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text("this is not valid toml {{{")

        with pytest.raises(ValueError, match="Invalid TOML"):
            load_project_config(tmp_path)

    def test_loads_toml(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[project]\nlanguage = "go"\nsrc_dir = "cmd/"\n'
            'test_dir = "cmd/"\ntest_cmd = "go test ./..."\n'
            'provider = "test-provider"\n'
            'lint_cmd = "golangci-lint run {file}"\n'
            'format_cmd = "gofmt -w {file}"\n'
            "worker_iteration_budget = 75\n"
            "worker_iteration_warn_at = 60\n"
            "worker_tree_max_lines = 0\n"
            "bootstrap_timeout = 45\n"
            "\n[providers.test-provider]\n"
            'default_agent = "test-model"\n'
            'base_url = "https://provider.test/v1"\n'
            'api_key_env = "TEST_PROVIDER_API_KEY"\n'
        )
        pc = load_project_config(tmp_path)
        assert pc.language == "go"
        assert pc.src_dir == "cmd/"
        assert pc.test_cmd == "go test ./..."
        assert pc.default_agent == "test-model"
        assert pc.llm_provider == "test-provider"
        assert pc.llm_base_url == "https://provider.test/v1"
        assert pc.llm_api_key_env == "TEST_PROVIDER_API_KEY"
        assert pc.worker_iteration_budget == 75
        assert pc.worker_iteration_warn_at == 60
        assert pc.worker_tree_max_lines == 0
        assert pc.bootstrap_timeout == 45

    def test_partial_toml_fills_defaults(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\nlanguage = "rust"\n')
        pc = load_project_config(tmp_path)
        assert pc.language == "rust"
        assert pc.test_dir == "tests/"  # default preserved

    def test_conventions_section(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[project]\n\n[conventions]\ntest_style = "pytest fixtures"\n'
            'imports = "absolute only"\n'
        )
        pc = load_project_config(tmp_path)
        assert pc.conventions == {"test_style": "pytest fixtures", "imports": "absolute only"}

    def test_markers_as_list(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\ntest_markers = ["unit", "slow"]\n')
        pc = load_project_config(tmp_path)
        assert pc.test_markers == ("unit", "slow")

    def test_type_check_cmd_loaded(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\ntype_check_cmd = "ty check"\n')
        pc = load_project_config(tmp_path)
        assert pc.type_check_cmd == "ty check"

    def test_loads_named_provider_registry(self, tmp_path):
        _write_named_provider_registry(tmp_path)

        pc = load_project_config(tmp_path)

        assert pc.llm_provider == "openai"
        assert pc.default_agent == "gpt-test"
        assert pc.llm_runtime_settings() == ("https://api.openai.com/v1", "OPENAI_API_KEY")
        _assert_openrouter_provider(pc)
        assert pc.provider_default_agents() == {
            "openai": "gpt-test",
            "openrouter": "openrouter/test",
        }

    def test_loads_claude_code_provider_without_api_key_env(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            """
[project]
provider = "claude-haiku-worker"

[providers.claude-haiku-worker]
default_agent = "haiku"
base_url = "claude-code://fast?preset=edit"
"""
        )

        pc = load_project_config(tmp_path)

        provider = pc.provider_config("claude-haiku-worker")
        assert provider.base_url == "claude-code://fast?preset=edit"
        assert provider.api_key_env == ""
        assert provider.requires_api_key() is False
        assert pc.llm_api_key_env == ""
        payload = pc.to_worker_payload("claude-haiku-worker")
        assert payload["llm_api_key_env"] == ""

    def test_source_repo_config_wires_local_gemma_provider(self):
        pc = load_project_config(Path(__file__).resolve().parents[1])

        local = pc.provider_config("local")
        assert local.name == "local"
        assert local.base_url == "http://localhost:8080/v1"
        assert local.api_key_env == "LOCAL_LLM_API_KEY"
        assert local.default_agent == "unsloth/gemma-4-26b-a4b-it-UD-MLX-4bit"
        assert local.max_tokens == 262144
        assert pc.provider_default_agents()["local"] == local.default_agent
        assert pc.agents["gemma-4-26b-a4b"] == local.default_agent

        payload = pc.to_worker_payload("local")
        assert payload["llm_provider"] == "local"
        assert payload["llm_base_url"] == local.base_url
        assert payload["llm_api_key_env"] == local.api_key_env
        assert payload["llm_max_tokens"] == local.max_tokens

        fireworks = pc.provider_config("fireworks")
        assert fireworks.token_limit_label == "Fireworks adaptive serverless TPM"
        assert fireworks.prompt_token_limit_header == "x-ratelimit-limit-tokens-prompt"
        assert fireworks.generated_token_limit_header == "x-ratelimit-limit-tokens-generated"
        fireworks_payload = pc.to_worker_payload("fireworks")
        assert fireworks_payload["llm_token_limit_label"] == fireworks.token_limit_label
        assert (
            fireworks_payload["llm_prompt_token_limit_header"]
            == fireworks.prompt_token_limit_header
        )
        assert (
            fireworks_payload["llm_generated_token_limit_header"]
            == fireworks.generated_token_limit_header
        )

        haiku = pc.provider_config("claude-haiku-worker")
        assert haiku.default_agent == "haiku"
        assert haiku.base_url == "claude-code://fast?preset=edit"
        assert haiku.api_key_env == ""
        assert haiku.requires_api_key() is False

        sonnet = pc.provider_config("claude-sonnet-review")
        assert sonnet.default_agent == "sonnet"
        assert sonnet.base_url == "claude-code://daily?preset=review"
        assert sonnet.api_key_env == ""
        assert sonnet.requires_api_key() is False

    def test_rejects_unknown_selected_provider(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\nprovider = "openai"\n')

        with pytest.raises(ValueError, match=r"\[providers\.openai\]"):
            load_project_config(tmp_path)

    def test_rejects_openai_compatible_provider_without_api_key_env(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            """
[project]
provider = "fireworks"

[providers.fireworks]
base_url = "https://api.fireworks.ai/inference/v1"
"""
        )

        with pytest.raises(ValueError, match=r"\[providers\.fireworks\]\.api_key_env"):
            load_project_config(tmp_path)

    @pytest.mark.parametrize(
        ("project_toml", "message"),
        [
            (
                """
[project]
provider = 123

[providers.fireworks]
base_url = "https://api.fireworks.ai/inference/v1"
api_key_env = "FIREWORKS_API_KEY"
""",
                r"\[project\]\.provider must be a string",
            ),
            (
                """
[project]
provider = "fireworks"
default_agent = false

[providers.fireworks]
base_url = "https://api.fireworks.ai/inference/v1"
api_key_env = "FIREWORKS_API_KEY"
""",
                r"\[project\]\.default_agent must be a string",
            ),
            (
                """
[project]
provider = "fireworks"

[providers.fireworks]
base_url = 123
api_key_env = "FIREWORKS_API_KEY"
""",
                r"\[providers\.fireworks\]\.base_url must be a string",
            ),
            (
                """
[project]
provider = "fireworks"

[providers.fireworks]
base_url = "https://api.fireworks.ai/inference/v1"
api_key_env = ["FIREWORKS_API_KEY"]
""",
                r"\[providers\.fireworks\]\.api_key_env must be a string",
            ),
            (
                """
[project]
provider = "fireworks"

[providers.fireworks]
default_agent = 123
base_url = "https://api.fireworks.ai/inference/v1"
api_key_env = "FIREWORKS_API_KEY"
""",
                r"\[providers\.fireworks\]\.default_agent must be a string",
            ),
            (
                """
[project]
provider = "fireworks"

[providers.fireworks]
base_url = "https://api.fireworks.ai/inference/v1"
api_key_env = "FIREWORKS_API_KEY"
max_tokens = "8192"
""",
                r"\[providers\.fireworks\]\.max_tokens must be an integer",
            ),
            (
                """
[project]
provider = "fireworks"

[providers.fireworks]
base_url = "https://api.fireworks.ai/inference/v1"
api_key_env = "FIREWORKS_API_KEY"
max_tokens = 0
""",
                r"\[providers\.fireworks\]\.max_tokens must be > 0",
            ),
        ],
    )
    def test_rejects_non_string_provider_config(self, tmp_path, project_toml: str, message: str):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(project_toml)

        with pytest.raises(ValueError, match=message):
            load_project_config(tmp_path)

    def test_rejects_non_string_agent_alias(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text("[agents]\nfast = 123\n")

        with pytest.raises(ValueError, match=r"\[agents\] values must be strings: fast"):
            load_project_config(tmp_path)

    def test_tool_policy_loaded(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            """
[project]

[tool_policy]
restrict_run_bash = true
require_wrapped_verify_tools = true
require_uv_run = true
deny_shell_file_mutations = true
deny_shell_commands = ["pip", "python -m pip"]
"""
        )
        pc = load_project_config(tmp_path)
        assert pc.tool_policy.restrict_run_bash is True
        assert pc.tool_policy.require_wrapped_verify_tools is True
        assert pc.tool_policy.require_uv_run is True
        assert pc.tool_policy.deny_shell_file_mutations is True
        assert pc.tool_policy.deny_shell_commands == ("pip", "python -m pip")

    def test_tool_policy_loaded_with_deny_network_egress(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            """
[project]

[tool_policy]
deny_network_egress = true
"""
        )
        pc = load_project_config(tmp_path)
        assert pc.tool_policy.deny_network_egress is True

    def test_tool_policy_roundtrip_deny_network_egress(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            """
[project]
provider = "test"

[providers.test]
base_url = "https://test.example.com/v1"
api_key_env = "TEST_KEY"

[tool_policy]
deny_network_egress = true
"""
        )
        pc = load_project_config(tmp_path)
        round_tripped = ProjectConfig.from_worker_payload(pc.to_worker_payload())
        assert round_tripped.tool_policy.deny_network_egress is True


class TestScopeIgnoreFiles:
    def test_default_is_empty(self):
        assert ProjectConfig().scope_ignore_files == (".venv", "uv.lock", "__pycache__", "*.pyc")
        assert ProjectConfig().scope_allow_files == ()
        assert ProjectConfig().scope_deny_files == ()

    def test_loads_from_scope_section(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[project]\n\n[scope]\nallow_files = ["src/**"]\n'
            'deny_files = ["src/private/**"]\nignore_files = ["uv.lock", "go.sum"]\n'
        )
        pc = load_project_config(tmp_path)
        assert pc.scope_allow_files == ("src/**",)
        assert pc.scope_deny_files == ("src/private/**",)
        assert pc.scope_ignore_files == (
            ".venv",
            "uv.lock",
            "__pycache__",
            "*.pyc",
            "go.sum",
        )

    @pytest.mark.parametrize("key", ["allow_files", "deny_files"])
    def test_rejects_non_list_scope_path_policy(self, tmp_path, key):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(f'[project]\n\n[scope]\n{key} = "src/**"\n')

        with pytest.raises(ValueError, match=rf"\[scope\]\.{key} must be a list of strings"):
            load_project_config(tmp_path)

    @pytest.mark.parametrize("key", ["allow_files", "deny_files"])
    def test_rejects_non_string_scope_path_policy_entries(self, tmp_path, key):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(f"[project]\n\n[scope]\n{key} = [123]\n")

        with pytest.raises(ValueError, match=rf"\[scope\]\.{key} must be a list of strings"):
            load_project_config(tmp_path)

    def test_rejects_non_list_scope_ignore_files(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\n\n[scope]\nignore_files = "uv.lock"\n')

        with pytest.raises(ValueError, match=r"\[scope\]\.ignore_files must be a list"):
            load_project_config(tmp_path)

    def test_rejects_non_string_scope_ignore_file_entries(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text("[project]\n\n[scope]\nignore_files = [123]\n")

        with pytest.raises(ValueError, match=r"\[scope\]\.ignore_files must be a list"):
            load_project_config(tmp_path)

    def test_scope_ignore_files_is_governor_only(self):
        """scope_ignore_files is consumed by settlement, not the worker, so it
        must not appear in the worker payload. Keeps AtomicConfig minimal."""
        pc = _project_with_provider("test-provider", scope_ignore_files=("uv.lock",))
        payload = pc.to_worker_payload()
        assert "scope_ignore_files" not in payload
        assert "scope_allow_files" not in payload
        assert "scope_deny_files" not in payload
        # After round-trip, scope_ignore_files falls back to default ().
        restored = ProjectConfig.from_worker_payload(payload)
        assert restored.scope_ignore_files == (".venv", "uv.lock", "__pycache__", "*.pyc")
        assert restored.scope_allow_files == ()
        assert restored.scope_deny_files == ()

    def test_rejects_reserved_paths(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[project]\n\n[scope]\nignore_files = [".sentrux/baseline.json"]\n'
        )
        raised = False
        try:
            load_project_config(tmp_path)
        except ValueError as exc:
            raised = True
            assert ".sentrux/baseline.json" in str(exc)
        assert raised, "expected ValueError on reserved path in scope.ignore_files"

    def test_rejects_dgov_sentrux_metadata_scope_ignore(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[project]\n\n[scope]\nignore_files = [".sentrux/dgov-baseline.json"]\n'
        )
        raised = False
        try:
            load_project_config(tmp_path)
        except ValueError as exc:
            raised = True
            assert ".sentrux/dgov-baseline.json" in str(exc)
        assert raised, "expected ValueError on reserved path in scope.ignore_files"

    @pytest.mark.parametrize(
        "entry",
        [
            ".sentrux/",
            ".sentrux/*",
            ".coverage-baseline/*",
            "*.json",
        ],
    )
    def test_rejects_scope_ignore_entries_covering_reserved_paths(self, tmp_path, entry):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            f'[project]\n\n[scope]\nignore_files = ["{entry}"]\n'
        )

        with pytest.raises(ValueError, match="reserved paths"):
            load_project_config(tmp_path)

    def test_reports_scope_ignore_reserved_path_shadow(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(
            '[project]\n\n[scope]\nignore_files = ["*.json", ".sentrux/"]\n'
        )

        with pytest.raises(ValueError) as exc:
            load_project_config(tmp_path)

        message = str(exc.value)
        assert (
            "project.toml [scope] ignore_files cannot include patterns that shadow reserved paths:"
        ) in message
        assert "  '*.json' would match '.coverage-baseline/coverage.json'" in message
        assert "  '.sentrux/' would match '.sentrux/baseline.json'" in message

    def test_missing_section_yields_empty(self, tmp_path):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text('[project]\nlanguage = "python"\n')
        pc = load_project_config(tmp_path)
        assert pc.scope_ignore_files == (".venv", "uv.lock", "__pycache__", "*.pyc")

    @pytest.mark.parametrize(
        "section",
        [
            "project",
            "providers",
            "conventions",
            "tool_policy",
            "sentrux",
            "scope",
            "agents",
            "departments",
        ],
    )
    def test_rejects_malformed_table_section(self, tmp_path, section):
        dgov_dir = tmp_path / ".dgov"
        dgov_dir.mkdir()
        (dgov_dir / "project.toml").write_text(f'{section} = "bad"\n')

        with pytest.raises(ValueError, match=rf"\[{section}\] must be a table"):
            load_project_config(tmp_path)


class TestTypeCheckCommand:
    def test_type_check_cmd_default(self):
        assert ProjectConfig().type_check_cmd is None

    def test_resolve_type_check_cmd(self):
        pc = ProjectConfig(type_check_cmd="ty check")
        assert pc.resolve_type_check_cmd() == "ty check"

    def test_prompt_section_includes_type_check(self):
        pc = ProjectConfig(type_check_cmd="ty check")
        section = pc.to_prompt_section()
        assert "Type check command: ty check" in section

    def test_prompt_section_omits_type_check_when_empty(self):
        pc = ProjectConfig()
        section = pc.to_prompt_section()
        assert "Type check" not in section
