"""Tests for dgov/plan.py module.

Tests cover:
- parse_plan_file(path) - reads TOML, returns PlanSpec
- compile_plan(plan) - converts PlanSpec to DagDefinition
- validate_plan(plan) - structural validation
- _paths_overlap(path, touch) - path overlap detection
- _normalize_touch_path(path) - path normalization
"""

import pytest

from dgov.plan import (
    PlanSpec,
    PlanUnit,
    PlanUnitFiles,
    PlanValidationError,
    _normalize_touch_path,
    _paths_overlap,
    compile_plan,
    parse_plan_file,
    validate_plan,
)
from dgov.types import ConstitutionalViolation

# A prompt that passes structure validation for tests that don't care about prompt content.
_VALID_PROMPT = "Orient:\nContext.\n\nEdit:\n1. Change.\n\nVerify:\n- Check."

# =============================================================================
# _normalize_touch_path tests
# =============================================================================


class TestNormalizeTouchPath:
    def test_strips_leading_dot_slash(self):
        assert _normalize_touch_path("./file.txt") == "file.txt"
        assert _normalize_touch_path("./src/main.py") == "src/main.py"

    def test_strips_trailing_slash(self):
        assert _normalize_touch_path("src/") == "src"
        assert _normalize_touch_path("some/path/") == "some/path"

    def test_strips_whitespace(self):
        assert _normalize_touch_path("  file.txt  ") == "file.txt"

    def test_handles_multiple_prefixes_and_suffixes(self):
        assert _normalize_touch_path("./src/") == "src"
        assert _normalize_touch_path("././nested/path///") == "nested/path"

    def test_handles_empty_string(self):
        assert _normalize_touch_path("") == ""

    def test_handles_plain_paths(self):
        assert _normalize_touch_path("file.txt") == "file.txt"
        assert _normalize_touch_path("src/main.py") == "src/main.py"


# =============================================================================
# _paths_overlap tests
# =============================================================================


class TestPathsOverlap:
    def test_identical_paths_overlap(self):
        assert _paths_overlap("src/main.py", "src/main.py") is True
        assert _paths_overlap("./src/main.py", "src/main.py") is True

    def test_parent_child_overlap(self):
        assert _paths_overlap("src/main.py", "src") is True
        assert _paths_overlap("src", "src/main.py") is True

    def test_nested_directory_overlap(self):
        assert _paths_overlap("src/utils/helper.py", "src") is True

    def test_sibling_paths_no_overlap(self):
        assert _paths_overlap("src/main.py", "src/utils.py") is False

    def test_unrelated_paths_no_overlap(self):
        assert _paths_overlap("src/main.py", "tests/test_main.py") is False

    def test_similar_prefix_no_overlap(self):
        assert _paths_overlap("src_main.py", "src/main.py") is False

    def test_empty_path_no_overlap(self):
        assert _paths_overlap("", "src/main.py") is False
        assert _paths_overlap("src/main.py", "") is False

    def test_normalizes_before_comparison(self):
        assert _paths_overlap("./src/main.py", "src/") is True


# =============================================================================
# parse_plan_file tests
# =============================================================================


class TestParsePlanFile:
    def test_parses_minimal_plan_file(self, tmp_path):
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "test-plan"

[tasks.test-unit]
summary = "A test unit"
prompt = "Do something"
commit_message = "Add test feature"
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        assert isinstance(result, PlanSpec)
        assert result.name == "test-plan"
        assert "test-unit" in result.units
        unit = result.units["test-unit"]
        assert unit.slug == "test-unit"
        assert unit.summary == "A test unit"
        assert unit.prompt == "Do something"
        assert unit.commit_message == "Add test feature"

    def test_parses_plan_with_files(self, tmp_path):
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "file-plan"

[tasks.create-files]
summary = "Create some files"
prompt = "Create files"
commit_message = "Add files"

[tasks.create-files.files]
create = ["src/new.py", "tests/new_test.py"]
edit = ["src/existing.py"]
delete = ["old_file.py"]
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        unit = result.units["create-files"]
        assert unit.files.create == ("src/new.py", "tests/new_test.py")
        assert unit.files.edit == ("src/existing.py",)
        assert unit.files.delete == ("old_file.py",)

    def test_parses_plan_with_read_only_files(self, tmp_path):
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "read-plan"

[tasks.inspect]
summary = "Inspect state"
prompt = "Read the implementation before editing docs"
commit_message = "docs: record findings"

