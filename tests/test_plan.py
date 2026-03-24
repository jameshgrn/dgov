"""Tests for dgov.plan — plan schema, validator, and compiler."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.plan import (
    PlanEval,
    PlanSpec,
    PlanUnit,
    PlanUnitFiles,
    compile_plan,
    parse_plan_file,
    scratch_plan_path,
    scratch_plans_dir,
    validate_plan,
    write_scratch_plan,
)

pytestmark = pytest.mark.unit

_VALID_PLAN_TOML = """
[plan]
version = 1
name = "test-plan"
goal = "Test the plan system"
default_agent = "qwen-9b"
default_timeout_s = 600

[[evals]]
id = "E1"
kind = "regression"
statement = "Task A updates src/a.py."
evidence = "uv run pytest tests/test_a.py -q"
scope = ["src/a.py"]

[[evals]]
id = "E2"
kind = "happy_path"
statement = "Task B creates src/b.py."
evidence = "uv run pytest tests/test_b.py -q"
scope = ["src/b.py"]

[units.task-a]
summary = "First task"
prompt = "Do task A"
commit_message = "Complete A"
satisfies = ["E1"]
agent = "qwen-35b"
timeout_s = 900

[units.task-a.files]
edit = ["src/a.py"]
read = ["src/helpers.py"]

[units.task-a.acceptance]
tests_pass = true
lint_clean = true

[units.task-b]
summary = "Second task"
prompt = "Do task B"
commit_message = "Complete B"
satisfies = ["E2"]
depends_on = ["task-a"]

[units.task-b.files]
create = ["src/b.py"]
edit = ["src/other.py"]
"""


def _write_plan(tmp_path: Path, content: str = _VALID_PLAN_TOML) -> str:
    p = tmp_path / "plan.toml"
    p.write_text(content)
    return str(p)


class TestParsePlanFile:
    def test_valid_plan(self, tmp_path):
        """Parse a valid TOML plan file."""
        spec = parse_plan_file(_write_plan(tmp_path))
        assert isinstance(spec, PlanSpec)
        assert spec.name == "test-plan"
        assert spec.goal == "Test the plan system"
        assert len(spec.evals) == 2
        assert len(spec.units) == 2
        assert "task-a" in spec.units
        assert "task-b" in spec.units

    def test_unit_fields(self, tmp_path):
        """Check unit fields are parsed correctly."""
        spec = parse_plan_file(_write_plan(tmp_path))
        task_a = spec.units["task-a"]
        assert task_a.slug == "task-a"
        assert task_a.summary == "First task"
        assert task_a.prompt == "Do task A"
        assert task_a.commit_message == "Complete A"
        assert task_a.satisfies == ("E1",)
        assert task_a.agent == "qwen-35b"
        assert task_a.timeout_s == 900

    def test_eval_fields(self, tmp_path):
        """Check eval fields are parsed correctly."""
        spec = parse_plan_file(_write_plan(tmp_path))
        assert spec.evals[0].eval_id == "E1"
        assert spec.evals[0].kind == "regression"
        assert spec.evals[0].statement == "Task A updates src/a.py."
        assert spec.evals[0].evidence == "uv run pytest tests/test_a.py -q"
        assert spec.evals[0].scope == ("src/a.py",)

    def test_unit_files(self, tmp_path):
        """Check file specs are parsed correctly."""
        spec = parse_plan_file(_write_plan(tmp_path))
        task_a = spec.units["task-a"]
        assert task_a.files.edit == ("src/a.py",)
        assert task_a.files.read == ("src/helpers.py",)
        assert task_a.files.create == ()
        assert task_a.files.delete == ()

    def test_unit_acceptance(self, tmp_path):
        """Check acceptance criteria are parsed correctly."""
        spec = parse_plan_file(_write_plan(tmp_path))
        task_a = spec.units["task-a"]
        assert task_a.acceptance.tests_pass is True
        assert task_a.acceptance.lint_clean is True
        assert task_a.acceptance.custom_check == ""

    def test_depends_on(self, tmp_path):
        """Check dependency resolution."""
        spec = parse_plan_file(_write_plan(tmp_path))
        assert spec.units["task-b"].depends_on == ("task-a",)
        assert spec.units["task-a"].depends_on == ()

    def test_defaults(self, tmp_path):
        """Check plan-level defaults are set correctly."""
        spec = parse_plan_file(_write_plan(tmp_path))
        assert spec.default_agent == "qwen-9b"
        assert spec.default_timeout_s == 600
        assert spec.max_concurrent == 0
        assert spec.merge_strategy == "squash"

    def test_unit_default_agent(self, tmp_path):
        """Units without explicit agent get empty string."""
        spec = parse_plan_file(_write_plan(tmp_path))
        # task-b has no agent set in TOML
        assert spec.units["task-b"].agent == ""

    def test_unit_default_timeout(self, tmp_path):
        """Units without explicit timeout_s get 0."""
        spec = parse_plan_file(_write_plan(tmp_path))
        # task-b has no timeout_s set in TOML
        assert spec.units["task-b"].timeout_s == 0

    def test_missing_plan_section(self, tmp_path):
        """TOML without [plan] section raises ValueError."""
        toml_content = """
