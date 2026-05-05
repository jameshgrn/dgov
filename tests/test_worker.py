"""Tests for worker.py — AtomicTools and helpers.

Worker is a standalone subprocess, so we import it directly and test
the AtomicTools class against real temp directories. No network calls.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# worker.py is a script with `openai` dependency; patch it before import
sys.modules.setdefault("openai", type(sys)("openai"))
sys.modules["openai"].OpenAI = object  # type: ignore

from dgov.tool_policy import ToolPolicy  # noqa: E402
from dgov.worker import (  # noqa: E402
    _build_system_prompt,
    _clip_tool_result,
    _diff_stat_for_error,
    _iteration_budget,
    _load_project_config,
    _repo_map_snapshot,
    _tool_choice_for_iteration,
    run_worker,
)
from dgov.workers.atomic import AtomicConfig, AtomicTools, get_tool_spec  # noqa: E402


@pytest.fixture()
def tools(tmp_path: Path) -> AtomicTools:
    (tmp_path / "hello.py").write_text("x = 1\n")
    return AtomicTools(tmp_path, AtomicConfig())


# -- _check_path --


def test_check_path_valid(tools: AtomicTools) -> None:
    result = tools._check_path("hello.py")
    assert isinstance(result, Path)
    assert result.name == "hello.py"


def test_check_path_traversal_blocked(tools: AtomicTools) -> None:
    result = tools._check_path("../../etc/passwd")
    assert isinstance(result, str)
    assert "traversal" in result.lower()


# -- read_file --


def test_read_file_full(tools: AtomicTools) -> None:
    result = tools.read_file("hello.py")
    assert result == "x = 1\n"


def test_read_file_line_range(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "multi.py").write_text("a\nb\nc\nd\n")
    result = tools.read_file("multi.py", start_line=2, end_line=3)
    assert "2: b" in result
    assert "3: c" in result
    assert "1: a" not in result


def test_read_file_missing(tools: AtomicTools) -> None:
    result = tools.read_file("nope.py")
    assert result.startswith("Error:")


# -- write_file --


def test_write_file(tools: AtomicTools, tmp_path: Path) -> None:
    result = tools.write_file("new.txt", "hello world")
    assert "Successfully" in result
    assert (tmp_path / "new.txt").read_text() == "hello world"


def test_write_file_creates_dirs(tools: AtomicTools, tmp_path: Path) -> None:
    result = tools.write_file("sub/dir/file.txt", "nested")
    assert "Successfully" in result
    assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested"


def test_write_file_rejects_existing_file(tools: AtomicTools) -> None:
    result = tools.write_file("hello.py", "x = 2\n")
    assert result.startswith("Error:")
    assert "edit_file or apply_patch" in result


# -- edit_file --


def test_edit_file_happy(tools: AtomicTools, tmp_path: Path) -> None:
    result = tools.edit_file("hello.py", "x = 1", "x = 2")
    assert "Successfully" in result
    assert (tmp_path / "hello.py").read_text() == "x = 2\n"


def test_edit_file_not_found_text(tools: AtomicTools) -> None:
    result = tools.edit_file("hello.py", "zzz", "aaa")
    assert "not found" in result


def test_edit_file_ambiguous(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "dup.py").write_text("aa\naa\n")
    result = tools.edit_file("dup.py", "aa", "bb")
    assert "matches 2" in result


def test_edit_file_missing(tools: AtomicTools) -> None:
    result = tools.edit_file("nope.py", "a", "b")
    assert result.startswith("Error:")


# -- apply_patch --


def test_apply_patch_simple(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "patch_me.py").write_text("line1\nline2\nline3\n")
    patch = "--- a/patch_me.py\n+++ b/patch_me.py\n@@ -2,1 +2,1 @@\n-line2\n+replaced\n"
    result = tools.apply_patch("patch_me.py", patch)
    assert "Successfully" in result
    assert "replaced" in (tmp_path / "patch_me.py").read_text()


def test_apply_patch_missing_file(tools: AtomicTools) -> None:
    result = tools.apply_patch("nope.py", "@@ -1,1 +1,1 @@\n-a\n+b\n")
    assert result.startswith("Error:")


# -- file_symbols --


def test_file_symbols(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "mod.py").write_text(
        "X = 1\n\ndef foo():\n    pass\n\nclass Bar:\n    def baz(self):\n        pass\n"
    )
    result = tools.file_symbols("mod.py")
    assert "def foo" in result
    assert "class Bar" in result
    assert "def Bar.baz" in result
    assert "X = ..." in result


def test_file_symbols_not_python(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("# hi")
    result = tools.file_symbols("readme.md")
    assert "Error:" in result


def test_file_symbols_syntax_error(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def (broken:\n")
    result = tools.file_symbols("bad.py")
    assert "SyntaxError" in result


# -- check_syntax --


def test_check_syntax_valid(tools: AtomicTools) -> None:
    result = tools.check_syntax("hello.py")
    assert "OK" in result


def test_check_syntax_invalid(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("def (:\n")
    result = tools.check_syntax("bad.py")
    assert "SyntaxError" in result


# -- head / tail --


def test_head(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_text("\n".join(f"line{i}" for i in range(50)))
    result = tools.head("lines.txt", n=5)
    assert "1: line0" in result
    assert "5: line4" in result
    assert "line5" not in result


def test_tail(tools: AtomicTools, tmp_path: Path) -> None:
    (tmp_path / "lines.txt").write_text("\n".join(f"line{i}" for i in range(50)))
    result = tools.tail("lines.txt", n=3)
    assert "line49" in result
    assert "line47" in result
    assert "line46" not in result


def test_head_missing(tools: AtomicTools) -> None:
    result = tools.head("nope.txt")
    assert result.startswith("Error:")


# -- _load_project_config --


def test_load_project_config_defaults(tmp_path: Path) -> None:
    config = _load_project_config(tmp_path)
    assert config.language == "python"
    assert config.test_dir == "tests/"


def test_load_project_config_from_toml(tmp_path: Path) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(
        '[project]\nlanguage = "rust"\nsrc_dir = "src/"\n'
        'test_dir = "tests/"\ntest_markers = ["unit"]\n'
        "worker_iteration_budget = 75\nworker_iteration_warn_at = 60\n"
        "worker_tree_max_lines = 0\n"
    )
    config = _load_project_config(tmp_path)
    assert config.language == "rust"
    assert config.test_markers == ("unit",)
    assert config.worker_iteration_budget == 75
    assert config.worker_iteration_warn_at == 60
    assert config.worker_tree_max_lines == 0


def test_load_project_config_preserves_type_check_and_line_length(tmp_path: Path) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(
        '[project]\ntype_check_cmd = "uv run ty check"\nline_length = 120\n'
    )

    config = _load_project_config(tmp_path)

    assert config.type_check_cmd == "uv run ty check"
    assert config.line_length == 120


def test_load_project_config_tool_policy(tmp_path: Path) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(
        """
