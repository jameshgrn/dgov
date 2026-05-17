"""Tests for dgov/serializer.py module.

Tests cover:
- _toml_str(value) - string escaping for TOML double-quoted strings
- _toml_ml_str(value) - multi-line TOML string handling
- _toml_key(fq_id) - TOML key quoting
- serialize_compiled_toml(bundle_result, source_mtime_max) - full serialization
"""

import tomllib
from pathlib import Path

import pytest

from dgov.plan import PlanUnit, PlanUnitFiles
from dgov.plan_tree import FlatPlan, RootMeta
from dgov.serializer import _toml_key, _toml_ml_str, _toml_str, serialize_compiled_toml
from dgov.sop_bundler import BundleResult, IdentityBundler

# =============================================================================
# _toml_str tests
# =============================================================================


class TestTomlStr:
    """Test _toml_str function for escaping strings into TOML double-quoted format."""

    def test_plain_string_double_quoted(self):
        """Plain string should be wrapped in double quotes."""
        result = _toml_str("hello world")
        assert result == '"hello world"'

    def test_string_with_quotes_escaped(self):
        """String with quotes should have them escaped."""
        result = _toml_str('say "hello"')
        assert result == '"say \\"hello\\""'

    def test_string_with_newlines_escaped(self):
        """String with newlines should have them escaped as \\n."""
        result = _toml_str("line1\nline2")
        assert result == '"line1\\nline2"'

    def test_string_with_backslashes_escaped(self):
        """String with backslashes should have them escaped."""
        result = _toml_str("path\\to\\file")
        assert result == '"path\\\\to\\\\file"'

    def test_string_with_mixed_special_chars(self):
        """String with multiple special characters should be properly escaped."""
        result = _toml_str('line1\n"quoted"\\path')
        assert result == '"line1\\n\\"quoted\\"\\\\path"'


# =============================================================================
# _toml_ml_str tests
# =============================================================================


class TestTomlMlStr:
    """Test _toml_ml_str function for handling multi-line strings."""

    def test_single_line_falls_through_to_toml_str(self):
        """Single-line string should use double-quoted format via _toml_str."""
        result = _toml_ml_str("single line")
        assert result == '"single line"'

    def test_single_line_with_quotes_escaped(self):
        """Single-line string with quotes should be escaped like _toml_str."""
        result = _toml_ml_str('say "hello"')
        assert result == '"say \\"hello\\""'

    def test_multi_line_uses_triple_quotes(self):
        """Multi-line string should use triple-quoted format."""
        result = _toml_ml_str("line1\nline2\nline3")
        assert result.startswith('"""\n')
        assert result.endswith('"""')
        assert "line1\nline2\nline3" in result

    def test_multi_line_backslash_escaped(self):
        """Multi-line string should escape backslashes."""
        result = _toml_ml_str("path\\to\\file")
        # In multi-line mode, backslashes are doubled
        assert "\\\\" in result

    def test_multi_line_triple_quote_escaped(self):
        """Triple-quotes in content must be escaped or they terminate the TOML string."""
        value = 'step 1:\n    def foo():\n        """docstring"""\n        pass\n'
        result = _toml_ml_str(value)
        # The output must be parseable TOML — no raw """ inside the string body
        import tomllib

        toml_src = f"x = {result}"
        parsed = tomllib.loads(toml_src)
        assert '"""' in parsed["x"]  # round-trip preserves the triple-quotes


# =============================================================================
# _toml_key tests
# =============================================================================


class TestTomlKey:
    """Test _toml_key function for TOML key formatting."""

    def test_simple_alphanumeric_slug_bare(self):
        """Simple alphanumeric slug should be returned as bare key."""
        result = _toml_key("simple-slug")
        assert result == "simple-slug"

    def test_slug_with_underscores_bare(self):
        """Slug with underscores should be bare key."""
        result = _toml_key("my_unit_name")
        assert result == "my_unit_name"

    def test_slug_with_dots_quoted(self):
        """Slug with dots should be quoted."""
        result = _toml_key("section.file.slug")
        assert result == '"section.file.slug"'

    def test_slug_with_slashes_quoted(self):
        """Slug with slashes should be quoted."""
        result = _toml_key("section/file")
        assert result == '"section/file"'

    def test_slug_with_mixed_special_chars_quoted(self):
        """Slug with both dots and slashes should be quoted."""
        result = _toml_key("section/file.stem.slug")
        assert result == '"section/file.stem.slug"'

    def test_slug_with_numbers_and_underscores_bare(self):
        """Slug with numbers and underscores should be bare key."""
        result = _toml_key("unit_123_v2")
        assert result == "unit_123_v2"


