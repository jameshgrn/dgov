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
from typing import Any, cast

import pytest

# worker.py is a script with `openai` dependency; patch it before import
sys.modules.setdefault("openai", type(sys)("openai"))
sys.modules["openai"].OpenAI = object  # type: ignore

from dgov.tool_policy import ToolPolicy  # noqa: E402
from dgov.worker import _build_system_prompt, run_worker  # noqa: E402
from dgov.workers.atomic import AtomicTools, get_tool_spec  # noqa: E402
from dgov.workers.config import AtomicConfig  # noqa: E402
from dgov.workers.runtime import (  # noqa: E402
    _validate_plan,
    clip_tool_result,
    diff_stat_for_error,
    execute_tool_call,
    iteration_budget,
    load_project_config,
    repo_map_snapshot,
    tool_choice_for_iteration,
)

pytestmark = pytest.mark.unit


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


def test_check_path_blocks_sibling_prefix_escape(tmp_path: Path) -> None:
    worktree = tmp_path / "repo"
    sibling = tmp_path / "repo_evil"
    worktree.mkdir()
    sibling.mkdir()
    tools = AtomicTools(worktree, AtomicConfig())

    result = tools.write_file("../repo_evil/owned.txt", "escaped\n")

    assert result.startswith("Error:")
    assert not (sibling / "owned.txt").exists()


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
    config = load_project_config(tmp_path)
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
    config = load_project_config(tmp_path)
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

    config = load_project_config(tmp_path)

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
    config = load_project_config(tmp_path)
    assert config.tool_policy == ToolPolicy(
        restrict_run_bash=True,
        require_wrapped_verify_tools=True,
        require_uv_run=True,
    )


def test_load_project_config_llm_defaults(tmp_path: Path) -> None:
    config = load_project_config(tmp_path)
    assert config.llm_provider == ""
    assert config.llm_base_url == ""
    assert config.llm_api_key_env == ""


def test_load_project_config_llm_from_toml(tmp_path: Path) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(
        '[project]\nprovider = "test-provider"\n'
        "\n[providers.test-provider]\n"
        'base_url = "https://provider.test/v1"\n'
        'api_key_env = "TEST_PROVIDER_API_KEY"\n'
    )
    config = load_project_config(tmp_path)
    assert config.llm_provider == "test-provider"
    assert config.llm_base_url == "https://provider.test/v1"
    assert config.llm_api_key_env == "TEST_PROVIDER_API_KEY"


def test_load_project_config_llm_from_named_provider(tmp_path: Path) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(
        """
[project]
provider = "openai"

[providers.openai]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
"""
    )
    config = load_project_config(tmp_path)
    assert config.llm_provider == "openai"
    assert config.llm_base_url == "https://api.openai.com/v1"
    assert config.llm_api_key_env == "OPENAI_API_KEY"


@pytest.mark.parametrize(
    "section",
    [
        "project",
        "providers",
        "conventions",
        "tool_policy",
    ],
)
def test_load_project_config_rejects_malformed_table_section(tmp_path: Path, section: str) -> None:
    dgov_dir = tmp_path / ".dgov"
    dgov_dir.mkdir()
    (dgov_dir / "project.toml").write_text(f'{section} = "bad"\n')

    with pytest.raises(ValueError, match=rf"\[{section}\] must be a table"):
        load_project_config(tmp_path)


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
    repo_map = repo_map_snapshot(tmp_path, AtomicConfig(), max_lines=0)
    assert "file_000.txt" in repo_map
    assert "file_089.txt" in repo_map


def test_repo_map_snapshot_truncates_for_prompt_budget(tmp_path: Path) -> None:
    for idx in range(2_000):
        (tmp_path / f"file_{idx:04}.txt").write_text("x\n")
    repo_map = repo_map_snapshot(tmp_path, AtomicConfig(), max_lines=0, max_chars=300)

    assert "... [repo map truncated for prompt budget]" in repo_map
    assert len(repo_map) <= 300 + len("\n... [repo map truncated for prompt budget]")


def test_clip_tool_result_truncates_large_payload() -> None:
    result = clip_tool_result("x" * 500, max_chars=120)

    assert "... [tool output truncated for prompt budget]" in result
    assert len(result) <= 120 + len("\n... [tool output truncated for prompt budget]")