[units.task-a]
summary = "x"
prompt = "do x"
commit_message = "x"
[units.task-a.files]
edit = ["a.py"]
"""
        with pytest.raises(ValueError, match="Missing \\[plan\\] section"):
            parse_plan_file(_write_plan(tmp_path, toml_content))

    def test_missing_name(self, tmp_path):
        """[plan] without name raises ValueError."""
        toml_content = """
[plan]
version = 1
goal = "test goal"
[units.task-a]
summary = "x"
prompt = "do x"
commit_message = "x"
[units.task-a.files]
edit = ["a.py"]
"""
        with pytest.raises(ValueError, match="Missing plan.name"):
            parse_plan_file(_write_plan(tmp_path, toml_content))

    def test_missing_goal(self, tmp_path):
        """[plan] without goal raises ValueError."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
[units.task-a]
summary = "x"
prompt = "do x"
commit_message = "x"
[units.task-a.files]
edit = ["a.py"]
"""
        with pytest.raises(ValueError, match="Missing plan.goal"):
            parse_plan_file(_write_plan(tmp_path, toml_content))

    def test_missing_units(self, tmp_path):
        """No [units] section raises ValueError."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
"""
        with pytest.raises(ValueError, match="Missing \\[units\\] section"):
            parse_plan_file(_write_plan(tmp_path, toml_content))

    def test_unit_missing_prompt(self, tmp_path):
        """Unit without prompt raises ValueError."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[units.bad]
summary = "bad unit"
commit_message = "commit"
[units.bad.files]
edit = ["a.py"]
"""
        with pytest.raises(ValueError, match="missing required field 'prompt'"):
            parse_plan_file(_write_plan(tmp_path, toml_content))

    def test_unit_no_files(self, tmp_path):
        """Unit with no files raises ValueError."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[units.bad]
summary = "bad unit"
prompt = "do it"
commit_message = "commit"
"""
        with pytest.raises(ValueError, match="must specify at least one file"):
            parse_plan_file(_write_plan(tmp_path, toml_content))

    def test_glob_rejected(self, tmp_path):
        """Files with glob pattern raises ValueError."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[units.bad]
summary = "bad unit"
prompt = "do it"
commit_message = "commit"
[units.bad.files]
edit = ["src/*.py"]
"""
        with pytest.raises(ValueError, match="Globs not allowed"):
            parse_plan_file(_write_plan(tmp_path, toml_content))

    def test_absolute_path_rejected(self, tmp_path):
        """Files with absolute path raises ValueError."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[units.bad]