# =============================================================================
# serialize_compiled_toml tests
# =============================================================================


def _create_test_root_meta() -> RootMeta:
    """Create a standard RootMeta for test plans."""
    return RootMeta(
        name="test-plan",
        summary="Test plan summary",
        sections=("section1",),
    )


def _create_unit_a() -> PlanUnit:
    """Create unit A with edit and create file claims, no dependencies."""
    return PlanUnit(
        slug="section1/file1.unit_a",
        summary="First unit",
        prompt="Do first thing",
        commit_message="First done",
        files=PlanUnitFiles(
            edit=("src/main.py",),
            create=("src/new.py",),
        ),
        depends_on=(),
    )


def _create_unit_b() -> PlanUnit:
    """Create unit B with delete file claim, depends on unit A."""
    return PlanUnit(
        slug="section1/file1.unit_b",
        summary="Second unit",
        prompt="Do second thing\nWith multiple lines",
        commit_message="Second done",
        files=PlanUnitFiles(
            delete=("old_file.py",),
        ),
        depends_on=("section1/file1.unit_a",),
    )


def _create_test_source_map(plan_root: Path) -> dict[str, Path]:
    """Create source map mapping unit slugs to their source TOML files."""
    toml_path = plan_root / "section1" / "file1.toml"
    return {
        "section1/file1.unit_a": toml_path,
        "section1/file1.unit_b": toml_path,
    }


def _create_flat_plan(tmp_path) -> FlatPlan:
    """Create a minimal FlatPlan with 2 units for testing."""
    plan_root = tmp_path / "test_plan"
    plan_root.mkdir()

    unit_a = _create_unit_a()
    unit_b = _create_unit_b()

    return FlatPlan(
        plan_root=plan_root,
        root_meta=_create_test_root_meta(),
        units={
            "section1/file1.unit_a": unit_a,
            "section1/file1.unit_b": unit_b,
        },
        source_map=_create_test_source_map(plan_root),
        source_mtime_max=1234567890.0,
    )


def _create_bundle_result(flat_plan: FlatPlan) -> BundleResult:
    """Create a BundleResult using IdentityBundler."""
    bundler = IdentityBundler()
    mapping_dict = bundler.pick(flat_plan.units, [])
    # Convert list to tuple for immutability
    sop_mapping = {k: tuple(v) for k, v in mapping_dict.items()}

    return BundleResult(
        plan=flat_plan,
        sop_mapping=sop_mapping,
        sop_set_hash="abc123def456",
    )


def _extract_task_section(serialized: str, task_key: str) -> str:
    """Extract a single task section from serialized TOML output.

    Args:
        serialized: The full TOML output from serialize_compiled_toml.
        task_key: The task key (e.g., 'section1/file.complete_task').

    Returns:
        The task section as a string, or empty string if not found.
    """
    section_header = f'[tasks."{task_key}"]'
    start = serialized.find(section_header)
    if start == -1:
        return ""

    # Find the end of this section (next section header or end of string)
    end = serialized.find("\n[", start + 1)
    if end == -1:
        end = len(serialized)

    return serialized[start:end]


def _find_field_position(task_section: str, field_pattern: str) -> int:
    """Find the position of a field within a task section.

    Args:
        task_section: The task section string (from _extract_task_section).
        field_pattern: The field pattern to search for (e.g., 'timeout_s = 300').

    Returns:
        The position index, or -1 if not found.
    """
    return task_section.find(field_pattern)


