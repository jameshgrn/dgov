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
    PlanEval,
    PlanSpec,
    PlanUnit,
    PlanUnitFiles,
    _normalize_touch_path,
    _paths_overlap,
    compile_plan,
    parse_plan_file,
    validate_plan,
)

# =============================================================================
# _normalize_touch_path tests
# =============================================================================


class TestNormalizeTouchPath:
    """Tests for _normalize_touch_path function."""

    def test_strips_leading_dot_slash(self):
        """Should strip leading ./ from paths."""
        assert _normalize_touch_path("./file.txt") == "file.txt"
        assert _normalize_touch_path("./src/main.py") == "src/main.py"

    def test_strips_trailing_slash(self):
        """Should strip trailing / from paths."""
        assert _normalize_touch_path("src/") == "src"
        assert _normalize_touch_path("some/path/") == "some/path"

    def test_strips_whitespace(self):
        """Should strip leading and trailing whitespace."""
        assert _normalize_touch_path("  file.txt  ") == "file.txt"
        assert _normalize_touch_path("\tpath/to/file\n") == "path/to/file"

    def test_handles_multiple_prefixes_and_suffixes(self):
        """Should handle combinations of ./ and trailing slashes.

        Note: lstrip('./') strips any combination of '.' and '/' from the start,
        so '././' becomes empty string, then the path continues.
        """
        assert _normalize_touch_path("./src/") == "src"
        # lstrip("./") strips all '.' and '/' from the start, so
        # "././nested/path///" -> "nested/path" after normalization
        assert _normalize_touch_path("././nested/path///") == "nested/path"

    def test_handles_empty_string(self):
        """Should handle empty string."""
        assert _normalize_touch_path("") == ""

    def test_handles_plain_paths(self):
        """Should leave plain paths unchanged."""
        assert _normalize_touch_path("file.txt") == "file.txt"
        assert _normalize_touch_path("src/main.py") == "src/main.py"


# =============================================================================
# _paths_overlap tests
# =============================================================================


class TestPathsOverlap:
    """Tests for _paths_overlap function."""

    def test_identical_paths_overlap(self):
        """Identical paths should overlap."""
        assert _paths_overlap("src/main.py", "src/main.py") is True
        assert _paths_overlap("./src/main.py", "src/main.py") is True

    def test_parent_child_overlap(self):
        """Parent-child path relationships should overlap."""
        assert _paths_overlap("src/main.py", "src") is True
        assert _paths_overlap("src", "src/main.py") is True

    def test_nested_directory_overlap(self):
        """Deeply nested paths should overlap with ancestors."""
        assert _paths_overlap("src/utils/helper.py", "src") is True
        assert _paths_overlap("src", "src/utils/helper.py") is True

    def test_sibling_paths_no_overlap(self):
        """Sibling paths should not overlap."""
        assert _paths_overlap("src/main.py", "src/utils.py") is False

    def test_unrelated_paths_no_overlap(self):
        """Completely unrelated paths should not overlap."""
        assert _paths_overlap("src/main.py", "tests/test_main.py") is False

    def test_similar_prefix_no_overlap(self):
        """Paths with similar prefixes but different directories should not overlap."""
        assert _paths_overlap("src_main.py", "src/main.py") is False

    def test_empty_path_no_overlap(self):
        """Empty paths should not overlap with anything."""
        assert _paths_overlap("", "src/main.py") is False
        assert _paths_overlap("src/main.py", "") is False

    def test_normalizes_before_comparison(self):
        """Should normalize paths before comparing."""
        assert _paths_overlap("./src/main.py", "src/") is True
        assert _paths_overlap("  src/main.py  ", "./src/") is True


# =============================================================================
# parse_plan_file tests
# =============================================================================


class TestParsePlanFile:
    """Tests for parse_plan_file function."""

    def test_parses_minimal_plan_file(self, tmp_path):
        """Should parse minimal valid TOML plan file."""
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "test-plan"

[units.test-unit]
slug = "test-unit"
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

    def test_parses_plan_with_dag_alias(self, tmp_path):
        """Should parse plan with [dag] section instead of [plan]."""
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[dag]
name = "dag-plan"