[project]

[tool_policy]
restrict_run_bash = true
require_wrapped_verify_tools = true
require_uv_run = true
"""
    )
    config = _load_project_config(tmp_path)
    assert config.tool_policy == ToolPolicy(
        restrict_run_bash=True,
        require_wrapped_verify_tools=True,
        require_uv_run=True,
    )


def test_load_project_config_llm_defaults(tmp_path: Path) -> None:
    config = _load_project_config(tmp_path)
    assert config.llm_base_url == "https://api.fireworks.ai/inference/v1"
    assert config.llm_api_key_env == "FIREWORKS_API_KEY"


def test_load_project_config_llm_from_toml(tmp_path: Path) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(
        '[project]\nllm_base_url = "https://api.openai.com/v1"\n'
        'llm_api_key_env = "OPENAI_API_KEY"\n'
    )
    config = _load_project_config(tmp_path)
    assert config.llm_base_url == "https://api.openai.com/v1"
    assert config.llm_api_key_env == "OPENAI_API_KEY"


# -- get_tool_spec --


def test_get_tool_spec_returns_list() -> None:
    specs = get_tool_spec()
    assert isinstance(specs, list)
    assert len(specs) > 20
    names = {s["function"]["name"] for s in specs}
    assert "read_file" in names
    assert "done" in names
    assert "edit_file" in names


def test_repo_map_snapshot_returns_all_lines_when_unbounded(tmp_path: Path) -> None:
    for idx in range(90):
        (tmp_path / f"file_{idx:03}.txt").write_text("x\n")
    repo_map = _repo_map_snapshot(tmp_path, AtomicConfig(), max_lines=0)
    assert "file_000.txt" in repo_map
    assert "file_089.txt" in repo_map


def test_repo_map_snapshot_truncates_for_prompt_budget(tmp_path: Path) -> None:
    for idx in range(2_000):
        (tmp_path / f"file_{idx:04}.txt").write_text("x\n")
    repo_map = _repo_map_snapshot(tmp_path, AtomicConfig(), max_lines=0, max_chars=300)

    assert "... [repo map truncated for prompt budget]" in repo_map
    assert len(repo_map) <= 300 + len("\n... [repo map truncated for prompt budget]")


def test_clip_tool_result_truncates_large_payload() -> None:
    result = _clip_tool_result("x" * 500, max_chars=120)

    assert "... [tool output truncated for prompt budget]" in result
    assert len(result) <= 120 + len("\n... [tool output truncated for prompt budget]")


def test_build_system_prompt_uses_configured_budget_and_repo_map(tmp_path: Path) -> None:
    for idx in range(90):
        (tmp_path / f"file_{idx:03}.txt").write_text("x\n")
    config = AtomicConfig(
        worker_iteration_budget=75,
        worker_iteration_warn_at=60,
        worker_tree_max_lines=0,
    )
    prompt = _build_system_prompt(tmp_path, config)
    assert "75 tool calls" in prompt
    assert "past call 60" in prompt
    assert "file_089.txt" in prompt


def test_build_system_prompt_uses_repo_map_language(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "mod.py").write_text("def hello():\n    return 1\n")

    prompt = _build_system_prompt(tmp_path, AtomicConfig())

    assert "REPO MAP:" in prompt
    assert "def hello" in prompt
    assert "ast_grep" in prompt


def test_build_system_prompt_injects_task_scope(tmp_path: Path) -> None:
    prompt = _build_system_prompt(
        tmp_path,
        AtomicConfig(),
        {
            "task_slug": "core/main.fix",
            "create": ["src/new.py"],
            "edit": ["src/existing.py"],
            "read": ["tests/test_existing.py"],
            "verify_test_targets": ["tests/test_existing.py"],
        },
    )

    assert "TASK SCOPE:" in prompt
    assert "src/new.py" in prompt
    assert "src/existing.py" in prompt
    assert "tests/test_existing.py" in prompt
    assert "Verification test targets" in prompt
    assert "files.create already exists" in prompt


def test_iteration_budget_clamps_nonpositive_values() -> None:
    assert _iteration_budget(AtomicConfig(worker_iteration_budget=0)) == 1


def test_tool_choice_for_iteration_forces_done_only_near_real_budget_end() -> None:
    assert _tool_choice_for_iteration(iteration=7, budget=10) == "auto"
    assert _tool_choice_for_iteration(iteration=8, budget=10) == {
        "type": "function",
        "function": {"name": "done"},
    }
    assert _tool_choice_for_iteration(iteration=1, budget=2) == "auto"


def test_diff_stat_for_error_summarizes_worktree_changes(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "tracked.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "tracked.py").write_text("x = 2\n")

    summary = _diff_stat_for_error(tmp_path)

    assert "tracked.py" in summary


def test_run_worker_uses_configured_iteration_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    events: list[tuple[str, object]] = []
    call_count = 0

    class _FakeMessage:
        content = None

        def __init__(self) -> None:
            self.tool_calls = []

        def model_dump(self, exclude_none: bool = True):
            return {"role": "assistant"}

    class _FakeOpenAI:
        def __init__(self, *args, **kwargs):
            completions = SimpleNamespace(create=self._create)
            self.chat = SimpleNamespace(completions=completions)

        def _create(self, **kwargs):
            nonlocal call_count
            call_count += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=_FakeMessage(), finish_reason="length")],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

    monkeypatch.setattr("dgov.worker.OpenAI", _FakeOpenAI)
    monkeypatch.setattr(
        "dgov.worker.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )

    with pytest.raises(SystemExit) as excinfo:
        run_worker(
            "do it",
            tmp_path,
            "test-model",
            json.dumps({"worker_iteration_budget": 2}),
        )

    assert excinfo.value.code == 1
    assert call_count == 2
    assert events[-1][0] == "error"
    assert str(events[-1][1]).startswith("Exceeded max iterations (2)")


def test_run_worker_forces_done_near_iteration_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FIREWORKS_API_KEY", "test-key")
    events: list[tuple[str, object]] = []
    tool_choices: list[object] = []

    class _FakeMessage:
        content = None

        def __init__(self, tool_calls=None) -> None:
            self.tool_calls = tool_calls or []

        def model_dump(self, exclude_none: bool = True):
            data = {"role": "assistant"}
            if self.tool_calls:
                data["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in self.tool_calls
                ]
            return data

    class _FakeOpenAI:
        def __init__(self, *args, **kwargs):
            completions = SimpleNamespace(create=self._create)
            self.chat = SimpleNamespace(completions=completions)

        def _create(self, **kwargs):
            tool_choice = kwargs["tool_choice"]
            tool_choices.append(tool_choice)
            tool_calls = []
            if isinstance(tool_choice, dict):
                tool_calls = [
                    SimpleNamespace(
                        id="call-1",
                        function=SimpleNamespace(
                            name="done",
                            arguments=json.dumps({"summary": "Reached finalization handoff."}),
                        ),
                    )
                ]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=_FakeMessage(tool_calls), finish_reason="length")
                ],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

    monkeypatch.setattr("dgov.worker.OpenAI", _FakeOpenAI)
    monkeypatch.setattr(
        "dgov.worker.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )

    with pytest.raises(SystemExit) as excinfo:
        run_worker(
            "do it",
            tmp_path,
            "test-model",
            json.dumps({"worker_iteration_budget": 10, "worker_iteration_warn_at": 8}),
        )

    assert excinfo.value.code == 0
    assert tool_choices[-1] == {"type": "function", "function": {"name": "done"}}
    assert ("done", "Reached finalization handoff.") in events