def _create_complete_optional_fields_plan(tmp_path) -> tuple[FlatPlan, BundleResult]:
    """Create a FlatPlan and BundleResult with all optional fields populated.

    Creates a plan with timeout_s, iteration_budget, test_cmd, and sop_mapping
    to test field ordering in serialized output.

    Returns:
        Tuple of (flat_plan, bundle_result) ready for serialization.
    """
    plan_root = tmp_path / "test_plan"
    plan_root.mkdir()

    root_meta = RootMeta(
        name="ordering-plan",
        summary="Test field ordering",
        sections=("section1",),
    )

    unit = PlanUnit(
        slug="section1/file.complete_task",
        summary="Complete task with all optional fields",
        prompt="Do everything",
        commit_message="Complete",
        files=PlanUnitFiles(edit=("src/main.py",)),
        timeout_s=300,
        iteration_budget=5,
        test_cmd="pytest tests/test_main.py",
    )

    flat_plan = FlatPlan(
        plan_root=plan_root,
        root_meta=root_meta,
        units={"section1/file.complete_task": unit},
        source_map={"section1/file.complete_task": plan_root / "section1" / "file.toml"},
        source_mtime_max=1234567890.0,
    )

    bundle = BundleResult(
        plan=flat_plan,
        sop_mapping={"section1/file.complete_task": ("sop-a", "sop-b")},
        sop_set_hash="ordering_hash",
    )

    return flat_plan, bundle


def _assert_field_order(task_section: str, field_specs: list[tuple[str, str]]) -> None:
    """Assert that fields appear in task_section in the specified order.

    Args:
        task_section: The task section extracted from serialized TOML.
        field_specs: List of (field_pattern, field_name) tuples defining
            the expected order. Each pattern is searched for in the section.

    Raises:
        AssertionError: If any field is missing or ordering is violated.
    """
    positions: list[int] = []
    field_names: list[str] = []

    for pattern, name in field_specs:
        pos = _find_field_position(task_section, pattern)
        if pos == -1:
            raise AssertionError(f"{name} not found in task section")
        positions.append(pos)
        field_names.append(name)

    # Verify strict ascending order
    for i in range(len(positions) - 1):
        if positions[i] >= positions[i + 1]:
            details = ", ".join(
                f"{name}={pos}" for name, pos in zip(field_names, positions, strict=True)
            )
            raise AssertionError(f"Field ordering violated: {details}")


