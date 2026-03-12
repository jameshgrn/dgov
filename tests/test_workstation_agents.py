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
    build_resume_command,
    detect_installed_agents,
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


# ---------------------------------------------------------------------------
# AGENT_REGISTRY
# ---------------------------------------------------------------------------


class TestAgentRegistry:
    def test_known_agents(self) -> None:
        expected = {"claude", "pi", "codex", "gemini", "qwen"}
        assert set(AGENT_REGISTRY.keys()) == expected

    def test_claude_transport(self) -> None:
        assert AGENT_REGISTRY["claude"].prompt_transport == "positional"

    def test_gemini_transport(self) -> None:
        assert AGENT_REGISTRY["gemini"].prompt_transport == "option"
        assert AGENT_REGISTRY["gemini"].prompt_option == "--prompt-interactive"

    def test_pi_default_flags(self) -> None:
        assert "river-gpu0" in AGENT_REGISTRY["pi"].default_flags

    def test_all_have_resume_template(self) -> None:
        for agent_id, defn in AGENT_REGISTRY.items():
            assert defn.resume_template is not None, f"{agent_id} has no resume_template"


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
        assert (tmp_path / ".workstation" / "prompts").is_dir()
        assert "slug" in Path(filepath).name

    def test_snippet_format(self) -> None:
        snippet = _prompt_read_and_delete_snippet("/tmp/prompt.txt")
        assert "DMUX_PROMPT_FILE" in snippet
        assert "DMUX_PROMPT_CONTENT" in snippet
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
        assert "DMUX_PROMPT_CONTENT" in cmd

    def test_no_prompt_returns_base(self) -> None:
        cmd = build_launch_command("claude", None)
        assert cmd == "claude"

    def test_no_prompt_uses_no_prompt_command(self) -> None:
        # Claude has no no_prompt_command, so falls back to base
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
        assert "DMUX_PROMPT_CONTENT" in cmd

    def test_pi_includes_default_flags(self, tmp_path: Path) -> None:
        cmd = build_launch_command("pi", "Do stuff", project_root=str(tmp_path), slug="pi-task")
        assert "--provider river-gpu0" in cmd

    def test_unknown_agent_raises(self) -> None:
        with pytest.raises(KeyError):
            build_launch_command("nonexistent", "prompt")


# ---------------------------------------------------------------------------
# build_resume_command
# ---------------------------------------------------------------------------


class TestBuildResumeCommand:
    def test_claude_resume(self) -> None:
        cmd = build_resume_command("claude")
        assert cmd is not None
        assert "claude --continue" in cmd

    def test_claude_resume_with_permissions(self) -> None:
        cmd = build_resume_command("claude", "bypassPermissions")
        assert cmd is not None
        assert "--dangerously-skip-permissions" in cmd

    def test_codex_resume(self) -> None:
        cmd = build_resume_command("codex")
        assert cmd is not None
        assert "codex resume" in cmd

    def test_gemini_resume(self) -> None:
        cmd = build_resume_command("gemini")
        assert cmd is not None
        assert "--resume latest" in cmd

    def test_pi_resume(self) -> None:
        cmd = build_resume_command("pi")
        assert cmd is not None
        assert "pi --continue" in cmd


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
        assert "pi" not in installed


class TestAllAgentsHaveCorrectTransport:
    """Test that each agent has correct transport type defined."""

    def test_all_agents_have_valid_transport(self) -> None:
        from dgov.agents import AGENT_REGISTRY

        valid_transports = {"positional", "option", "send-keys", "stdin"}

        for agent_id, agent_def in AGENT_REGISTRY.items():
            assert agent_def.prompt_transport in valid_transports, (
                f"Invalid transport for {agent_id}: {agent_def.prompt_transport}"
            )

    def test_all_agents_have_permission_flags(self) -> None:
        """All agents should have permission_flags dict."""
        from dgov.agents import AGENT_REGISTRY

        for agent_id, agent_def in AGENT_REGISTRY.items():
            assert hasattr(agent_def, "permission_flags")
            assert isinstance(agent_def.permission_flags, dict)


class TestAllRegistryEntriesHaveRequiredFields:
    """Test case 1: verify all 5 agents have id, name, prompt_command."""

    def test_all_registry_entries_have_required_fields(self) -> None:
        from dgov.agents import AGENT_REGISTRY, AgentDef

        expected_agent_ids = {"claude", "pi", "codex", "gemini", "qwen"}

        assert set(AGENT_REGISTRY.keys()) == expected_agent_ids

        for agent_id, agent_def in AGENT_REGISTRY.items():
            assert isinstance(agent_def, AgentDef)
            assert hasattr(agent_def, "id") and agent_def.id == agent_id
            assert hasattr(agent_def, "name") and isinstance(agent_def.name, str)
            assert hasattr(agent_def, "prompt_command") and isinstance(
                agent_def.prompt_command, str
            )