[tasks.task1]
slug = "task1"
summary = "Task one"
prompt = "Do task one"
commit_message = "Task 1 done"
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        assert result.name == "dag-plan"
        assert "task1" in result.units

    def test_parses_plan_with_files(self, tmp_path):
        """Should parse plan with file operations."""
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "file-plan"

[units.create-files]
slug = "create-files"
summary = "Create some files"
prompt = "Create files"
commit_message = "Add files"

[units.create-files.files]
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

    def test_parses_plan_with_dependencies(self, tmp_path):
        """Should parse plan with unit dependencies."""
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "dep-plan"

[units.setup]
slug = "setup"
summary = "Setup"
prompt = "Do setup"
commit_message = "Setup done"

[units.main-task]
slug = "main-task"
summary = "Main task"
prompt = "Do main task"
commit_message = "Main done"
depends_on = ["setup"]
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        assert result.units["main-task"].depends_on == ("setup",)

    def test_parses_plan_with_evals(self, tmp_path):
        """Should parse plan with eval criteria."""
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "eval-plan"

[units.test-unit]
slug = "test-unit"
summary = "Test unit"
prompt = "Run tests"
commit_message = "Tests done"
satisfies = ["eval-1"]

[[evals]]
id = "eval-1"
kind = "unit"
statement = "All tests pass"
evidence = "pytest output"
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        assert len(result.evals) == 1
        assert result.evals[0].eval_id == "eval-1"
        assert result.evals[0].kind == "unit"
        assert result.evals[0].statement == "All tests pass"
        assert result.evals[0].evidence == "pytest output"

    def test_parses_plan_with_agent_settings(self, tmp_path):
        """Should parse plan with agent and timeout settings."""
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "agent-plan"

[units.custom-agent]
slug = "custom-agent"
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

    def test_raises_file_not_found(self, tmp_path):
        """Should raise FileNotFoundError for non-existent file."""
        non_existent = tmp_path / "does_not_exist.toml"

        with pytest.raises(FileNotFoundError):
            parse_plan_file(str(non_existent))

    def test_parses_multiple_units(self, tmp_path):
        """Should parse plan with multiple units."""
        plan_file = tmp_path / "plan.toml"
        plan_content = """
[plan]
name = "multi-plan"

[units.first]
slug = "first"
summary = "First task"
prompt = "First"
commit_message = "First done"

[units.second]
slug = "second"
summary = "Second task"
prompt = "Second"
commit_message = "Second done"

[units.third]
slug = "third"
summary = "Third task"
prompt = "Third"
commit_message = "Third done"
depends_on = ["first", "second"]
"""
        plan_file.write_text(plan_content)

        result = parse_plan_file(str(plan_file))

        assert len(result.units) == 3
        assert "first" in result.units
        assert "second" in result.units
        assert "third" in result.units


# =============================================================================
# compile_plan tests
# =============================================================================