class TestSerializeCompiledToml:
    """Test serialize_compiled_toml for full TOML output generation."""

    def test_starts_with_plan_section(self, tmp_path):
        """Output should start with [plan] section."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert result.startswith("[plan]\n")

    def test_contains_required_plan_fields(self, tmp_path):
        """Output should contain name, source_mtime_max, sop_set_hash in [plan]."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'name = "test-plan"' in result
        assert "source_mtime_max = " in result
        assert 'sop_set_hash = "abc123def456"' in result

    def test_contains_task_sections(self, tmp_path):
        """Output should contain [tasks."..."] sections for each unit."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        # Keys with special chars should be quoted
        assert '[tasks."section1/file1.unit_a"]' in result
        assert '[tasks."section1/file1.unit_b"]' in result

    def test_contains_summary_and_commit_message(self, tmp_path):
        """Output should include summary and commit_message for each unit."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'summary = "First unit"' in result
        assert 'summary = "Second unit"' in result
        assert 'commit_message = "First done"' in result
        assert 'commit_message = "Second done"' in result

    def test_contains_prompt_with_appropriate_format(self, tmp_path):
        """Output should include prompt field."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        # Single-line prompt should be double-quoted
        assert 'prompt = "Do first thing"' in result
        # Multi-line prompt should use triple quotes
        assert 'prompt = """\nDo second thing\nWith multiple lines"""' in result

    def test_contains_depends_on(self, tmp_path):
        """Output should include depends_on for unit with dependencies."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        # Unit B depends on unit A - depends_on should be quoted array
        assert 'depends_on = ["section1/file1.unit_a"]' in result

    def test_contains_files_edit_and_create(self, tmp_path):
        """Output should include files.edit and files.create where present."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'files.edit = ["src/main.py"]' in result
        assert 'files.create = ["src/new.py"]' in result

    def test_contains_task_test_cmd(self, tmp_path):
        """Output should include task-level test_cmd overrides when present."""
        flat_plan = _create_flat_plan(tmp_path)
        unit_a = flat_plan.units["section1/file1.unit_a"]
        flat_plan = FlatPlan(
            plan_root=flat_plan.plan_root,
            root_meta=flat_plan.root_meta,
            units={
                **flat_plan.units,
                "section1/file1.unit_a": PlanUnit(
                    slug=unit_a.slug,
                    summary=unit_a.summary,
                    prompt=unit_a.prompt,
                    commit_message=unit_a.commit_message,
                    files=unit_a.files,
                    depends_on=unit_a.depends_on,
                    test_cmd="./scripts/qgis-python.sh -m pytest tests/plugin/test_a.py",
                ),
            },
            source_map=flat_plan.source_map,
            source_mtime_max=flat_plan.source_mtime_max,
        )
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'test_cmd = "./scripts/qgis-python.sh -m pytest tests/plugin/test_a.py"' in result

    def test_contains_files_delete(self, tmp_path):
        """Output should include files.delete where present."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'files.delete = ["old_file.py"]' in result

    def test_contains_files_read(self, tmp_path):
        """Output should include files.read when present."""
        flat_plan = _create_flat_plan(tmp_path)
        unit_b = flat_plan.units["section1/file1.unit_b"]
        flat_plan = FlatPlan(
            plan_root=flat_plan.plan_root,
            root_meta=flat_plan.root_meta,
            units={
                **flat_plan.units,
                "section1/file1.unit_b": PlanUnit(
                    slug=unit_b.slug,
                    summary=unit_b.summary,
                    prompt=unit_b.prompt,
                    commit_message=unit_b.commit_message,
                    files=PlanUnitFiles(
                        delete=unit_b.files.delete,
                        read=("src/main.py", "docs/spec.md"),
                    ),
                    depends_on=unit_b.depends_on,
                ),
            },
            source_map=flat_plan.source_map,
            source_mtime_max=flat_plan.source_mtime_max,
        )
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'files.read = ["src/main.py", "docs/spec.md"]' in result

    def test_is_valid_toml_round_trip(self, tmp_path):
        """Output should be valid TOML that can be parsed by tomllib."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        # Should not raise an exception
        parsed = tomllib.loads(result)

        assert "plan" in parsed
        assert parsed["plan"]["name"] == "test-plan"
        assert parsed["plan"]["sop_set_hash"] == "abc123def456"
        assert "tasks" in parsed

    def test_tasks_parsed_correctly(self, tmp_path):
        """Round-trip should parse tasks with correct structure."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)
        parsed = tomllib.loads(result)

        tasks = parsed["tasks"]
        # Check that our units are present as keys
        assert "section1/file1.unit_a" in tasks
        assert "section1/file1.unit_b" in tasks

        # Verify unit A structure
        unit_a = tasks["section1/file1.unit_a"]
        assert unit_a["summary"] == "First unit"
        assert unit_a["files"]["edit"] == ["src/main.py"]

        # Verify unit B structure
        unit_b = tasks["section1/file1.unit_b"]
        assert unit_b["summary"] == "Second unit"
        assert unit_b["depends_on"] == ["section1/file1.unit_a"]