summary = "bad unit"
prompt = "do it"
commit_message = "commit"
[units.bad.files]
edit = ["/etc/passwd"]
"""
        with pytest.raises(ValueError, match="must be relative"):
            parse_plan_file(_write_plan(tmp_path, toml_content))


class TestScratchPlans:
    def test_scratch_plans_dir_uses_session_root(self, tmp_path: Path) -> None:
        session_root = tmp_path / "session"
        actual = scratch_plans_dir(str(tmp_path), str(session_root))
        assert actual == session_root.resolve() / ".dgov" / "plans"

    def test_scratch_plan_path_appends_toml(self, tmp_path: Path) -> None:
        actual = scratch_plan_path("review_refactor", project_root=str(tmp_path))
        assert actual == tmp_path.resolve() / ".dgov" / "plans" / "review_refactor.toml"

    def test_write_scratch_plan_creates_valid_skeleton(self, tmp_path: Path) -> None:
        path = write_scratch_plan("review_refactor", project_root=str(tmp_path))
        assert path == tmp_path.resolve() / ".dgov" / "plans" / "review_refactor.toml"
        assert path.exists()

        spec = parse_plan_file(str(path))
        assert spec.name == "review_refactor"
        assert spec.goal == "Replace with the concrete goal before running."
        assert spec.evals[0].eval_id == "E1"
        assert "first_change" in spec.units
        assert spec.units["first_change"].satisfies == ("E1",)

    def test_write_scratch_plan_rejects_invalid_name(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid scratch plan name"):
            write_scratch_plan("Review Refactor", project_root=str(tmp_path))

    def test_write_scratch_plan_requires_force_to_overwrite(self, tmp_path: Path) -> None:
        path = write_scratch_plan("river_test", project_root=str(tmp_path))
        path.write_text("sentinel")

        with pytest.raises(ValueError, match="already exists"):
            write_scratch_plan("river_test", project_root=str(tmp_path))

        rewritten = write_scratch_plan("river_test", project_root=str(tmp_path), force=True)
        assert rewritten == path
        assert "[plan]" in path.read_text()


class TestValidatePlan:
    def test_valid_plan_no_issues(self, tmp_path):
        """Valid plan returns empty issue list."""
        spec = parse_plan_file(_write_plan(tmp_path))
        issues = validate_plan(spec)
        assert issues == []

    def test_missing_evals_is_error(self, tmp_path):
        """Plans must define evals before units."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"

