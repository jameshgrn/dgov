"""Unit tests for dgov.agents — agent registry and command building."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.agents import (
    AGENT_REGISTRY,
    AgentDef,
    _perm_flags,
    _prompt_read_and_delete_snippet,
    _write_prompt_file,
    build_launch_command,
    detect_installed_agents,
    load_registry,
)

pytestmark = pytest.mark.unit


class TestAgentDef:
    def test_frozen(self) -> None:
        agent = AGENT_REGISTRY["claude"]
        with pytest.raises(AttributeError):
            agent.id = "modified"  # type: ignore[misc]

    def test_required_fields(self) -> None:
        agent = AgentDef(
            id="test",
            name="Test Agent",
            short_label="ta",
            prompt_command="test-cli",
            prompt_transport="positional",
        )
        assert agent.id == "test"
        assert agent.prompt_option is None
        assert agent.permission_flags == {}
        assert agent.default_flags == ""

    def test_new_fields_have_defaults(self) -> None:
        agent = AgentDef(
            id="test",
            name="Test",
            short_label="t",
            prompt_command="test",
            prompt_transport="positional",
        )
        assert agent.health_check is None
        assert agent.health_fix is None
        assert agent.max_concurrent is None
        assert agent.color is None
        assert agent.env == {}
        assert agent.source == "built-in"


# ---------------------------------------------------------------------------
# AGENT_REGISTRY (all built-ins)
# ---------------------------------------------------------------------------

_ALL_BUILTIN_IDS = {
    "claude",
    "codex",
    "gemini",
    "opencode",
    "cline",
    "qwen",
    "amp",
    "pi",
    "cursor",
    "copilot",
    "crush",
}


class TestAgentRegistry:
    def test_known_agents(self) -> None:
        assert set(AGENT_REGISTRY.keys()) == _ALL_BUILTIN_IDS

    def test_claude_transport(self) -> None:
        assert AGENT_REGISTRY["claude"].prompt_transport == "positional"

    def test_gemini_transport(self) -> None:
        assert AGENT_REGISTRY["gemini"].prompt_transport == "option"
        assert AGENT_REGISTRY["gemini"].prompt_option == "--prompt-interactive"

    def test_builtin_colors(self) -> None:
        assert AGENT_REGISTRY["claude"].color == 39
        assert AGENT_REGISTRY["codex"].color == 214
        assert AGENT_REGISTRY["gemini"].color == 135

    def test_new_agent_colors(self) -> None:
        assert AGENT_REGISTRY["opencode"].color == 82
        assert AGENT_REGISTRY["cline"].color == 196
        assert AGENT_REGISTRY["qwen"].color == 99
        assert AGENT_REGISTRY["amp"].color == 208
        assert AGENT_REGISTRY["pi"].color == 34
        assert AGENT_REGISTRY["cursor"].color == 45
        assert AGENT_REGISTRY["copilot"].color == 231
        assert AGENT_REGISTRY["crush"].color == 219

    def test_new_agent_transports(self) -> None:
        assert AGENT_REGISTRY["opencode"].prompt_transport == "option"
        assert AGENT_REGISTRY["cline"].prompt_transport == "send-keys"
        assert AGENT_REGISTRY["qwen"].prompt_transport == "option"
        assert AGENT_REGISTRY["amp"].prompt_transport == "stdin"
        assert AGENT_REGISTRY["pi"].prompt_transport == "positional"
        assert AGENT_REGISTRY["cursor"].prompt_transport == "positional"
        assert AGENT_REGISTRY["copilot"].prompt_transport == "option"
        assert AGENT_REGISTRY["crush"].prompt_transport == "send-keys"

    def test_crush_send_keys_config(self) -> None:
        crush = AGENT_REGISTRY["crush"]
        assert crush.send_keys_pre_prompt == ("Escape", "Tab")
        assert crush.send_keys_post_paste_delay_ms == 200
        assert crush.send_keys_ready_delay_ms == 1200
        assert crush.no_prompt_command == "crush"

    def test_cline_send_keys_config(self) -> None:
        cline = AGENT_REGISTRY["cline"]
        assert cline.send_keys_post_paste_delay_ms == 120
        assert cline.send_keys_ready_delay_ms == 2500


# ---------------------------------------------------------------------------
# load_registry
# ---------------------------------------------------------------------------


class TestLoadRegistry:
    def test_returns_builtins_with_no_config(self, tmp_path: Path) -> None:
        registry = load_registry(str(tmp_path))
        assert set(registry.keys()) >= _ALL_BUILTIN_IDS

    def test_project_config_overrides_safe_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Project-local config can override safe fields (color, max_concurrent)
        on existing built-in agents, but unsafe fields (command, default_flags,
        env) are stripped for security."""
        # Isolate from user's real ~/.dgov/agents.toml
        monkeypatch.setattr("dgov.agents.Path.home", lambda: tmp_path / "fakehome")
        config_dir = tmp_path / ".dgov"
        config_dir.mkdir()
        (config_dir / "agents.toml").write_text(
            "[agents.pi]\n"
            'command = "evil"\n'
            'default_flags = "--provider river-gpu0"\n'
            "color = 34\n"
            "max_concurrent = 2\n"
        )
        registry = load_registry(str(tmp_path))
        assert "pi" in registry
        assert registry["pi"].color == 34
        assert registry["pi"].max_concurrent == 2
        assert registry["pi"].source == "project"
        # Unsafe fields stripped — command and default_flags unchanged from builtin
        assert registry["pi"].prompt_command == "pi"
        assert registry["pi"].default_flags == "-p"

    def test_project_config_overrides_builtin(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".dgov"
        config_dir.mkdir()
        (config_dir / "agents.toml").write_text("[agents.claude]\ncolor = 99\n")
        registry = load_registry(str(tmp_path))
        assert registry["claude"].color == 99
        assert registry["claude"].source == "project"
        # Other fields preserved from built-in
        assert registry["claude"].prompt_command == "claude"
        assert registry["claude"].prompt_transport == "positional"

    def test_user_config_adds_agent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.agents.Path.home", lambda: tmp_path)
        user_config = tmp_path / ".dgov"
        user_config.mkdir(parents=True)
        (user_config / "agents.toml").write_text(
            "[agents.aider]\n"
            'command = "aider"\n'
            'transport = "positional"\n'
            'health_check = "aider --version"\n'
        )
        registry = load_registry(None)
        assert "aider" in registry
        assert registry["aider"].source == "user"
        assert registry["aider"].health_check == "aider --version"

    def test_env_and_permissions_from_toml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # User-global config (not project-local) can define commands and env
        monkeypatch.setattr("dgov.agents.Path.home", lambda: tmp_path)
        config_dir = tmp_path / ".dgov"
        config_dir.mkdir()
        (config_dir / "agents.toml").write_text(
            "[agents.custom]\n"
            'command = "custom-cli"\n'
            'transport = "positional"\n'
            "\n"
            "[agents.custom.permissions]\n"
            'plan = "--read-only"\n'
            "\n"
            "[agents.custom.env]\n"
            'CUDA_VISIBLE_DEVICES = "0,1"\n'
            "\n"
            "[agents.custom.resume]\n"
            'template = "custom-cli --resume{permissions}"\n'
        )
        registry = load_registry(None)
        agent = registry["custom"]
        assert agent.permission_flags == {"plan": "--read-only"}
        assert agent.env == {"CUDA_VISIBLE_DEVICES": "0,1"}
        assert agent.resume_template == "custom-cli --resume{permissions}"

    def test_project_overrides_user(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.agents.Path.home", lambda: tmp_path)
        user_config = tmp_path / ".dgov"
        user_config.mkdir(parents=True)
        (user_config / "agents.toml").write_text(
            '[agents.pi]\ncommand = "pi"\ntransport = "positional"\ncolor = 34\n'
        )
        project_root = tmp_path / "project"
        project_root.mkdir()
        (project_root / ".dgov").mkdir()
        (project_root / ".dgov" / "agents.toml").write_text("[agents.pi]\ncolor = 99\n")
        registry = load_registry(str(project_root))
        assert registry["pi"].color == 99
        assert registry["pi"].source == "project"


# ---------------------------------------------------------------------------
# _perm_flags
# ---------------------------------------------------------------------------


class TestPermFlags:
    def test_empty_mode(self) -> None:
        assert _perm_flags(AGENT_REGISTRY["claude"], "") == ""

    def test_known_mode(self) -> None:
        result = _perm_flags(AGENT_REGISTRY["claude"], "bypassPermissions")
        assert "--dangerously-skip-permissions" in result

    def test_unknown_mode(self) -> None:
        assert _perm_flags(AGENT_REGISTRY["claude"], "nonexistent") == ""

    def test_codex_full_auto(self) -> None:
        result = _perm_flags(AGENT_REGISTRY["codex"], "acceptEdits")
        assert "--full-auto" in result


# ---------------------------------------------------------------------------
# _write_prompt_file / _prompt_read_and_delete_snippet
# ---------------------------------------------------------------------------


class TestPromptFile:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        filepath = _write_prompt_file(str(tmp_path), "my-task", "Do the thing")
        assert Path(filepath).exists()
        assert Path(filepath).read_text() == "Do the thing"

    def test_write_creates_prompts_dir(self, tmp_path: Path) -> None:
        filepath = _write_prompt_file(str(tmp_path), "slug", "prompt")
        assert (tmp_path / ".dgov" / "prompts").is_dir()
        assert "slug" in Path(filepath).name

    def test_snippet_format(self) -> None:
        snippet = _prompt_read_and_delete_snippet("/tmp/prompt.txt")
        assert "DGOV_PROMPT_FILE" in snippet
        assert "DGOV_PROMPT_CONTENT" in snippet
        assert "rm -f" in snippet
        assert "/tmp/prompt.txt" in snippet


# ---------------------------------------------------------------------------
# build_launch_command
# ---------------------------------------------------------------------------


class TestBuildLaunchCommand:
    def test_positional_with_prompt(self, tmp_path: Path) -> None:
        cmd = build_launch_command(
            "claude", "Fix the bug", project_root=str(tmp_path), slug="fix-bug"
        )
        assert "claude" in cmd
        assert "DGOV_PROMPT_CONTENT" in cmd

    def test_no_prompt_returns_base(self) -> None:
        cmd = build_launch_command("claude", None)
        assert cmd == "claude"

    def test_no_prompt_uses_no_prompt_command(self) -> None:
        cmd = build_launch_command("claude", None)
        assert "claude" in cmd

    def test_permission_mode_added(self, tmp_path: Path) -> None:
        cmd = build_launch_command(
            "claude",
            "prompt",
            permission_mode="bypassPermissions",
            project_root=str(tmp_path),
        )
        assert "--dangerously-skip-permissions" in cmd

    def test_extra_flags(self, tmp_path: Path) -> None:
        cmd = build_launch_command(
            "claude",
            "prompt",
            extra_flags="--verbose",
            project_root=str(tmp_path),
        )
        assert "--verbose" in cmd

    def test_option_transport(self, tmp_path: Path) -> None:
        cmd = build_launch_command("gemini", "Do stuff", project_root=str(tmp_path), slug="gem")
        assert "--prompt-interactive" in cmd
        assert "DGOV_PROMPT_CONTENT" in cmd

    def test_with_custom_registry(self, tmp_path: Path) -> None:
        custom_reg = {
            "pi": AgentDef(
                id="pi",
                name="pi",
                short_label="pi",
                prompt_command="pi",
                prompt_transport="positional",
                default_flags="--provider river-gpu0",
            )
        }
        cmd = build_launch_command(
            "pi", "Do stuff", project_root=str(tmp_path), slug="pi-task", registry=custom_reg
        )
        assert "--provider river-gpu0" in cmd

    def test_unknown_agent_raises(self) -> None:
        with pytest.raises(KeyError):
            build_launch_command("nonexistent", "prompt")


# ---------------------------------------------------------------------------
# detect_installed_agents
# ---------------------------------------------------------------------------


class TestDetectInstalledAgents:
    def test_none_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("dgov.agents.shutil.which", lambda _: None)
        assert detect_installed_agents() == []

    def test_some_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_which(cmd):
            return "/usr/bin/claude" if cmd == "claude" else None

        monkeypatch.setattr("dgov.agents.shutil.which", fake_which)
        installed = detect_installed_agents()
        assert "claude" in installed

    def test_with_custom_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_which(cmd):
            return "/usr/bin/pi" if cmd == "pi" else None

        monkeypatch.setattr("dgov.agents.shutil.which", fake_which)
        custom_reg = {
            "pi": AgentDef(
                id="pi",
                name="pi",
                short_label="pi",
                prompt_command="pi",
                prompt_transport="positional",
            )
        }
        installed = detect_installed_agents(custom_reg)
        assert "pi" in installed


class TestAllAgentsHaveCorrectTransport:
    def test_all_agents_have_valid_transport(self) -> None:
        valid_transports = {"positional", "option", "send-keys", "stdin"}
        for agent_id, agent_def in AGENT_REGISTRY.items():
            assert agent_def.prompt_transport in valid_transports, (
                f"Invalid transport for {agent_id}: {agent_def.prompt_transport}"
            )

    def test_all_agents_have_permission_flags(self) -> None:
        for agent_id, agent_def in AGENT_REGISTRY.items():
            assert hasattr(agent_def, "permission_flags")
            assert isinstance(agent_def.permission_flags, dict)


class TestAllRegistryEntriesHaveRequiredFields:
    def test_all_registry_entries_have_required_fields(self) -> None:
        assert set(AGENT_REGISTRY.keys()) == _ALL_BUILTIN_IDS

        for agent_id, agent_def in AGENT_REGISTRY.items():
            assert isinstance(agent_def, AgentDef)
            assert hasattr(agent_def, "id") and agent_def.id == agent_id
            assert hasattr(agent_def, "name") and isinstance(agent_def.name, str)
            assert hasattr(agent_def, "prompt_command") and isinstance(
                agent_def.prompt_command, str
            )


class TestBuildLaunchCommandOption:
    def test_build_launch_command_option(self, tmp_path: Path) -> None:
        agent = AGENT_REGISTRY["gemini"]
        assert agent.prompt_transport == "option"
        assert agent.prompt_option == "--prompt-interactive"

        cmd = build_launch_command(
            agent_id="gemini",
            prompt="test prompt content",
            permission_mode="",
            project_root=str(tmp_path),
            slug="task2",
        )

        assert "gemini" in cmd
        assert "--prompt-interactive" in cmd
        assert "$DGOV_PROMPT_CONTENT" in cmd


class TestBuildLaunchCommandPositional:
    def test_build_launch_command_positional(self, tmp_path: Path) -> None:
        agent = AGENT_REGISTRY["claude"]
        assert agent.prompt_transport == "positional"

        cmd = build_launch_command(
            agent_id="claude",
            prompt="test prompt content",
            permission_mode="",
            project_root=str(tmp_path),
            slug="task1",
        )

        assert "claude" in cmd
        assert "$DGOV_PROMPT_CONTENT" in cmd
        assert 'cat "$DGOV_PROMPT_FILE"' in cmd
        assert 'rm -f "$DGOV_PROMPT_FILE"' in cmd


class TestBuildLaunchCommandSendKeysAgent:
    def test_send_keys_agent_ignores_prompt_file(self, tmp_path: Path) -> None:
        cmd = build_launch_command(
            agent_id="claude",
            prompt="ignored for send-keys",
            permission_mode="",
            project_root=str(tmp_path),
            slug="task",
        )
        # claude uses positional, so prompt file IS used
        assert "$DGOV_PROMPT_FILE" in cmd


class TestBuildLaunchCommandSendkeys:
    def test_build_launch_command_sendkeys(self, tmp_path: Path) -> None:
        cmd = build_launch_command(
            agent_id="claude",
            prompt="test prompt content",
            permission_mode="",
            project_root=str(tmp_path),
            slug="task3",
        )
        assert "claude" in cmd
        assert "$DGOV_PROMPT_FILE" in cmd
        assert "$DGOV_PROMPT_CONTENT" in cmd


class TestBuildLaunchCommandWithExtraFlags:
    def test_build_launch_command_with_extra_flags(self, tmp_path: Path) -> None:
        cmd = build_launch_command(
            agent_id="claude",
            prompt="test",
            permission_mode="",
            project_root=str(tmp_path),
            slug="task5",
            extra_flags="--verbose --timeout 300",
        )
        assert "--verbose" in cmd
        assert "--timeout" in cmd
        assert "300" in cmd


class TestBuildLaunchCommandWithPermissions:
    def test_build_launch_command_with_permissions(self, tmp_path: Path) -> None:
        cmd_bypass = build_launch_command(
            agent_id="claude",
            prompt="test",
            permission_mode="bypassPermissions",
            project_root=str(tmp_path),
            slug="task4a",
        )
        assert "--dangerously-skip-permissions" in cmd_bypass

        cmd_accept = build_launch_command(
            agent_id="claude",
            prompt="test",
            permission_mode="acceptEdits",
            project_root=str(tmp_path),
            slug="task4b",
        )
        assert "--permission-mode acceptEdits" in cmd_accept

        cmd_plan = build_launch_command(
            agent_id="claude",
            prompt="test",
            permission_mode="plan",
            project_root=str(tmp_path),
            slug="task4c",
        )
        assert "--permission-mode plan" in cmd_plan

        cmd_gemini_yolo = build_launch_command(
            agent_id="gemini",
            prompt="test",
            permission_mode="bypassPermissions",
            project_root=str(tmp_path),
            slug="task4d",
        )
        assert "--approval-mode yolo" in cmd_gemini_yolo


class TestCommandTemplateRendering:
    def test_claude_command_without_prompt(self, tmp_path: Path) -> None:
        cmd = build_launch_command(
            agent_id="claude",
            prompt=None,
            permission_mode="",
            project_root=str(tmp_path),
            slug="task-noprompt",
        )
        assert cmd.startswith("claude")
        assert "$DGOV_PROMPT_FILE" not in cmd
        assert "$DGOV_PROMPT_CONTENT" not in cmd

    def test_gemini_command_without_prompt(self, tmp_path: Path) -> None:
        cmd = build_launch_command(
            agent_id="gemini",
            prompt=None,
            permission_mode="",
            project_root=str(tmp_path),
            slug="task-gemini-noprompt",
        )
        assert "gemini" in cmd
        assert "$DGOV_PROMPT_FILE" not in cmd

    def test_different_slugs_produce_different_filenames(self, tmp_path: Path) -> None:
        filepath1 = _write_prompt_file(
            project_root=str(tmp_path), slug="task-foo", prompt="prompt A"
        )
        filepath2 = _write_prompt_file(
            project_root=str(tmp_path), slug="task-bar", prompt="prompt B"
        )
        assert Path(filepath1).name.startswith("task-foo--")
        assert Path(filepath2).name.startswith("task-bar--")
        assert filepath1 != filepath2

    def test_prompt_content_written_correctly(self, tmp_path: Path) -> None:
        test_prompts = [
            "simple text",
            "prompt with spaces and more words",
            "multiline\nprompt\ntest",
            "special chars: !@#$%^&*()",
        ]
        for prompt in test_prompts:
            filepath = _write_prompt_file(
                project_root=str(tmp_path), slug="test-special", prompt=prompt
            )
            content = Path(filepath).read_text(encoding="utf-8")
            assert content == prompt, f"Prompt mismatch for: {prompt[:20]}..."

    def test_codex_command_with_permissions(self, tmp_path: Path) -> None:
        cmd = build_launch_command(
            agent_id="codex",
            prompt="test",
            permission_mode="acceptEdits",
            project_root=str(tmp_path),
            slug="task-codex",
        )
        assert "codex" in cmd
        assert "--full-auto" in cmd


class TestPermFlagsUnknownMode:
    def test_perm_flags_unknown_mode(self) -> None:
        agent = AGENT_REGISTRY["claude"]
        result = _perm_flags(agent, "unknownMode")
        assert result == ""
        result_empty = _perm_flags(agent, "")
        assert result_empty == ""


class TestUnknownAgentName:
    def test_unknown_agent_id_raises_keyerror(self) -> None:
        assert "unknown-agent" not in AGENT_REGISTRY
        with pytest.raises(KeyError):
            build_launch_command(
                agent_id="unknown-agent",
                prompt="test",
                permission_mode="",
                project_root="/tmp",
                slug="task",
            )


class TestWritePromptFile:
    def test_write_prompt_file(self, tmp_path: Path) -> None:
        prompt = "this is my test prompt"
        filepath = _write_prompt_file(project_root=str(tmp_path), slug="test-slug", prompt=prompt)

        expected_path = tmp_path / ".dgov" / "prompts"
        assert Path(filepath).parent == expected_path
        assert Path(filepath).exists()
        content = Path(filepath).read_text(encoding="utf-8")
        assert content == prompt

        filename = Path(filepath).name
        assert filename.startswith("test-slug--")
        assert filename.endswith(".txt")
        assert len(filename.split("--")[1].split("-")[0]) > 0