class TestSerializeCompiledTomlFlatFiles:
    """Test serialization of flat files (touch) format."""

    def test_touch_only_serialized_as_flat_list(self, tmp_path):
        """Pure touch files should serialize as `files = [...]`."""
        plan_root = tmp_path / "test_plan"
        plan_root.mkdir()
        root_meta = RootMeta(name="touch-plan", summary="", sections=("s1",))
        unit = PlanUnit(
            slug="s1/f.task",
            summary="Task",
            prompt="Do",
            commit_message="Done",
            files=PlanUnitFiles(touch=("src/foo.py", "tests/test_foo.py")),
        )
        flat_plan = FlatPlan(
            plan_root=plan_root,
            root_meta=root_meta,
            units={"s1/f.task": unit},
            source_map={"s1/f.task": plan_root / "s1" / "f.toml"},
            source_mtime_max=1234567890.0,
        )
        bundle = BundleResult(plan=flat_plan, sop_mapping={"s1/f.task": ()}, sop_set_hash="h")
        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'files = ["src/foo.py", "tests/test_foo.py"]' in result
        assert "files.touch" not in result
        assert "files.edit" not in result

    def test_touch_with_delete_serialized_as_subtable(self, tmp_path):
        """Mixed touch + delete should use subtable format."""
        plan_root = tmp_path / "test_plan"
        plan_root.mkdir()
        root_meta = RootMeta(name="mixed-plan", summary="", sections=("s1",))
        unit = PlanUnit(
            slug="s1/f.task",
            summary="Task",
            prompt="Do",
            commit_message="Done",
            files=PlanUnitFiles(touch=("src/foo.py",), delete=("old.py",)),
        )
        flat_plan = FlatPlan(
            plan_root=plan_root,
            root_meta=root_meta,
            units={"s1/f.task": unit},
            source_map={"s1/f.task": plan_root / "s1" / "f.toml"},
            source_mtime_max=1234567890.0,
        )
        bundle = BundleResult(plan=flat_plan, sop_mapping={"s1/f.task": ()}, sop_set_hash="h")
        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'files.touch = ["src/foo.py"]' in result
        assert 'files.delete = ["old.py"]' in result

    def test_flat_files_round_trip(self, tmp_path):
        """Flat files serialization should round-trip through tomllib."""
        plan_root = tmp_path / "test_plan"
        plan_root.mkdir()
        root_meta = RootMeta(name="rt-plan", summary="", sections=("s1",))
        unit = PlanUnit(
            slug="s1/f.task",
            summary="Task",
            prompt="Do",
            commit_message="Done",
            files=PlanUnitFiles(touch=("a.py", "b.py")),
        )
        flat_plan = FlatPlan(
            plan_root=plan_root,
            root_meta=root_meta,
            units={"s1/f.task": unit},
            source_map={"s1/f.task": plan_root / "s1" / "f.toml"},
            source_mtime_max=1234567890.0,
        )
        bundle = BundleResult(plan=flat_plan, sop_mapping={"s1/f.task": ()}, sop_set_hash="h")
        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        parsed = tomllib.loads(result)
        task = parsed["tasks"]["s1/f.task"]
        assert task["files"] == ["a.py", "b.py"]


class TestSerializeCompiledTomlWithSopMapping:
    """Test serialize_compiled_toml with SOP mapping entries."""

    def test_sop_mapping_appears_in_output(self, tmp_path):
        """SOP mapping should appear in serialized output when present."""
        flat_plan = _create_flat_plan(tmp_path)

        # Create bundle with SOP mapping entries
        sop_mapping: dict[str, tuple[str, ...]] = {
            "section1/file1.unit_a": ("sop-a", "sop-b"),
            "section1/file1.unit_b": ("sop-c",),
        }
        bundle = BundleResult(
            plan=flat_plan,
            sop_mapping=sop_mapping,
            sop_set_hash="hash_with_sops",
        )

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        # Check that sop_mapping entries appear
        assert 'sop_mapping = ["sop-a", "sop-b"]' in result
        assert 'sop_mapping = ["sop-c"]' in result