[units.a]
summary = "a"
prompt = "do a"
commit_message = "commit a"
satisfies = ["E1"]
[units.a.files]
edit = ["a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        issues = validate_plan(spec)
        assert any("at least one [[evals]]" in issue.message for issue in issues)

    def test_missing_dependency(self, tmp_path):
        """Unit depends on non-existent unit returns error."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[[evals]]
id = "E1"
kind = "regression"
statement = "Task a depends on existing work."
evidence = "uv run pytest tests/test_a.py -q"
[units.task-a]
summary = "task a"
prompt = "do a"
commit_message = "commit a"
satisfies = ["E1"]
depends_on = ["nonexistent"]
[units.task-a.files]
edit = ["a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        issues = validate_plan(spec)
        # Check that we get an issue about missing dependency
        assert len(issues) >= 1
        assert any("nonexistent" in issue.message for issue in issues)

    def test_cycle_detection(self, tmp_path):
        """Two units depending on each other returns cycle error."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[[evals]]
id = "E1"
kind = "regression"
statement = "a resolves before b"
evidence = "uv run pytest tests/test_cycle.py -q"
[[evals]]
id = "E2"
kind = "edge"
statement = "b resolves before a"
evidence = "uv run pytest tests/test_cycle.py -q"
[units.a]
summary = "a"
prompt = "do a"
commit_message = "commit a"
satisfies = ["E1"]
depends_on = ["b"]
[units.a.files]
edit = ["a.py"]
[units.b]
summary = "b"
prompt = "do b"
commit_message = "commit b"
satisfies = ["E2"]
depends_on = ["a"]
[units.b.files]
edit = ["b.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        issues = validate_plan(spec)
        # Cycle detection may report one or both units, just check for cycle message
        assert len(issues) >= 1
        assert any("cycle" in issue.message.lower() for issue in issues)

    def test_file_conflict_parallel(self, tmp_path):
        """Two parallel units editing same file returns error."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[[evals]]
id = "E1"
kind = "regression"
statement = "a edits foo"
evidence = "uv run pytest tests/test_foo.py -q"
[[evals]]
id = "E2"
kind = "edge"
statement = "b edits foo"
evidence = "uv run pytest tests/test_foo.py -q"
[units.a]
summary = "a"
prompt = "do a"
commit_message = "commit a"
satisfies = ["E1"]
[units.a.files]
edit = ["src/foo.py"]
[units.b]
summary = "b"
prompt = "do b"
commit_message = "commit b"
satisfies = ["E2"]
[units.b.files]
edit = ["src/foo.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        issues = validate_plan(spec)
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "src/foo.py" in issues[0].message

    def test_no_conflict_sequential(self, tmp_path):
        """Two sequential units editing same file returns NO issue."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[[evals]]
id = "E1"
kind = "regression"
statement = "a edits foo before b"
evidence = "uv run pytest tests/test_foo.py -q"
[[evals]]
id = "E2"
kind = "happy_path"
statement = "b edits foo after a"
evidence = "uv run pytest tests/test_foo.py -q"
[units.a]
summary = "a"
prompt = "do a"
commit_message = "commit a"
satisfies = ["E1"]
[units.a.files]
edit = ["src/foo.py"]
[units.b]
summary = "b"
prompt = "do b"
commit_message = "commit b"
satisfies = ["E2"]
depends_on = ["a"]
[units.b.files]
edit = ["src/foo.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        issues = validate_plan(spec)
        # Check that there's no file conflict issue (sequential units don't conflict)
        file_conflicts = [i for i in issues if "both touch" in i.message]
        assert len(file_conflicts) == 0

    def test_long_summary_warning(self, tmp_path):
        """Unit with >80 char summary returns warning."""
        long_summary = "x" * 81
        toml_content = f"""
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[[evals]]
id = "E1"
kind = "regression"
statement = "long summary still has a target eval"
evidence = "uv run pytest tests/test_long.py -q"
[units.long]
summary = "{long_summary}"
prompt = "do it"
commit_message = "commit"
satisfies = ["E1"]
[units.long.files]
edit = ["a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        issues = validate_plan(spec)
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "long" in issues[0].unit
        assert "80" in issues[0].message

    def test_empty_prompt_error(self, tmp_path):
        """Unit with whitespace-only prompt returns error."""
        # Create PlanUnit directly with empty prompt
        unit = PlanUnit(
            slug="bad",
            summary="bad unit",
            prompt="   ",
            commit_message="commit",
            files=PlanUnitFiles(edit=("a.py",)),
            satisfies=("E1",),
        )
        plan = PlanSpec(
            name="test",
            goal="test goal",
            units={"bad": unit},
            evals=(
                PlanEval(
                    eval_id="E1",
                    kind="regression",
                    statement="Prompt must describe the work.",
                    evidence="uv run pytest tests/test_prompt.py -q",
                ),
            ),
        )
        issues = validate_plan(plan)
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "prompt must not be empty" in issues[0].message

    def test_empty_commit_message_error(self, tmp_path):
        """Unit with whitespace-only commit_message returns error."""
        unit = PlanUnit(
            slug="bad",
            summary="bad unit",
            prompt="do it",
            commit_message="   ",
            files=PlanUnitFiles(edit=("a.py",)),
            satisfies=("E1",),
        )
        plan = PlanSpec(
            name="test",
            goal="test goal",
            units={"bad": unit},
            evals=(
                PlanEval(
                    eval_id="E1",
                    kind="regression",
                    statement="Commit message must be present.",
                    evidence="uv run pytest tests/test_commit.py -q",
                ),
            ),
        )
        issues = validate_plan(plan)
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "commit_message must not be empty" in issues[0].message

    def test_unit_must_reference_known_eval(self, tmp_path):
        """Unknown eval references are validation errors."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[[evals]]
id = "E1"
kind = "regression"
statement = "Known eval"
evidence = "uv run pytest tests/test_known.py -q"

[units.a]
summary = "a"
prompt = "do a"
commit_message = "commit a"
satisfies = ["E2"]
[units.a.files]
edit = ["a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        issues = validate_plan(spec)
        assert any("unknown eval 'E2'" in issue.message for issue in issues)

    def test_eval_must_be_satisfied_by_a_unit(self, tmp_path):
        """Unclaimed evals are validation errors."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[[evals]]
id = "E1"
kind = "regression"
statement = "claimed eval"
evidence = "uv run pytest tests/test_claimed.py -q"
[[evals]]
id = "E2"
kind = "invariant"
statement = "unclaimed eval"
evidence = "uv run pytest tests/test_unclaimed.py -q"

[units.a]
summary = "a"
prompt = "do a"
commit_message = "commit a"
satisfies = ["E1"]
[units.a.files]
edit = ["a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        issues = validate_plan(spec)
        assert any("not satisfied by any unit" in issue.message for issue in issues)

    def test_eval_kind_must_be_valid(self, tmp_path):
        """Invalid eval kinds are validation errors."""

        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "test goal"
[[evals]]
id = "E1"
kind = "regression"
statement = "Valid eval"
evidence = "uv run pytest tests/test_valid.py -q"

[[evals]]
id = "E2"
kind = "invalid_kind"
statement = "Invalid kind test"
evidence = "uv run pytest tests/test_invalid.py -q"

[units.a]
summary = "a"
prompt = "do a"
commit_message = "commit a"
satisfies = ["E1"]
[units.a.files]
edit = ["a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        issues = validate_plan(spec)
        assert len(issues) >= 1
        assert any("invalid kind" in issue.message.lower() for issue in issues)


class TestCompilePlan:
    def test_compile_basic(self, tmp_path):
        """Compile valid TOML, check DagDefinition has same task count."""
        from dgov.dag_parser import DagDefinition

        spec = parse_plan_file(_write_plan(tmp_path))
        dag = compile_plan(spec)
        assert isinstance(dag, DagDefinition)
        assert len(dag.tasks) == 2
        assert "task-a" in dag.tasks
        assert "task-b" in dag.tasks

    def test_compile_resolves_agent_default(self, tmp_path):
        """task-b has no agent, after compile its DagTaskSpec.agent should be default."""
        spec = parse_plan_file(_write_plan(tmp_path))
        dag = compile_plan(spec)
        # task-b has no agent set in TOML, should resolve to plan default
        assert dag.tasks["task-b"].agent == "qwen-9b"

    def test_compile_resolves_timeout_default(self, tmp_path):
        """task-b has no timeout_s, after compile its DagTaskSpec.timeout_s should be 600."""
        spec = parse_plan_file(_write_plan(tmp_path))
        dag = compile_plan(spec)
        # task-b has no timeout_s set in TOML, should resolve to plan default
        assert dag.tasks["task-b"].timeout_s == 600

    def test_compile_preserves_explicit_values(self, tmp_path):
        """task-a with explicit agent and timeout_s preserves those values."""
        spec = parse_plan_file(_write_plan(tmp_path))
        dag = compile_plan(spec)
        assert dag.tasks["task-a"].agent == "qwen-35b"
        assert dag.tasks["task-a"].timeout_s == 900

    def test_compile_injects_read_files(self, tmp_path):
        """task-a has read files, compiled prompt contains 'Also read:'."""
        spec = parse_plan_file(_write_plan(tmp_path))
        dag = compile_plan(spec)
        prompt = dag.tasks["task-a"].prompt
        assert "Also read:" in prompt
        assert "src/helpers.py" in prompt

    def test_compile_injects_evals(self, tmp_path):
        """Compiled prompts include linked eval statements and evidence."""
        spec = parse_plan_file(_write_plan(tmp_path))
        dag = compile_plan(spec)
        prompt = dag.tasks["task-a"].prompt
        assert "## Evals to satisfy" in prompt
        assert "[E1] regression: Task A updates src/a.py." in prompt
        assert "Evidence: uv run pytest tests/test_a.py -q" in prompt

    def test_compile_drops_read_from_file_claims(self, tmp_path):
        """Compiled DagFileSpec should NOT have read files."""
        spec = parse_plan_file(_write_plan(tmp_path))
        dag = compile_plan(spec)
        # read files should not appear in DagFileSpec
        assert "src/helpers.py" not in dag.tasks["task-a"].files.create
        assert "src/helpers.py" not in dag.tasks["task-a"].files.edit
        assert "src/helpers.py" not in dag.tasks["task-a"].files.delete
        # But edit files should still be there
        assert "src/a.py" in dag.tasks["task-a"].files.edit

    def test_compile_merge_strategy_squash(self, tmp_path):
        """Compile with merge_strategy='squash' produces merge_squash=True."""
        spec = parse_plan_file(_write_plan(tmp_path))
        dag = compile_plan(spec)
        assert dag.merge_squash is True

    def test_compile_merge_strategy_rebase(self, tmp_path):
        """Modify plan to merge_strategy='rebase', compile produces merge_squash=False."""
        toml_content = """
[plan]
version = 1
name = "test-plan"
goal = "Test the plan system"
merge_strategy = "rebase"
default_agent = "qwen-9b"
default_timeout_s = 600

[units.task-a]
summary = "First task"
prompt = "Do task A"
commit_message = "Complete A"
[units.task-a.files]
edit = ["src/a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        dag = compile_plan(spec)
        assert dag.merge_squash is False


class TestCompilePlanConfigFlow:
    def test_compile_permission_mode(self, tmp_path):
        """permission_mode flows through to DagTaskSpec."""
        toml_content = """
[plan]
version = 1
name = "test"
goal = "test"
permission_mode = "default"

[units.a]
summary = "a"
prompt = "do a"
commit_message = "a"
[units.a.files]
edit = ["a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        dag = compile_plan(spec)
        assert dag.tasks["a"].permission_mode == "default"

    def test_compile_max_retries(self, tmp_path):
        """max_retries flows through to DagDefinition."""
        toml_content = """
[plan]
version = 1
name = "test"
goal = "test"
max_retries = 3

[units.a]
summary = "a"
prompt = "do a"
commit_message = "a"
[units.a.files]
edit = ["a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        dag = compile_plan(spec)
        assert dag.default_max_retries == 3

    def test_compile_merge_resolve(self, tmp_path):
        """merge_resolve flows through to DagDefinition."""
        toml_content = """
[plan]
version = 1
name = "test"
goal = "test"
merge_resolve = "rebase"

[units.a]
summary = "a"
prompt = "do a"
commit_message = "a"
[units.a.files]
edit = ["a.py"]
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        dag = compile_plan(spec)
        assert dag.merge_resolve == "rebase"

    def test_compile_custom_check_maps_to_post_merge(self, tmp_path):
        """acceptance.custom_check maps to DagTaskSpec.post_merge_check."""
        toml_content = """
[plan]
version = 1
name = "test"
goal = "test"

[units.a]
summary = "a"
prompt = "do a"
commit_message = "a"
[units.a.files]
edit = ["a.py"]
[units.a.acceptance]
custom_check = "uv run pytest tests/test_a.py -q"
"""
        spec = parse_plan_file(_write_plan(tmp_path, toml_content))
        dag = compile_plan(spec)
        assert dag.tasks["a"].post_merge_check == "uv run pytest tests/test_a.py -q"

    def test_compile_defaults_flow(self, tmp_path):
        """Default config values flow through correctly."""
        spec = parse_plan_file(_write_plan(tmp_path))
        dag = compile_plan(spec)
        assert dag.default_max_retries == 1
        assert dag.merge_resolve == "skip"
        assert dag.tasks["task-a"].permission_mode == "bypassPermissions"


class TestSerializePlan:
    def test_round_trip(self, tmp_path):
        """Parse -> serialize -> parse produces equivalent PlanSpec."""
        from dgov.plan import serialize_plan

        original = parse_plan_file(_write_plan(tmp_path))
        toml_str = serialize_plan(original)

        # Write serialized TOML and parse it back
        roundtrip_path = tmp_path / "roundtrip.toml"
        roundtrip_path.write_text(toml_str)
        roundtripped = parse_plan_file(str(roundtrip_path))

        assert roundtripped.name == original.name
        assert roundtripped.goal == original.goal
        assert roundtripped.evals == original.evals
        assert len(roundtripped.units) == len(original.units)
        for slug in original.units:
            assert slug in roundtripped.units
            orig_unit = original.units[slug]
            rt_unit = roundtripped.units[slug]
            assert rt_unit.summary == orig_unit.summary
            assert rt_unit.commit_message == orig_unit.commit_message
            assert rt_unit.files.edit == orig_unit.files.edit
            assert rt_unit.files.create == orig_unit.files.create
            assert rt_unit.depends_on == orig_unit.depends_on

    def test_serialize_contains_plan_header(self, tmp_path):
        """Serialized TOML contains [plan] section."""
        from dgov.plan import serialize_plan

        spec = parse_plan_file(_write_plan(tmp_path))
        toml_str = serialize_plan(spec)
        assert "[plan]" in toml_str
        assert 'name = "test-plan"' in toml_str
        assert 'goal = "Test the plan system"' in toml_str
        assert "[[evals]]" in toml_str
        assert 'id = "E1"' in toml_str

    def test_serialize_contains_units(self, tmp_path):
        """Serialized TOML contains unit sections."""
        from dgov.plan import serialize_plan

        spec = parse_plan_file(_write_plan(tmp_path))
        toml_str = serialize_plan(spec)
        assert "[units.task-a]" in toml_str
        assert "[units.task-b]" in toml_str
        assert 'satisfies = ["E1"]' in toml_str

    def test_serialize_non_default_config(self, tmp_path):
        """Non-default config values appear in serialized output."""
        from dgov.plan import serialize_plan

        unit = PlanUnit(
            slug="x",
            summary="x",
            prompt="do x",
            commit_message="x",
            files=PlanUnitFiles(edit=("a.py",)),
        )
        plan = PlanSpec(
            name="test",
            goal="test",
            units={"x": unit},
            evals=(
                PlanEval(
                    eval_id="E1",
                    kind="regression",
                    statement="x is implemented",
                    evidence="uv run pytest tests/test_x.py -q",
                ),
            ),
            max_concurrent=4,
            merge_strategy="rebase",
            default_agent="qwen-35b",
            max_retries=3,
        )
        toml_str = serialize_plan(plan)
        assert "max_concurrent = 4" in toml_str
        assert 'merge_strategy = "rebase"' in toml_str
        assert 'default_agent = "qwen-35b"' in toml_str
        assert "max_retries = 3" in toml_str


class TestRunPlan:
    def test_run_plan_validates(self, tmp_path):
        """run_plan raises ValueError on invalid plan."""
        from dgov.plan import run_plan

        toml_content = """
[plan]
version = 1
name = "bad"
goal = "test"
[[evals]]
id = "E1"
kind = "regression"
statement = "a should exist"
evidence = "uv run pytest tests/test_a.py -q"

[units.a]
summary = "a"
prompt = "do a"
commit_message = "a"
satisfies = ["E1"]
depends_on = ["nonexistent"]
[units.a.files]
edit = ["a.py"]
"""
        plan_path = _write_plan(tmp_path, toml_content)
        with pytest.raises(ValueError, match="validation failed"):
            run_plan(plan_path)

    def test_run_plan_rejects_bad_version(self, tmp_path):
        """run_plan raises ValueError on unsupported version."""
        from dgov.plan import run_plan

        toml_content = """
[plan]
version = 99
name = "bad"
goal = "test"
[[evals]]
id = "E1"
kind = "regression"
statement = "a should exist"
evidence = "uv run pytest tests/test_a.py -q"

[units.a]
summary = "a"
prompt = "do a"
commit_message = "a"
satisfies = ["E1"]
[units.a.files]
edit = ["a.py"]
"""
        plan_path = _write_plan(tmp_path, toml_content)
        with pytest.raises(ValueError, match="Unsupported plan version"):
            run_plan(plan_path)

    def test_run_plan_passes_eval_contract_to_kernel(self, tmp_path, monkeypatch):
        """run_plan persists evals as typed contract rows via the kernel submit path."""
        from dgov.plan import run_plan

        captured: dict[str, object] = {}

        def fake_run_dag_via_kernel(dag, **kwargs):
            captured["dag"] = dag
            captured["kwargs"] = kwargs
            return object()

        monkeypatch.setattr("dgov.dag.run_dag_via_kernel", fake_run_dag_via_kernel)

        plan_path = _write_plan(tmp_path)
        run_plan(plan_path)

        kwargs = captured["kwargs"]
        assert kwargs["plan_evals"] == [
            {
                "eval_id": "E1",
                "kind": "regression",
                "statement": "Task A updates src/a.py.",
                "evidence": "uv run pytest tests/test_a.py -q",
                "scope": ["src/a.py"],
            },
            {
                "eval_id": "E2",
                "kind": "happy_path",
                "statement": "Task B creates src/b.py.",
                "evidence": "uv run pytest tests/test_b.py -q",
                "scope": ["src/b.py"],
            },
        ]
        assert kwargs["unit_eval_links"] == [
            {"unit_slug": "task-a", "eval_id": "E1"},
            {"unit_slug": "task-b", "eval_id": "E2"},
        ]


class TestLTGovCompilation:
    def test_compile_lt_gov_task(self, tmp_path):
        """Compile an LT-GOV task with template variables."""
        # Create a mock template directory and file
        tpl_dir = tmp_path / ".dgov" / "templates"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "lt-gov.toml").write_text("""
name = "lt-gov"
template = "LT: {ltgov_slug}, Agent: {default_agent}, Tasks: {task_list}"
required_vars = ["ltgov_slug", "default_agent", "task_list"]
""")

        plan_toml = f"""
[plan]
version = 1
name = "lt-plan"
goal = "Test LT-GOV"
session_root = "{tmp_path}"
[[evals]]
id = "E1"
kind = "manual"
statement = "LT-GOV dispatches the planned worker set."
evidence = "Review .dgov/progress/lt-task.json after completion."

[units.lt-task]
summary = "LT summary"
prompt = "ignored"
commit_message = "LT commit"
satisfies = ["E1"]
role = "lt-gov"
template = "lt-gov"
[units.lt-task.vars]
task_list = "1. a, 2. b"
[units.lt-task.files]
edit = ["src/api.py"]
"""

        plan_file = _write_plan(tmp_path, plan_toml)
        plan = parse_plan_file(plan_file)
        dag = compile_plan(plan)

        task = dag.tasks["lt-task"]
        assert task.role == "lt-gov"
        assert task.prompt.startswith("LT: lt-task, Agent: qwen-9b, Tasks: 1. a, 2. b")
        assert "## Evals to satisfy" in task.prompt
        assert "[E1] manual: LT-GOV dispatches the planned worker set." in task.prompt
        assert task.template == "lt-gov"
        assert task.template_vars == {"task_list": "1. a, 2. b"}