[tasks.inspect.files]
read = ["src/core.py", "docs/spec.md"]
edit = ["README.md"]
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        unit = result.units["inspect"]
        assert unit.files.read == ("src/core.py", "docs/spec.md")
        assert unit.files.edit == ("README.md",)

    def test_parses_plan_with_dependencies(self, tmp_path):
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "dep-plan"

[tasks.setup]
summary = "Setup"
prompt = "Do setup"
commit_message = "Setup done"

[tasks.main-task]
summary = "Main task"
prompt = "Do main task"
commit_message = "Main done"
depends_on = ["setup"]
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        assert result.units["main-task"].depends_on == ("setup",)

    def test_parses_plan_with_agent_settings(self, tmp_path):
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "agent-plan"

[tasks.custom-agent]
summary = "Custom agent task"
prompt = "Do something"
commit_message = "Done"
agent = "claude-3"
timeout_s = 1200
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        unit = result.units["custom-agent"]
        assert unit.agent == "claude-3"
        assert unit.timeout_s == 1200

    def test_parses_plan_with_task_test_override(self, tmp_path):
        plan_file = tmp_path / "plan.toml"
        plan_file.write_text(
            """
[plan]
name = "test-override-plan"

[tasks.custom-tests]
summary = "Override tests"
prompt = "Do it"
commit_message = "Done"
test_cmd = "./scripts/qgis-python.sh -m pytest tests/plugin/test_task.py"
"""
        )

        result = parse_plan_file(str(plan_file))

        assert (
            result.units["custom-tests"].test_cmd
            == "./scripts/qgis-python.sh -m pytest tests/plugin/test_task.py"
        )

    def test_parses_plan_with_task_iteration_budget(self, tmp_path):
        plan_file = tmp_path / "plan.toml"
        plan_file.write_text(
            """
[plan]
name = "iteration-budget-plan"

[tasks.focused]
summary = "Stay focused"
prompt = "Do it"
commit_message = "Done"
iteration_budget = 12
"""
        )

        result = parse_plan_file(str(plan_file))

        assert result.units["focused"].iteration_budget == 12

    def test_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_plan_file(str(tmp_path / "does_not_exist.toml"))

    def test_parses_multiple_units(self, tmp_path):
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "multi-plan"

[tasks.first]
summary = "First task"
prompt = "First"
commit_message = "First done"

[tasks.second]
summary = "Second task"
prompt = "Second"
commit_message = "Second done"

[tasks.third]
summary = "Third task"
prompt = "Third"
commit_message = "Third done"
depends_on = ["first", "second"]
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        assert len(result.units) == 3


# =============================================================================
# compile_plan tests
# =============================================================================