class TestSerializeCompiledTomlWithAgentAndTimeout:
    """Test serialize_compiled_toml with agent and timeout_s fields."""

    def _agent_timeout_bundle(self, tmp_path) -> BundleResult:
        plan_root = tmp_path / "test_plan"
        plan_root.mkdir()
        root_meta = RootMeta(
            name="agent-plan",
            summary="Test with agent",
            sections=("section1",),
            default_agent="gpt-test",
            default_provider="openai",
        )

        unit = PlanUnit(
            slug="section1/file.agent_task",
            summary="Agent task",
            prompt="Do something",
            commit_message="Done",
            files=PlanUnitFiles(),
            agent="provider/model-name",
            provider="llm",
            timeout_s=1200,
        )
        flat_plan = FlatPlan(
            plan_root=plan_root,
            root_meta=root_meta,
            units={"section1/file.agent_task": unit},
            source_map={"section1/file.agent_task": plan_root / "section1" / "file.toml"},
            source_mtime_max=1234567890.0,
        )
        return BundleResult(
            plan=flat_plan,
            sop_mapping={"section1/file.agent_task": ()},
            sop_set_hash="agent_hash",
        )

    def test_agent_and_timeout_in_output(self, tmp_path):
        """Agent and timeout_s should appear in output when populated."""
        bundle = self._agent_timeout_bundle(tmp_path)
        flat_plan = bundle.plan
        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'default_agent = "gpt-test"' in result
        assert 'agent = "provider/model-name"' in result
        assert 'default_provider = "openai"' in result
        assert 'provider = "llm"' in result
        assert "timeout_s = 1200" in result

    def test_agent_and_timeout_not_present_when_empty(self, tmp_path):
        """Agent and timeout_s should not appear when not set."""
        flat_plan = _create_flat_plan(tmp_path)
        bundle = _create_bundle_result(flat_plan)

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        # Original units don't have agent or timeout set
        assert "agent =" not in result
        assert "timeout_s =" not in result

    def test_role_emitted_for_researcher_tasks(self, tmp_path):
        """Non-default task roles should round-trip through compiled TOML."""
        plan_root = tmp_path / "test_plan"
        plan_root.mkdir()

        root_meta = RootMeta(
            name="research-plan",
            summary="Test with researcher role",
            sections=("section1",),
        )

        unit = PlanUnit(
            slug="section1/file.research_task",
            summary="Research task",
            prompt="Investigate something",
            commit_message="Done",
            files=PlanUnitFiles(),
            role="researcher",
        )

        flat_plan = FlatPlan(
            plan_root=plan_root,
            root_meta=root_meta,
            units={"section1/file.research_task": unit},
            source_map={"section1/file.research_task": plan_root / "section1" / "file.toml"},
            source_mtime_max=1234567890.0,
        )

        bundle = BundleResult(
            plan=flat_plan,
            sop_mapping={"section1/file.research_task": ()},
            sop_set_hash="research_hash",
        )

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert 'role = "researcher"' in result

    def test_iteration_budget_emitted_for_task_override(self, tmp_path):
        """Task-local iteration budgets should round-trip through compiled TOML."""
        plan_root = tmp_path / "test_plan"
        plan_root.mkdir()

        root_meta = RootMeta(
            name="iteration-plan",
            summary="Test with task iteration budget",
            sections=("section1",),
        )

        unit = PlanUnit(
            slug="section1/file.focused_task",
            summary="Focused task",
            prompt="Implement carefully",
            commit_message="Done",
            files=PlanUnitFiles(),
            iteration_budget=12,
        )

        flat_plan = FlatPlan(
            plan_root=plan_root,
            root_meta=root_meta,
            units={"section1/file.focused_task": unit},
            source_map={"section1/file.focused_task": plan_root / "section1" / "file.toml"},
            source_mtime_max=1234567890.0,
        )

        bundle = BundleResult(
            plan=flat_plan,
            sop_mapping={"section1/file.focused_task": ()},
            sop_set_hash="iteration_hash",
        )

        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        assert "iteration_budget = 12" in result

    @pytest.mark.unit
    def test_sop_mapping_appears_after_numeric_and_test_cmd_fields(self, tmp_path):
        """sop_mapping should be emitted after timeout_s, iteration_budget, and test_cmd.

        This ensures the original field ordering is preserved: agent, role,
        depends_on, timeout_s, iteration_budget, test_cmd, sop_mapping, then files.
        """
        flat_plan, bundle = _create_complete_optional_fields_plan(tmp_path)
        result = serialize_compiled_toml(bundle, flat_plan.source_mtime_max)

        task_section = _extract_task_section(result, "section1/file.complete_task")
        assert task_section, "Task section not found"

        _assert_field_order(
            task_section,
            [
                ("timeout_s = 300", "timeout_s"),
                ("iteration_budget = 5", "iteration_budget"),
                ('test_cmd = "pytest tests/test_main.py"', "test_cmd"),
                ('sop_mapping = ["sop-a", "sop-b"]', "sop_mapping"),
            ],
        )