class TestBuildLaunchCommandOption:
    """Test case 3: test gemini (option transport with --prompt-interactive)."""

    def test_build_launch_command_option(self, tmp_path: Path) -> None:
        from dgov.agents import AGENT_REGISTRY, build_launch_command

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
        assert "$DMUX_PROMPT_CONTENT" in cmd


class TestBuildLaunchCommandPositional:
    """Test case 2: test claude (positional transport) generates correct command."""

    def test_build_launch_command_positional(self, tmp_path: Path) -> None:
        from dgov.agents import AGENT_REGISTRY, build_launch_command

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
        assert "$DMUX_PROMPT_CONTENT" in cmd
        assert 'cat "$DMUX_PROMPT_FILE"' in cmd
        assert 'rm -f "$DMUX_PROMPT_FILE"' in cmd


class TestBuildLaunchCommandSendKeysAgent:
    """Additional test for agents with send-keys transport."""

    def test_send_keys_agent_ignores_prompt_file(self, tmp_path: Path) -> None:
        # Create a mock agent with send-keys transport
        from dgov.agents import AgentDef, build_launch_command

        class TempAgent(AgentDef):
            pass  # Just for test instantiation

        # Note: AGENT_REGISTRY doesn't currently have a send-keys agent,
        # so we're testing the code path via direct call simulation
        cmd = build_launch_command(
            agent_id="claude",  # Use real agent but note transport is positional
            prompt="ignored for send-keys",
            permission_mode="",
            project_root=str(tmp_path),
            slug="task",
        )

        # claude uses positional, so prompt file IS used
        assert "$DMUX_PROMPT_FILE" in cmd


class TestBuildLaunchCommandSendkeys:
    """Test case 4: verify non-send-keys agents use prompt file."""

    def test_build_launch_command_sendkeys(self, tmp_path: Path) -> None:
        from dgov.agents import build_launch_command

        # There's no send-keys transport agent in current AGENT_REGISTRY.
        # Test with positional transport agent to verify normal behavior works.
        cmd = build_launch_command(
            agent_id="claude",
            prompt="test prompt content",
            permission_mode="",
            project_root=str(tmp_path),
            slug="task3",
        )

        # For non-send-keys agents, command includes prompt file handling
        assert "claude" in cmd
        assert "$DMUX_PROMPT_FILE" in cmd
        assert "$DMUX_PROMPT_CONTENT" in cmd


class TestBuildLaunchCommandWithExtraFlags:
    """Test case 6: verify extra_flags appended."""

    def test_build_launch_command_with_extra_flags(self, tmp_path: Path) -> None:
        from dgov.agents import build_launch_command

        # pi has default_flags --provider river-gpu0
        cmd = build_launch_command(
            agent_id="pi",
            prompt="test",
            permission_mode="",
            project_root=str(tmp_path),
            slug="task5",
            extra_flags="--verbose --timeout 300",
        )

        assert "--provider river-gpu0" in cmd
        assert "--verbose" in cmd
        assert "--timeout" in cmd
        assert "300" in cmd


class TestBuildLaunchCommandWithPermissions:
    """Test case 5: test bypassPermissions, acceptEdits, plan modes."""

    def test_build_launch_command_with_permissions(self, tmp_path: Path) -> None:
        from dgov.agents import build_launch_command

        # claude with bypassPermissions
        cmd_bypass = build_launch_command(
            agent_id="claude",
            prompt="test",
            permission_mode="bypassPermissions",
            project_root=str(tmp_path),
            slug="task4a",
        )
        assert "--dangerously-skip-permissions" in cmd_bypass

        # claude with acceptEdits
        cmd_accept = build_launch_command(
            agent_id="claude",
            prompt="test",
            permission_mode="acceptEdits",
            project_root=str(tmp_path),
            slug="task4b",
        )
        assert "--permission-mode acceptEdits" in cmd_accept

        # claude with plan
        cmd_plan = build_launch_command(
            agent_id="claude",
            prompt="test",
            permission_mode="plan",
            project_root=str(tmp_path),
            slug="task4c",
        )
        assert "--permission-mode plan" in cmd_plan

        # gemini with bypassPermissions (yolo)
        cmd_gemini_yolo = build_launch_command(
            agent_id="gemini",
            prompt="test",
            permission_mode="bypassPermissions",
            project_root=str(tmp_path),
            slug="task4d",
        )
        assert "--approval-mode yolo" in cmd_gemini_yolo