def _valid_emit_plan_task(slug: str = "task-a") -> dict[str, object]:
    return {
        "slug": slug,
        "prompt": "Orient:\nRead.\n\nEdit:\n1. Change.\n\nVerify:\n- Check.",
        "commit_message": "Change task",
        "files": {"edit": ["src/example.py"]},
    }


def test_validate_plan_accepts_worker_task() -> None:
    assert _validate_plan({"tasks": [_valid_emit_plan_task()]}) is None


def test_validate_plan_rejects_worker_without_file_claim() -> None:
    task = _valid_emit_plan_task()
    task["files"] = {"read": ["src/example.py"]}

    error = _validate_plan({"tasks": [task]})

    assert error is not None
    assert "must claim at least one file" in error


def test_validate_plan_rejects_unknown_dependency() -> None:
    task = _valid_emit_plan_task()
    task["depends_on"] = ["missing"]

    error = _validate_plan({"tasks": [task]})

    assert error == "Error: Task 'task-a' depends on unknown slug 'missing'."


def test_validate_plan_rejects_invalid_dependency_shape() -> None:
    task = _valid_emit_plan_task()
    task["depends_on"] = "task-b"

    error = _validate_plan({"tasks": [task]})

    assert error == "Error: Task 'task-a' has invalid depends_on. Must be a list."


def test_validate_plan_rejects_dependency_cycle() -> None:
    task_a = _valid_emit_plan_task("task-a")
    task_b = _valid_emit_plan_task("task-b")
    task_a["depends_on"] = ["task-b"]
    task_b["depends_on"] = ["task-a"]

    error = _validate_plan({"tasks": [task_a, task_b]})

    assert error is not None
    assert "Dependency cycle detected" in error