class TestCompilePlan:
    def test_compiles_minimal_plan(self):
        plan = PlanSpec(
            name="simple-plan",
            goal="Simple goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task one",
                    prompt="Do task one",
                    commit_message="Task 1 done",
                    files=PlanUnitFiles(),
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        assert result.name == "simple-plan"
        assert "task-1" in result.tasks
        task = result.tasks["task-1"]
        assert task.slug == "task-1"
        assert task.prompt == "Do task one"

    def test_uses_default_agent_when_unit_has_none(self):
        plan = PlanSpec(
            name="agent-plan",
            goal="Agent goal",
            default_agent="qwen-35b",
            default_timeout_s=600,
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    agent="",
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        assert result.tasks["task-1"].agent == "qwen-35b"

    def test_preserves_unit_agent_when_specified(self):
        plan = PlanSpec(
            name="custom-agent-plan",
            goal="Goal",
            default_agent="qwen-35b",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    agent="claude-3-opus",
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        assert result.tasks["task-1"].agent == "claude-3-opus"

    def test_fails_unauthorized_department_edit(self):
        plan = PlanSpec(
            name="constitution",
            goal="Goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Fix kernel",
                    prompt=_VALID_PROMPT,
                    commit_message="Done",
                    files=PlanUnitFiles(edit=("src/dgov/kernel.py",)),
                )
            },
        )

        with pytest.raises(
            ConstitutionalViolation,
            match="Constitutional violation: unit touches",
        ):
            compile_plan(
                plan,
                project_agent="test-agent",
                departments={"Core": ["src/dgov/kernel.py"]},
            )

    def test_fails_unauthorized_department_touch(self):
        plan = PlanSpec(
            name="constitution",
            goal="Goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Fix kernel",
                    prompt=_VALID_PROMPT,
                    commit_message="Done",
                    files=PlanUnitFiles(touch=("src/dgov/kernel.py",)),
                )
            },
        )

        with pytest.raises(
            ConstitutionalViolation,
            match="Constitutional violation: unit touches",
        ):
            compile_plan(
                plan,
                project_agent="test-agent",
                departments={"Core": ["src/dgov/kernel.py"]},
            )

    def test_allows_authorized_department_edit(self):
        plan = PlanSpec(
            name="constitution",
            goal="Goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Core: fix kernel",
                    prompt=_VALID_PROMPT,
                    commit_message="Done",
                    files=PlanUnitFiles(edit=("src/dgov/kernel.py",)),
                )
            },
        )

        result = compile_plan(
            plan,
            project_agent="test-agent",
            departments={"Core": ["src/dgov/kernel.py"]},
        )

        assert result.tasks["task-1"].files.edit == ("src/dgov/kernel.py",)

    def test_preserves_unit_role_when_specified(self):
        plan = PlanSpec(
            name="research-role-plan",
            goal="Goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task",
                    prompt="Investigate it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    role="researcher",
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        assert result.tasks["task-1"].role == "researcher"

    def test_preserves_unit_iteration_budget_when_specified(self):
        plan = PlanSpec(
            name="iteration-budget-plan",
            goal="Goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    iteration_budget=12,
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        assert result.tasks["task-1"].iteration_budget == 12

    def test_uses_default_timeout_when_unit_has_none(self):
        plan = PlanSpec(
            name="timeout-plan",
            goal="Goal",
            default_timeout_s=1200,
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    timeout_s=0,
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        assert result.tasks["task-1"].timeout_s == 1200

    def test_preserves_dependencies(self):
        plan = PlanSpec(
            name="dep-plan",
            goal="Goal",
            units={
                "task-2": PlanUnit(
                    slug="task-2",
                    summary="Task 2",
                    prompt="Do task 2",
                    commit_message="Task 2 done",
                    files=PlanUnitFiles(),
                    depends_on=("task-1",),
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        assert result.tasks["task-2"].depends_on == ("task-1",)

    def test_rejects_unknown_task_role(self):
        plan = PlanSpec(
            name="bad-role-plan",
            goal="Goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    # Intentionally invalid literal to exercise the runtime
                    # validator in compile_plan.
                    role="mystery",  # ty: ignore[invalid-argument-type]
                )
            },
        )

        with pytest.raises(PlanValidationError, match="Unknown task role"):
            compile_plan(plan, project_agent="test-agent")

    def test_preserves_file_operations(self):
        plan = PlanSpec(
            name="file-plan",
            goal="Goal",
            units={
                "file-task": PlanUnit(
                    slug="file-task",
                    summary="File task",
                    prompt="Handle files",
                    commit_message="Files handled",
                    files=PlanUnitFiles(
                        create=("new.py",),
                        edit=("existing.py",),
                        delete=("old.py",),
                    ),
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        task = result.tasks["file-task"]
        assert task.files.create == ("new.py",)
        assert task.files.edit == ("existing.py",)
        assert task.files.delete == ("old.py",)

    def test_preserves_read_only_files(self):
        plan = PlanSpec(
            name="read-plan",
            goal="Goal",
            units={
                "file-task": PlanUnit(
                    slug="file-task",
                    summary="File task",
                    prompt="Inspect files",
                    commit_message="Docs handled",
                    files=PlanUnitFiles(
                        read=("src/core.py", "docs/spec.md"),
                        edit=("README.md",),
                    ),
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        task = result.tasks["file-task"]
        assert task.files.read == ("src/core.py", "docs/spec.md")
        assert task.all_touches() == ("README.md",)

    def test_preserves_touch_through_compile(self):
        plan = PlanSpec(
            name="touch-plan",
            goal="Goal",
            units={
                "task": PlanUnit(
                    slug="task",
                    summary="Touch task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(touch=("src/foo.py", "tests/test_foo.py")),
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        task = result.tasks["task"]
        assert task.files.touch == ("src/foo.py", "tests/test_foo.py")
        assert task.files.create == ()
        assert task.files.edit == ()

    def test_compiles_multiple_units(self):
        plan = PlanSpec(
            name="multi-plan",
            goal="Goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task 1",
                    prompt="Do 1",
                    commit_message="Done 1",
                    files=PlanUnitFiles(),
                ),
                "task-2": PlanUnit(
                    slug="task-2",
                    summary="Task 2",
                    prompt="Do 2",
                    commit_message="Done 2",
                    files=PlanUnitFiles(),
                ),
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        assert len(result.tasks) == 2

    def test_preserves_project_and_session_root(self):
        plan = PlanSpec(
            name="path-plan",
            goal="Goal",
            project_root="/home/user/project",
            session_root="/tmp/session",
            units={
                "task": PlanUnit(
                    slug="task",
                    summary="Task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                )
            },
        )

        result = compile_plan(plan, project_agent="test-agent")

        assert result.project_root == "/home/user/project"
        assert result.session_root == "/tmp/session"


# =============================================================================
# validate_plan tests
# =============================================================================


class TestValidatePlan:
    def test_returns_empty_list_for_valid_plan(self):
        plan = PlanSpec(
            name="valid-plan",
            goal="Goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task",
                    prompt=_VALID_PROMPT,
                    commit_message="Done",
                    files=PlanUnitFiles(),
                )
            },
        )
        assert validate_plan(plan) == []

    def test_detects_file_conflict_between_independent_tasks(self):
        plan = PlanSpec(
            name="conflict-plan",
            goal="Goal",
            units={
                "a": PlanUnit(
                    slug="a",
                    summary="A",
                    prompt=_VALID_PROMPT,
                    commit_message="A",
                    files=PlanUnitFiles(edit=("src/main.py",)),
                ),
                "b": PlanUnit(
                    slug="b",
                    summary="B",
                    prompt=_VALID_PROMPT,
                    commit_message="B",
                    files=PlanUnitFiles(edit=("src/main.py",)),
                ),
            },
        )
        issues = validate_plan(plan)
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "src/main.py" in issues[0].message

    def test_no_conflict_when_dependent(self):
        plan = PlanSpec(
            name="dep-plan",
            goal="Goal",
            units={
                "a": PlanUnit(
                    slug="a",
                    summary="A",
                    prompt=_VALID_PROMPT,
                    commit_message="A",
                    files=PlanUnitFiles(edit=("src/main.py",)),
                ),
                "b": PlanUnit(
                    slug="b",
                    summary="B",
                    prompt=_VALID_PROMPT,
                    commit_message="B",
                    files=PlanUnitFiles(edit=("src/main.py",)),
                    depends_on=("a",),
                ),
            },
        )
        assert validate_plan(plan) == []

    def test_no_conflict_different_files(self):
        plan = PlanSpec(
            name="no-conflict",
            goal="Goal",
            units={
                "a": PlanUnit(
                    slug="a",
                    summary="A",
                    prompt=_VALID_PROMPT,
                    commit_message="A",
                    files=PlanUnitFiles(edit=("src/foo.py",)),
                ),
                "b": PlanUnit(
                    slug="b",
                    summary="B",
                    prompt=_VALID_PROMPT,
                    commit_message="B",
                    files=PlanUnitFiles(edit=("src/bar.py",)),
                ),
            },
        )
        assert validate_plan(plan) == []

    def test_detects_directory_overlap(self):
        plan = PlanSpec(
            name="dir-overlap",
            goal="Goal",
            units={
                "a": PlanUnit(
                    slug="a",
                    summary="A",
                    prompt=_VALID_PROMPT,
                    commit_message="A",
                    files=PlanUnitFiles(edit=("src/utils/helper.py",)),
                ),
                "b": PlanUnit(
                    slug="b",
                    summary="B",
                    prompt=_VALID_PROMPT,
                    commit_message="B",
                    files=PlanUnitFiles(delete=("src/utils",)),
                ),
            },
        )
        issues = validate_plan(plan)
        assert len(issues) >= 1
        assert issues[0].severity == "error"

    def test_transitive_dependency_not_conflict(self):
        plan = PlanSpec(
            name="transitive",
            goal="Goal",
            units={
                "a": PlanUnit(
                    slug="a",
                    summary="A",
                    prompt=_VALID_PROMPT,
                    commit_message="A",
                    files=PlanUnitFiles(edit=("src/main.py",)),
                ),
                "b": PlanUnit(
                    slug="b",
                    summary="B",
                    prompt=_VALID_PROMPT,
                    commit_message="B",
                    files=PlanUnitFiles(edit=("src/other.py",)),
                    depends_on=("a",),
                ),
                "c": PlanUnit(
                    slug="c",
                    summary="C",
                    prompt=_VALID_PROMPT,
                    commit_message="C",
                    files=PlanUnitFiles(edit=("src/main.py",)),
                    depends_on=("b",),
                ),
            },
        )
        assert validate_plan(plan) == []

    def test_compile_rejects_conflicting_plan(self):
        plan = PlanSpec(
            name="bad-plan",
            goal="Goal",
            units={
                "a": PlanUnit(
                    slug="a",
                    summary="A",
                    prompt=_VALID_PROMPT,
                    commit_message="A",
                    files=PlanUnitFiles(edit=("src/main.py",)),
                ),
                "b": PlanUnit(
                    slug="b",
                    summary="B",
                    prompt=_VALID_PROMPT,
                    commit_message="B",
                    files=PlanUnitFiles(edit=("src/main.py",)),
                ),
            },
        )
        with pytest.raises(PlanValidationError) as exc_info:
            compile_plan(plan, project_agent="test-agent")
        assert len(exc_info.value.issues) == 1

    def test_detects_touch_conflict_between_independent_tasks(self):
        plan = PlanSpec(
            name="touch-conflict",
            goal="Goal",
            units={
                "a": PlanUnit(
                    slug="a",
                    summary="A",
                    prompt=_VALID_PROMPT,
                    commit_message="A",
                    files=PlanUnitFiles(touch=("src/main.py",)),
                ),
                "b": PlanUnit(
                    slug="b",
                    summary="B",
                    prompt=_VALID_PROMPT,
                    commit_message="B",
                    files=PlanUnitFiles(touch=("src/main.py",)),
                ),
            },
        )
        issues = validate_plan(plan)
        assert len(issues) == 1
        assert "src/main.py" in issues[0].message

    def test_detects_touch_vs_edit_conflict(self):
        plan = PlanSpec(
            name="mixed-conflict",
            goal="Goal",
            units={
                "a": PlanUnit(
                    slug="a",
                    summary="A",
                    prompt=_VALID_PROMPT,
                    commit_message="A",
                    files=PlanUnitFiles(touch=("src/main.py",)),
                ),
                "b": PlanUnit(
                    slug="b",
                    summary="B",
                    prompt=_VALID_PROMPT,
                    commit_message="B",
                    files=PlanUnitFiles(edit=("src/main.py",)),
                ),
            },
        )
        issues = validate_plan(plan)
        assert len(issues) == 1


# =============================================================================
# validate_plan — unclaimed prompt path reference tests
# =============================================================================


class TestValidatePlanUnclaimedPromptRefs:
    """Tests for the prompt→claims cross-check.

    The validator scans each task's prompt for file-path references and warns
    when they aren't covered by the task's file claims.
    """

    def _unit(self, prompt: str, **file_kwargs) -> PlanUnit:
        return PlanUnit(
            slug="task",
            summary="Task",
            prompt=prompt,
            commit_message="Done",
            files=PlanUnitFiles(**file_kwargs),
        )

    def _plan(self, prompt: str, **file_kwargs) -> PlanSpec:
        return PlanSpec(
            name="test-plan",
            goal="Goal",
            units={"task": self._unit(prompt, **file_kwargs)},
        )

    def _ref_warnings(self, issues: list) -> list:
        """Filter to unclaimed-ref warnings only."""
        return [i for i in issues if "not in the file claim" in i.message]

    def test_no_warning_when_no_path_refs(self):
        plan = self._plan("Add the feature to the main module.")
        assert self._ref_warnings(validate_plan(plan)) == []

    def test_warns_on_unclaimed_test_ref_in_prompt(self):
        plan = self._plan(
            "Run tests/test_foo.py to verify.",
            edit=("src/foo.py",),
        )
        warnings = self._ref_warnings(validate_plan(plan))
        assert len(warnings) == 1
        assert "tests/test_foo.py" in warnings[0].message
        assert warnings[0].unit == "task"

    def test_warns_on_unclaimed_src_ref_in_prompt(self):
        plan = self._plan("Read src/adapters/db.py and fix the bug.")
        warnings = self._ref_warnings(validate_plan(plan))
        assert len(warnings) == 1
        assert "src/adapters/db.py" in warnings[0].message

    def test_no_warning_when_ref_claimed_via_touch(self):
        plan = self._plan(
            "Update tests/test_foo.py.",
            touch=("tests/test_foo.py",),
        )
        assert self._ref_warnings(validate_plan(plan)) == []

    def test_no_warning_when_ref_claimed_via_edit(self):
        plan = self._plan(
            "Update src/foo.py and tests/test_foo.py.",
            edit=("src/foo.py", "tests/test_foo.py"),
        )
        assert self._ref_warnings(validate_plan(plan)) == []

    def test_no_warning_when_ref_claimed_via_create(self):
        plan = self._plan(
            "Create tests/test_new.py with new tests.",
            create=("tests/test_new.py",),
        )
        assert self._ref_warnings(validate_plan(plan)) == []

    def test_warns_only_once_for_duplicate_ref(self):
        plan = self._plan("Run tests/test_foo.py. Then check tests/test_foo.py again.")
        warnings = self._ref_warnings(validate_plan(plan))
        assert len(warnings) == 1

    def test_warns_for_each_distinct_unclaimed_path(self):
        plan = self._plan("Check tests/test_foo.py and tests/test_bar.py.")
        warnings = self._ref_warnings(validate_plan(plan))
        assert len(warnings) == 2

    def test_warning_includes_scope_violation_guidance(self):
        plan = self._plan("Run tests/test_foo.py to verify.")
        warnings = self._ref_warnings(validate_plan(plan))
        assert "scope_violation" in warnings[0].message

    def test_warning_includes_unit_slug(self):
        plan = PlanSpec(
            name="slug-plan",
            goal="Goal",
            units={
                "my-slug": PlanUnit(
                    slug="my-slug",
                    summary="Task",
                    prompt="Update tests/test_foo.py",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                )
            },
        )
        warnings = self._ref_warnings(validate_plan(plan))
        assert warnings[0].unit == "my-slug"

    def test_no_warning_for_bare_filename(self):
        """Bare filenames without directories are not flagged."""
        plan = self._plan("Edit foo.py to fix the bug.")
        assert self._ref_warnings(validate_plan(plan)) == []

    def test_detects_test_prefix_variants(self):
        """Both 'test/' and 'tests/' paths are matched."""
        plan = self._plan("See test/test_foo.py for examples.")
        warnings = self._ref_warnings(validate_plan(plan))
        assert len(warnings) == 1
        assert "test/test_foo.py" in warnings[0].message

    def test_detects_toml_path_refs(self):
        plan = self._plan("Read .dgov/project.toml for config.")
        warnings = self._ref_warnings(validate_plan(plan))
        assert any(".dgov/project.toml" in w.message for w in warnings)

    def test_detects_json_path_refs(self):
        plan = self._plan("Load examples/vector-inspect.json for validation.")
        warnings = self._ref_warnings(validate_plan(plan))
        assert any("examples/vector-inspect.json" in w.message for w in warnings)

    def test_all_claimed_paths_no_warnings(self):
        """When every prompt path is claimed, no warnings emitted."""
        plan = self._plan(
            "Edit src/foo.py. Run tests/test_foo.py. Check docs/api.md.",
            edit=("src/foo.py", "tests/test_foo.py"),
            touch=("docs/api.md",),
        )
        assert self._ref_warnings(validate_plan(plan)) == []


# =============================================================================
# validate_plan — verify-only task warnings
# =============================================================================


class TestValidatePlanVerifyOnlyTasks:
    """Tasks that only create non-code files should not claim .py touch/edit."""

    def _plan(self, **file_kwargs) -> PlanSpec:
        return PlanSpec(
            name="test-plan",
            goal="Goal",
            units={
                "task": PlanUnit(
                    slug="task",
                    summary="Capture output",
                    prompt="Run the command and save output.",
                    commit_message="Capture output",
                    files=PlanUnitFiles(**file_kwargs),
                )
            },
        )

    def test_warns_on_py_touch_with_non_py_create(self):
        plan = self._plan(
            create=("examples/output.json", "examples/output.txt"),
            touch=("tests/integration/test_cli.py",),
        )
        warnings = [i for i in validate_plan(plan) if i.severity == "warning"]
        assert any("tempts the worker" in w.message for w in warnings)

    def test_warns_on_py_edit_with_non_py_create(self):
        plan = self._plan(
            create=("docs/architecture.md",),
            edit=("src/cli/vector.py",),
        )
        warnings = [i for i in validate_plan(plan) if i.severity == "warning"]
        assert any("tempts the worker" in w.message for w in warnings)

    def test_no_warning_when_create_includes_py(self):
        plan = self._plan(
            create=("src/new_module.py", "examples/output.json"),
            touch=("tests/test_new.py",),
        )
        warnings = [i for i in validate_plan(plan) if i.severity == "warning"]
        verify_warnings = [w for w in warnings if "tempts the worker" in w.message]
        assert len(verify_warnings) == 0

    def test_no_warning_when_no_create(self):
        plan = self._plan(edit=("src/foo.py",))
        warnings = [i for i in validate_plan(plan) if i.severity == "warning"]
        verify_warnings = [w for w in warnings if "tempts the worker" in w.message]
        assert len(verify_warnings) == 0

    def test_no_warning_when_no_py_touches(self):
        plan = self._plan(
            create=("examples/output.json",),
            touch=("examples/readme.txt",),
        )
        warnings = [i for i in validate_plan(plan) if i.severity == "warning"]
        verify_warnings = [w for w in warnings if "tempts the worker" in w.message]
        assert len(verify_warnings) == 0


# =============================================================================
# validate_plan — prompt structure warnings
# =============================================================================


class TestValidatePlanPromptStructure:
    """Prompts missing Orient/Edit/Verify headers get a warning."""

    def _plan(self, prompt: str) -> PlanSpec:
        return PlanSpec(
            name="test-plan",
            goal="Goal",
            units={
                "task": PlanUnit(
                    slug="task",
                    summary="Task",
                    prompt=prompt,
                    commit_message="Done",
                    files=PlanUnitFiles(),
                )
            },
        )

    def _structure_warnings(self, issues: list) -> list:
        return [i for i in issues if "section headers" in i.message]

    def test_no_warning_when_all_phases_present(self):
        prompt = """
Orient:
Read the file first.

Edit:
1. Change the thing.

Verify:
- uv run pytest -q
"""
        assert self._structure_warnings(validate_plan(self._plan(prompt))) == []

    def test_warns_when_all_phases_missing(self):
        prompt = "Just do the thing."
        warnings = self._structure_warnings(validate_plan(self._plan(prompt)))
        assert len(warnings) == 1
        assert "Orient" in warnings[0].message
        assert "Edit" in warnings[0].message
        assert "Verify" in warnings[0].message
        assert warnings[0].unit == "task"

    def test_warns_listing_only_missing_phases(self):
        prompt = """
Orient:
Read stuff.

Do the edits now.
"""
        warnings = self._structure_warnings(validate_plan(self._plan(prompt)))
        assert len(warnings) == 1
        assert "missing section headers: Edit, Verify." in warnings[0].message

    def test_accepts_markdown_header_format(self):
        prompt = """
## Orient:
Read stuff.

## Edit:
Change stuff.

## Verify:
Check stuff.
"""
        assert self._structure_warnings(validate_plan(self._plan(prompt))) == []

    def test_accepts_markdown_headers_without_colons(self):
        prompt = """
## Orient
Read stuff.

## Edit
Change stuff.

## Verify
Check stuff.
"""
        assert self._structure_warnings(validate_plan(self._plan(prompt))) == []

    def test_accepts_bold_format(self):
        prompt = """
**Orient:**
Read stuff.

**Edit:**
Change stuff.

**Verify:**
Check stuff.
"""
        assert self._structure_warnings(validate_plan(self._plan(prompt))) == []

    def test_accepts_bold_headers_without_colons(self):
        prompt = """
**Orient**
Read stuff.

**Edit**
Change stuff.

**Verify**
Check stuff.
"""
        assert self._structure_warnings(validate_plan(self._plan(prompt))) == []

    def test_case_insensitive(self):
        prompt = """
orient:
read stuff.

edit:
change stuff.

verify:
check stuff.
"""
        assert self._structure_warnings(validate_plan(self._plan(prompt))) == []

    def test_edit_in_prose_does_not_count(self):
        """'Edit the file' at line start is not a section header (no colon after 'Edit')."""
        prompt = "Edit the file to fix the bug."
        warnings = self._structure_warnings(validate_plan(self._plan(prompt)))
        assert len(warnings) == 1
        assert "Edit" in warnings[0].message

    def test_warning_includes_success_rate_guidance(self):
        prompt = "Do the thing."
        warnings = self._structure_warnings(validate_plan(self._plan(prompt)))
        assert "first-attempt success" in warnings[0].message

    def _plan_with(self, prompt: str, **kwargs) -> PlanSpec:
        return PlanSpec(
            name="test-plan",
            goal="Goal",
            units={
                "task": PlanUnit(
                    slug="task",
                    summary="Task",
                    prompt=prompt,
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    **kwargs,
                )
            },
        )

    def test_test_cmd_suppresses_verify_warning(self):
        prompt = """
Orient:
Read stuff.

Edit:
Change stuff.
"""
        plan = self._plan_with(prompt, test_cmd="uv run pytest -q")
        assert self._structure_warnings(validate_plan(plan)) == []

    def test_test_cmd_does_not_suppress_orient_or_edit(self):
        prompt = "Just do the thing."
        plan = self._plan_with(prompt, test_cmd="uv run pytest -q")
        warnings = self._structure_warnings(validate_plan(plan))
        assert len(warnings) == 1
        assert "missing section headers: Orient, Edit." in warnings[0].message

    def test_researcher_only_warns_orient(self):
        prompt = "Just do the thing."
        plan = self._plan_with(prompt, role="researcher")
        warnings = self._structure_warnings(validate_plan(plan))
        assert len(warnings) == 1
        assert warnings[0].message == (
            "Prompt is missing section headers: Orient. "
            "Structured prompts (Orient/Edit/Verify) have higher "
            "first-attempt success rates."
        )

    def test_reviewer_no_warning_with_orient(self):
        prompt = """
Orient:
Read the dependency diffs.
"""
        plan = self._plan_with(prompt, role="reviewer")
        assert self._structure_warnings(validate_plan(plan)) == []


# =============================================================================
# Integration tests
# =============================================================================


class TestFlatFilesParse:
    def test_parse_plan_with_flat_files(self, tmp_path):
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "flat-plan"

[tasks.simple]
summary = "Simple task"
prompt = "Do it"
commit_message = "Done"
files = ["src/foo.py", "tests/test_foo.py"]
"""
        plan_file.write_text(plan_content)
        result = parse_plan_file(str(plan_file))
        unit = result.units["simple"]
        assert unit.files.touch == ("src/foo.py", "tests/test_foo.py")
        assert unit.files.create == ()
        assert unit.files.edit == ()

    def test_flat_and_structured_coexist(self, tmp_path):
        """Different tasks can use flat vs structured format."""
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "mixed-plan"

[tasks.flat-task]
summary = "Flat"
prompt = "Do"
commit_message = "Done"
files = ["src/a.py"]

[tasks.structured-task]
summary = "Structured"
prompt = "Do"
commit_message = "Done"

[tasks.structured-task.files]
edit = ["src/b.py"]
create = ["src/c.py"]
"""
        plan_file.write_text(plan_content)
        result = parse_plan_file(str(plan_file))
        assert result.units["flat-task"].files.touch == ("src/a.py",)
        assert result.units["structured-task"].files.edit == ("src/b.py",)
        assert result.units["structured-task"].files.create == ("src/c.py",)


class TestPlanIntegration:
    def test_round_trip_parse_compile(self, tmp_path):
        plan_file = tmp_path / "integration.toml"
        plan_content = """
[plan]
name = "integration-plan"
project_root = "/project"

[tasks.setup]
summary = "Setup environment"
prompt = "Setup the environment"
commit_message = "Setup complete"
agent = "setup-agent"
timeout_s = 300

[tasks.setup.files]
create = ["config.yaml", ".env"]

[tasks.main]
summary = "Main implementation"
prompt = "Implement the feature"
commit_message = "Feature implemented"
depends_on = ["setup"]
agent = "main-agent"
timeout_s = 600

[tasks.main.files]
edit = ["src/main.py"]
create = ["src/helper.py"]
"""
        plan_file.write_text(plan_content)

        spec = parse_plan_file(str(plan_file))

        assert spec.name == "integration-plan"
        assert spec.project_root == "/project"
        assert len(spec.units) == 2

        dag = compile_plan(spec)

        assert dag.name == "integration-plan"
        assert len(dag.tasks) == 2

        setup_task = dag.tasks["setup"]
        assert setup_task.agent == "setup-agent"
        assert setup_task.timeout_s == 300

        main_task = dag.tasks["main"]
        assert main_task.depends_on == ("setup",)