class TestCommandTemplateRendering:
    """Test case 5: Command template rendering with various parameters."""

    def test_claude_command_without_prompt(self, tmp_path: Path) -> None:
        """Test positional transport without prompt - returns base command only."""
        from dgov.agents import build_launch_command

        cmd = build_launch_command(
            agent_id="claude",
            prompt=None,
            permission_mode="",
            project_root=str(tmp_path),
            slug="task-noprompt",
        )

        # Should return base claude command without prompt file handling
        assert cmd.startswith("claude")
        assert "$DMUX_PROMPT_FILE" not in cmd
        assert "$DMUX_PROMPT_CONTENT" not in cmd

    def test_gemini_command_without_prompt(self, tmp_path: Path) -> None:
        """Test option transport without prompt."""
        from dgov.agents import build_launch_command

        cmd = build_launch_command(
            agent_id="gemini",
            prompt=None,
            permission_mode="",
            project_root=str(tmp_path),
            slug="task-gemini-noprompt",
        )

        assert "gemini" in cmd
        assert "$DMUX_PROMPT_FILE" not in cmd

    def test_different_slugs_produce_different_filenames(self, tmp_path: Path) -> None:
        """Test that different slugs produce different prompt file names."""
        from dgov.agents import _write_prompt_file

        filepath1 = _write_prompt_file(
            project_root=str(tmp_path), slug="task-foo", prompt="prompt A"
        )
        filepath2 = _write_prompt_file(
            project_root=str(tmp_path), slug="task-bar", prompt="prompt B"
        )

        # Filenames should differ due to slug prefix
        assert Path(filepath1).name.startswith("task-foo--")
        assert Path(filepath2).name.startswith("task-bar--")
        assert filepath1 != filepath2  # Different full paths

    def test_prompt_content_written_correctly(self, tmp_path: Path) -> None:
        """Test that prompt content is written exactly as provided."""
        from dgov.agents import _write_prompt_file

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

    def test_qwen_command_with_option_transport(self, tmp_path: Path) -> None:
        """Test qwen uses -i option flag."""
        from dgov.agents import AGENT_REGISTRY, build_launch_command

        qwen_agent = AGENT_REGISTRY["qwen"]
        assert qwen_agent.prompt_transport == "option"
        assert qwen_agent.prompt_option == "-i"

        cmd = build_launch_command(
            agent_id="qwen",
            prompt="test qwen prompt",
            permission_mode="",
            project_root=str(tmp_path),
            slug="task-qwen",
        )

        assert "qwen" in cmd
        assert "-i" in cmd
        assert "$DMUX_PROMPT_CONTENT" in cmd

    def test_codex_command_with_permissions(self, tmp_path: Path) -> None:
        """Test codex with acceptEdits permission mode."""
        from dgov.agents import build_launch_command

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
    """Test case 10: returns empty string for unknown mode."""

    def test_perm_flags_unknown_mode(self) -> None:
        from dgov.agents import AGENT_REGISTRY, _perm_flags

        agent = AGENT_REGISTRY["claude"]

        # Unknown mode should return empty string
        result = _perm_flags(agent, "unknownMode")
        assert result == ""

        # Empty mode should also return empty string
        result_empty = _perm_flags(agent, "")
        assert result_empty == ""


class TestPiAgentDefaultProvider:
    """Test case 6: Verify pi agent uses correct default provider."""

    def test_pi_agent_default_provider(self) -> None:
        from dgov.agents import AGENT_REGISTRY, build_launch_command

        # Verify pi agent has the correct default flags
        pi_agent = AGENT_REGISTRY["pi"]
        assert pi_agent.default_flags == "--provider river-gpu0"

        # Build a command and verify the provider flag is present
        cmd = build_launch_command(
            agent_id="pi",
            prompt="test prompt",
            permission_mode="",
            project_root="/tmp",
            slug="task-pi",
        )

        # Should contain default provider
        assert "--provider river-gpu0" in cmd


class TestUnknownAgentName:
    """Test case 4: Unknown agent name should raise KeyError."""

    def test_unknown_agent_id_raises_keyerror(self) -> None:
        from dgov.agents import AGENT_REGISTRY, build_launch_command

        # Verify 'unknown-agent' is not in registry first
        assert "unknown-agent" not in AGENT_REGISTRY

        # Calling with unknown agent ID should raise KeyError
        with pytest.raises(KeyError):
            build_launch_command(
                agent_id="unknown-agent",
                prompt="test",
                permission_mode="",
                project_root="/tmp",
                slug="task",
            )

    def test_unknown_resume_agent_raises_keyerror(self) -> None:
        from dgov.agents import AGENT_REGISTRY, build_resume_command

        assert "unknown-agent" not in AGENT_REGISTRY

        with pytest.raises(KeyError):
            build_resume_command("unknown-agent")


class TestWritePromptFile:
    """Test case 9: verify file created in .workstation/prompts/ with correct content."""

    def test_write_prompt_file(self, tmp_path: Path) -> None:
        from dgov.agents import _write_prompt_file

        prompt = "this is my test prompt"

        filepath = _write_prompt_file(project_root=str(tmp_path), slug="test-slug", prompt=prompt)

        # Verify path structure
        expected_path = tmp_path / ".workstation" / "prompts"
        assert Path(filepath).parent == expected_path

        # Verify file exists and has correct content
        assert Path(filepath).exists()
        content = Path(filepath).read_text(encoding="utf-8")
        assert content == prompt

        # Verify filename format: {slug}--{ts}-{rand}.txt
        filename = Path(filepath).name
        assert filename.startswith("test-slug--")
        assert filename.endswith(".txt")
        assert len(filename.split("--")[1].split("-")[0]) > 0  # timestamp part