def _tool_call(name: str, args: dict[str, object], call_id: str = "call-1") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def test_execute_tool_call_emits_success_telemetry(
    tools: AtomicTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "dgov.workers.runtime.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )

    result, is_done = execute_tool_call(
        _tool_call("read_file", {"path": "hello.py"}),
        tools,
        role="worker",
        turn_index=2,
        tool_index=1,
    )

    assert "x = 1" in result
    assert is_done is False
    call_event = events[0][1]
    result_event = events[1][1]
    assert isinstance(call_event, dict)
    call_event = cast(dict[str, Any], call_event)
    assert call_event["tool"] == "read_file"
    assert call_event["args"] == {"path": "hello.py"}
    assert call_event["arg_keys"] == ["path"]
    assert call_event["call_id"] == "call-1"
    assert call_event["role"] == "worker"
    assert call_event["turn_index"] == 2
    assert call_event["tool_index"] == 1
    assert isinstance(result_event, dict)
    result_event = cast(dict[str, Any], result_event)
    assert result_event["tool"] == "read_file"
    assert result_event["status"] == "success"
    assert result_event["call_id"] == "call-1"
    assert result_event["result_chars"] == len(result)
    assert result_event["raw_result_chars"] == len(result)
    assert result_event["result_clipped"] is False
    assert isinstance(result_event["duration_ms"], float)
    assert result_event["duration_ms"] >= 0


def test_execute_tool_call_emits_failed_telemetry(
    tools: AtomicTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "dgov.workers.runtime.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )

    result, is_done = execute_tool_call(
        _tool_call("read_file", {"path": "missing.py"}),
        tools,
        role="worker",
        turn_index=1,
        tool_index=3,
    )

    assert result.startswith("Error:")
    assert is_done is False
    result_event = events[1][1]
    assert isinstance(result_event, dict)
    result_event = cast(dict[str, Any], result_event)
    assert result_event["status"] == "failed"
    assert result_event["error_kind"] == "not_found"
    assert result_event["role"] == "worker"
    assert result_event["turn_index"] == 1
    assert result_event["tool_index"] == 3


def test_execute_tool_call_reports_invalid_json_arguments(
    tools: AtomicTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "dgov.workers.runtime.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )
    call = SimpleNamespace(
        id="call-bad-json",
        function=SimpleNamespace(name="read_file", arguments="{not-json"),
    )

    result, is_done = execute_tool_call(
        call,
        tools,
        role="worker",
        turn_index=3,
        tool_index=2,
    )

    assert result.startswith("Error: Tool read_file arguments contain invalid JSON")
    assert is_done is False
    call_event = cast(dict[str, Any], events[0][1])
    result_event = cast(dict[str, Any], events[1][1])
    assert call_event["arg_keys"] == []
    assert result_event["status"] == "failed"
    assert result_event["error_kind"] == "validation_failed"


def test_execute_tool_call_rejects_non_object_json_arguments(
    tools: AtomicTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "dgov.workers.runtime.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )
    call = SimpleNamespace(
        id="call-array-json",
        function=SimpleNamespace(name="read_file", arguments='["hello.py"]'),
    )

    result, is_done = execute_tool_call(call, tools, role="worker")

    assert result == "Error: Tool read_file arguments must be a JSON object."
    assert is_done is False
    result_event = cast(dict[str, Any], events[1][1])
    assert result_event["status"] == "failed"
    assert result_event["error_kind"] == "validation_failed"


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
            "scope_allow_files": ["src/**", "tests/**"],
            "scope_deny_files": ["src/private/**"],
            "verify_test_targets": ["tests/test_existing.py"],
            "require_successful_test_verification": True,
            "required_verification_command": "uv run pytest tests/test_existing.py -q",
        },
    )

    assert "TASK SCOPE:" in prompt
    assert "src/new.py" in prompt
    assert "src/existing.py" in prompt
    assert "tests/test_existing.py" in prompt
    assert "Project path allowlist" in prompt
    assert "Project path denylist" in prompt
    assert "Verification test targets" in prompt
    assert "Retry completion gate" in prompt
    assert "uv run pytest tests/test_existing.py -q" in prompt
    assert "files.create already exists" in prompt
    assert "scope_status" in prompt


def test_done_is_blocked_until_required_retry_tests_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(
        "dgov.workers.runtime.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )
    call = _tool_call("done", {"summary": "fixed"})
    actuators = AtomicTools(
        tmp_path,
        AtomicConfig(test_cmd="true {test_dir}", test_dir="tests/"),
        task_scope={
            "verify_test_targets": ["tests/test_existing.py"],
            "require_successful_test_verification": True,
            "required_verification_command": "uv run pytest tests/test_existing.py -q",
        },
    )

    result, is_done = execute_tool_call(call, actuators, allowed_tools=frozenset({"done"}))

    assert is_done is False
    assert result.startswith("Error:")
    assert "requires a successful run_tests() call" in result
    result_event = events[1][1]
    assert isinstance(result_event, dict)
    result_event = cast(dict[str, Any], result_event)
    assert result_event["tool"] == "done"
    assert result_event["status"] == "failed"
    assert result_event["error_kind"] == "validation_failed"


def test_successful_retry_tests_unlock_done(tmp_path: Path) -> None:
    call = _tool_call("done", {"summary": "fixed"})
    actuators = AtomicTools(
        tmp_path,
        AtomicConfig(test_cmd="true {test_dir}", test_dir="tests/"),
        task_scope={
            "verify_test_targets": ["tests/test_existing.py"],
            "require_successful_test_verification": True,
        },
    )

    assert "EXIT:0" in actuators.run_tests()
    result, is_done = execute_tool_call(call, actuators, allowed_tools=frozenset({"done"}))

    assert is_done is True
    assert result == "fixed"


def test_iteration_budget_clamps_nonpositive_values() -> None:
    assert iteration_budget(AtomicConfig(worker_iteration_budget=0)) == 1


def test_tool_choice_for_iteration_forces_done_only_near_real_budget_end() -> None:
    assert tool_choice_for_iteration(iteration=7, budget=10) == "auto"
    assert tool_choice_for_iteration(iteration=8, budget=10) == {
        "type": "function",
        "function": {"name": "done"},
    }
    assert tool_choice_for_iteration(iteration=1, budget=2) == "auto"


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

    summary = diff_stat_for_error(tmp_path)

    assert "tracked.py" in summary


def _provider_payload(**overrides: object) -> str:
    payload: dict[str, object] = {
        "llm_provider": "test-provider",
        "llm_base_url": "https://provider.test/v1",
        "llm_api_key_env": "TEST_PROVIDER_API_KEY",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_run_worker_uses_configured_iteration_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "test-key")
    events: list[tuple[str, object]] = []
    call_count = 0

    class _FakeMessage:
        content = None

        def __init__(self) -> None:
            self.tool_calls = []

        def model_dump(self, exclude_none: bool = True):
            return {"role": "assistant"}

    class _FakeProvider:
        def create_chat_completion(self, **kwargs):
            nonlocal call_count
            call_count += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(message=_FakeMessage(), finish_reason="length")],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

    monkeypatch.setattr("dgov.worker.create_provider", lambda **_kwargs: _FakeProvider())
    monkeypatch.setattr(
        "dgov.workers.runtime.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )

    with pytest.raises(SystemExit) as excinfo:
        run_worker(
            "do it",
            tmp_path,
            "test-model",
            _provider_payload(worker_iteration_budget=2),
        )

    assert excinfo.value.code == 1
    assert call_count == 2
    assert events[-1][0] == "error"
    assert str(events[-1][1]).startswith("Exceeded max iterations (2)")


def test_run_worker_reports_invalid_project_config_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []
    _capture_events(monkeypatch, events)

    with pytest.raises(SystemExit) as excinfo:
        run_worker("do it", tmp_path, "test-model", "{not-json")

    assert excinfo.value.code == 1
    assert events == [
        (
            "error",
            "Project configuration error: Invalid worker project config JSON: "
            "Expecting property name enclosed in double quotes",
        )
    ]


def test_run_worker_reports_invalid_task_scope_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []
    _capture_events(monkeypatch, events)

    with pytest.raises(SystemExit) as excinfo:
        run_worker("do it", tmp_path, "test-model", "", "{not-json")

    assert excinfo.value.code == 1
    assert events == [
        (
            "error",
            "Task scope error: Invalid task scope JSON: "
            "Expecting property name enclosed in double quotes",
        )
    ]


def test_run_worker_reports_non_object_task_scope_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, object]] = []
    _capture_events(monkeypatch, events)

    with pytest.raises(SystemExit) as excinfo:
        run_worker("do it", tmp_path, "test-model", "", '["x.py"]')

    assert excinfo.value.code == 1
    assert events == [
        ("error", "Task scope error: Invalid task scope payload: expected a JSON object")
    ]


def _make_fake_message(tool_calls: list[SimpleNamespace] | None = None) -> SimpleNamespace:
    """Build a fake message response with optional tool calls."""
    data: dict[str, object] = {"role": "assistant"}
    if tool_calls:
        data["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in tool_calls
        ]
    return SimpleNamespace(content=None, tool_calls=tool_calls or [], model_dump=lambda **_: data)


def _make_fake_provider(
    tool_choices: list[object],
    events: list[tuple[str, object]],
    done_summary: str = "Reached finalization handoff.",
) -> type:
    """Build a fake provider class that captures tool choices and emits done on forced choice."""

    class _FakeProvider:
        def create_chat_completion(self, **kwargs):
            tool_choice = kwargs["tool_choice"]
            tool_choices.append(tool_choice)
            tool_calls: list[SimpleNamespace] = []
            if isinstance(tool_choice, dict):
                tool_calls = [
                    SimpleNamespace(
                        id="call-1",
                        function=SimpleNamespace(
                            name="done",
                            arguments=json.dumps({"summary": done_summary}),
                        ),
                    )
                ]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=_make_fake_message(tool_calls), finish_reason="length")
                ],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            )

    return _FakeProvider


def _capture_events(monkeypatch: pytest.MonkeyPatch, events: list[tuple[str, object]]) -> None:
    """Monkeypatch WorkerEvent.emit to capture events for assertion."""
    monkeypatch.setattr(
        "dgov.workers.runtime.WorkerEvent.emit",
        lambda self: events.append((self.type, self.content)),
    )


def test_run_worker_forces_done_near_iteration_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_API_KEY", "test-key")
    events: list[tuple[str, object]] = []
    tool_choices: list[object] = []

    _FakeProvider = _make_fake_provider(tool_choices, events)
    monkeypatch.setattr("dgov.worker.create_provider", lambda **_kwargs: _FakeProvider())
    _capture_events(monkeypatch, events)

    with pytest.raises(SystemExit) as excinfo:
        run_worker(
            "do it",
            tmp_path,
            "test-model",
            _provider_payload(worker_iteration_budget=10, worker_iteration_warn_at=8),
        )

    assert excinfo.value.code == 0
    assert tool_choices[-1] == {"type": "function", "function": {"name": "done"}}
    assert ("done", "Reached finalization handoff.") in events