class TestCompilePlan:
    """Tests for compile_plan function."""

    def test_compiles_minimal_plan(self):
        """Should compile minimal PlanSpec to DagDefinition."""
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

        result = compile_plan(plan)

        assert result.name == "simple-plan"
        assert "task-1" in result.tasks
        task = result.tasks["task-1"]
        assert task.slug == "task-1"
        assert task.summary == "Task one"
        assert task.prompt == "Do task one"
        assert task.commit_message == "Task 1 done"

    def test_uses_default_agent_when_unit_has_none(self):
        """Should use plan's default agent when unit agent is empty."""
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
                    agent="",  # Empty agent
                )
            },
        )

        result = compile_plan(plan)

        assert result.tasks["task-1"].agent == "qwen-35b"

    def test_preserves_unit_agent_when_specified(self):
        """Should preserve unit-specific agent when provided."""
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

        result = compile_plan(plan)

        assert result.tasks["task-1"].agent == "claude-3-opus"

    def test_uses_default_timeout_when_unit_has_none(self):
        """Should use plan's default timeout when unit timeout is 0."""
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

        result = compile_plan(plan)

        # Note: DagTaskSpec has default of 900, but since timeout_s=0 is falsy,
        # it uses default_timeout_s from plan (1200)
        assert result.tasks["task-1"].timeout_s == 1200

    def test_preserves_unit_timeout_when_specified(self):
        """Should preserve unit-specific timeout when provided."""
        plan = PlanSpec(
            name="custom-timeout-plan",
            goal="Goal",
            default_timeout_s=600,
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    timeout_s=1800,
                )
            },
        )

        result = compile_plan(plan)

        assert result.tasks["task-1"].timeout_s == 1800

    def test_preserves_dependencies(self):
        """Should preserve unit dependencies."""
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

        result = compile_plan(plan)

        assert result.tasks["task-2"].depends_on == ("task-1",)

    def test_preserves_file_operations(self):
        """Should preserve file create/edit/delete operations."""
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

        result = compile_plan(plan)

        task = result.tasks["file-task"]
        assert task.files.create == ("new.py",)
        assert task.files.edit == ("existing.py",)
        assert task.files.delete == ("old.py",)

    def test_includes_satisfies_evals_in_prompt(self):
        """Should append satisfied evals to the prompt."""
        plan = PlanSpec(
            name="eval-plan",
            goal="Goal",
            evals=(
                PlanEval(
                    eval_id="eval-1",
                    kind="unit",
                    statement="Tests pass",
                    evidence="pytest",
                ),
            ),
            units={
                "test-task": PlanUnit(
                    slug="test-task",
                    summary="Test task",
                    prompt="Run tests",
                    commit_message="Tests run",
                    files=PlanUnitFiles(),
                    satisfies=("eval-1",),
                )
            },
        )

        result = compile_plan(plan)

        prompt = result.tasks["test-task"].prompt
        assert "Run tests" in prompt
        assert "## Evals to satisfy" in prompt
        assert "[eval-1]" in prompt
        assert "unit: Tests pass" in prompt
        assert "Evidence: pytest" in prompt

    def test_handles_multiple_satisfies_evals(self):
        """Should handle unit satisfying multiple evals."""
        plan = PlanSpec(
            name="multi-eval-plan",
            goal="Goal",
            evals=(
                PlanEval(eval_id="eval-1", kind="unit", statement="A", evidence="E1"),
                PlanEval(eval_id="eval-2", kind="lint", statement="B", evidence="E2"),
            ),
            units={
                "task": PlanUnit(
                    slug="task",
                    summary="Task",
                    prompt="Do task",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    satisfies=("eval-1", "eval-2"),
                )
            },
        )

        result = compile_plan(plan)

        prompt = result.tasks["task"].prompt
        assert "[eval-1]" in prompt
        assert "[eval-2]" in prompt

    def test_handles_missing_eval_in_satisfies(self):
        """Should handle satisfies referencing non-existent eval.

        When a unit has satisfies but the eval doesn't exist, the prompt
        should still get the "Evals to satisfy" header, but no eval details.
        """
        plan = PlanSpec(
            name="plan",
            goal="Goal",
            evals=(),  # No evals defined
            units={
                "task": PlanUnit(
                    slug="task",
                    summary="Task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    satisfies=("non-existent",),
                )
            },
        )

        result = compile_plan(plan)

        # The header is added because there are satisfies, but no eval details
        # are appended since the eval doesn't exist
        prompt = result.tasks["task"].prompt
        assert "Do it" in prompt
        assert "## Evals to satisfy" in prompt

    def test_preserves_escalation(self):
        """Should preserve escalation configuration."""
        plan = PlanSpec(
            name="escalation-plan",
            goal="Goal",
            units={
                "risky-task": PlanUnit(
                    slug="risky-task",
                    summary="Risky task",
                    prompt="Be careful",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                    escalation=("manager", "cto"),
                )
            },
        )

        result = compile_plan(plan)

        assert result.tasks["risky-task"].escalation == ("manager", "cto")

    def test_preserves_role(self):
        """Should preserve role setting."""
        plan = PlanSpec(
            name="role-plan",
            goal="Goal",
            units={
                "review-task": PlanUnit(
                    slug="review-task",
                    summary="Review task",
                    prompt="Review code",
                    commit_message="Reviewed",
                    files=PlanUnitFiles(),
                    role="reviewer",
                )
            },
        )

        result = compile_plan(plan)

        assert result.tasks["review-task"].role == "reviewer"

    def test_compiles_multiple_units(self):
        """Should compile plan with multiple units."""
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

        result = compile_plan(plan)

        assert len(result.tasks) == 2
        assert "task-1" in result.tasks
        assert "task-2" in result.tasks

    def test_preserves_project_and_session_root(self):
        """Should preserve project_root and session_root."""
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

        result = compile_plan(plan)

        assert result.project_root == "/home/user/project"
        assert result.session_root == "/tmp/session"

    def test_preserves_merge_resolve(self):
        """Should preserve merge_resolve setting."""
        plan = PlanSpec(
            name="merge-plan",
            goal="Goal",
            merge_resolve="merge",
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

        result = compile_plan(plan)

        assert result.merge_resolve == "merge"


# =============================================================================
# validate_plan tests
# =============================================================================


class TestValidatePlan:
    """Tests for validate_plan function."""

    def test_returns_empty_list_for_valid_plan(self):
        """Should return empty list for valid plan."""
        plan = PlanSpec(
            name="valid-plan",
            goal="Goal",
            units={
                "task-1": PlanUnit(
                    slug="task-1",
                    summary="Task",
                    prompt="Do it",
                    commit_message="Done",
                    files=PlanUnitFiles(),
                )
            },
        )

        issues = validate_plan(plan)

        assert issues == []

    def test_returns_empty_list_for_empty_plan(self):
        """Should return empty list for plan with no units."""
        plan = PlanSpec(name="empty-plan", goal="Goal", units={})

        issues = validate_plan(plan)

        assert issues == []

    def test_returns_list_type(self):
        """Should always return a list."""
        plan = PlanSpec(name="any-plan", goal="Goal", units={})

        issues = validate_plan(plan)

        assert isinstance(issues, list)


# =============================================================================
# Integration tests
# =============================================================================


class TestPlanIntegration:
    """Integration tests combining parse and compile."""

    def test_round_trip_parse_compile(self, tmp_path):
        """Should be able to parse TOML and compile to DagDefinition."""
        plan_file = tmp_path / "integration.toml"
        plan_content = """
[plan]
name = "integration-plan"
project_root = "/project"
max_concurrent = 4

[units.setup]
slug = "setup"
summary = "Setup environment"
prompt = "Setup the environment"
commit_message = "Setup complete"
agent = "setup-agent"
timeout_s = 300

[units.setup.files]
create = ["config.yaml", ".env"]

[units.main]
slug = "main"
summary = "Main implementation"
prompt = "Implement the feature"
commit_message = "Feature implemented"
depends_on = ["setup"]
agent = "main-agent"
timeout_s = 600

[units.main.files]
edit = ["src/main.py"]
create = ["src/helper.py"]

[[evals]]
id = "test-pass"
kind = "unit"
statement = "All tests pass"
evidence = "pytest --cov"
"""
        plan_file.write_text(plan_content)

        # Parse the plan
        spec = parse_plan_file(str(plan_file))

        assert spec.name == "integration-plan"
        assert spec.project_root == "/project"
        assert len(spec.units) == 2

        # Compile the plan
        dag = compile_plan(spec)

        assert dag.name == "integration-plan"
        assert dag.project_root == "/project"
        assert len(dag.tasks) == 2

        # Verify tasks
        setup_task = dag.tasks["setup"]
        assert setup_task.agent == "setup-agent"
        assert setup_task.timeout_s == 300
        assert setup_task.files.create == ("config.yaml", ".env")

        main_task = dag.tasks["main"]
        assert main_task.agent == "main-agent"
        assert main_task.timeout_s == 600
        assert main_task.depends_on == ("setup",)
