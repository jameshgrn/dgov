"""Tests for DAG graph algorithms: validation, topological sort, tier computation."""

import pytest

from dgov.dag_graph import (
    compute_tiers,
    render_dry_run,
    topological_order,
    transitive_dependents,
    validate_dag,
)
from dgov.dag_parser import DagFileSpec, DagTaskSpec

pytestmark = pytest.mark.unit


def make_task(
    slug: str,
    summary: str = "test",
    agent: str = "qwen-35b",
    depends_on: tuple[str, ...] = (),
    files_create: tuple[str, ...] = (),
    files_edit: tuple[str, ...] = (),
    files_delete: tuple[str, ...] = (),
) -> DagTaskSpec:
    """Helper to create a DagTaskSpec for testing."""
    return DagTaskSpec(
        slug=slug,
        summary=summary,
        prompt="test prompt",
        commit_message="test commit",
        agent=agent,
        escalation=(),
        depends_on=depends_on,
        files=DagFileSpec(create=files_create, edit=files_edit, delete=files_delete),
        permission_mode="bypassPermissions",
        timeout_s=900,
    )


class TestValidateDag:
    """Tests for validate_dag function."""

    def test_empty_graph(self):
        """Empty task dict is valid."""
        assert validate_dag({}) is None

    def test_single_node_no_deps(self):
        """Single node with no dependencies is valid."""
        tasks = {"a": make_task("a")}
        assert validate_dag(tasks) is None

    def test_valid_linear_chain(self):
        """Linear chain of dependencies is valid."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b", depends_on=("a",)),
            "c": make_task("c", depends_on=("b",)),
        }
        assert validate_dag(tasks) is None

    def test_valid_diamond_dependency(self):
        """Diamond dependency pattern is valid."""
        tasks = {
            "top": make_task("top"),
            "left": make_task("left", depends_on=("top",)),
            "right": make_task("right", depends_on=("top",)),
            "bottom": make_task("bottom", depends_on=("left", "right")),
        }
        assert validate_dag(tasks) is None

    def test_disconnected_nodes(self):
        """Multiple disconnected components are valid."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b"),
            "c": make_task("c", depends_on=("a",)),
            "d": make_task("d"),
        }
        assert validate_dag(tasks) is None

    def test_missing_dependency_raises_valueerror(self):
        """Dependency on non-existent task raises ValueError."""
        tasks = {"a": make_task("a", depends_on=("nonexistent",))}
        expected_msg = r"Task 'a' depends on 'nonexistent' "
        expected_msg += r"which does not exist"
        with pytest.raises(ValueError, match=expected_msg):
            validate_dag(tasks)

    def test_self_dependency_raises_cycle_error(self):
        """Self-referential dependency raises cycle error."""
        tasks = {"a": make_task("a", depends_on=("a",))}
        with pytest.raises(ValueError, match=r"Dependency cycle detected involving 'a'"):
            validate_dag(tasks)

    def test_simple_cycle_raises_cycle_error(self):
        """Simple A->B->A cycle raises cycle error."""
        tasks = {
            "a": make_task("a", depends_on=("b",)),
            "b": make_task("b", depends_on=("a",)),
        }
        with pytest.raises(ValueError, match=r"Dependency cycle detected involving 'a'"):
            validate_dag(tasks)

    def test_complex_cycle_raises_cycle_error(self):
        """Complex cycle through multiple nodes raises cycle error."""
        tasks = {
            "a": make_task("a", depends_on=("c",)),
            "b": make_task("b", depends_on=("a",)),
            "c": make_task("c", depends_on=("b",)),
        }
        with pytest.raises(ValueError, match=r"Dependency cycle detected involving 'a'"):
            validate_dag(tasks)


class TestTopologicalOrder:
    """Tests for topological_order function."""

    def test_empty_graph_returns_empty_list(self):
        """Empty task dict returns empty list."""
        assert topological_order({}) == []

    def test_single_node(self):
        """Single node returns single-element list."""
        tasks = {"a": make_task("a")}
        assert topological_order(tasks) == ["a"]

    def test_linear_chain_respects_order(self):
        """Linear chain respects dependency order."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b", depends_on=("a",)),
            "c": make_task("c", depends_on=("b",)),
        }
        result = topological_order(tasks)
        assert result.index("a") < result.index("b") < result.index("c")

    def test_diamond_dependency(self):
        """Diamond dependency: top before bottom, left/right in between."""
        tasks = {
            "top": make_task("top"),
            "left": make_task("left", depends_on=("top",)),
            "right": make_task("right", depends_on=("top",)),
            "bottom": make_task("bottom", depends_on=("left", "right")),
        }
        result = topological_order(tasks)
        assert result.index("top") < result.index("left")
        assert result.index("top") < result.index("right")
        assert result.index("left") < result.index("bottom")
        assert result.index("right") < result.index("bottom")

    def test_disconnected_nodes_stable_order(self):
        """Disconnected nodes appear in sorted order."""
        tasks = {
            "z": make_task("z"),
            "a": make_task("a"),
            "m": make_task("m"),
        }
        result = topological_order(tasks)
        assert result == ["a", "m", "z"]

    def test_complex_dag(self):
        """Complex DAG with multiple levels."""
        tasks = {
            "root": make_task("root"),
            "level1_a": make_task("level1_a", depends_on=("root",)),
            "level1_b": make_task("level1_b", depends_on=("root",)),
            "level2_a": make_task("level2_a", depends_on=("level1_a",)),
            "level2_b": make_task("level2_b", depends_on=("level1_a", "level1_b")),
            "leaf": make_task("leaf", depends_on=("level2_a", "level2_b")),
        }
        result = topological_order(tasks)
        assert result.index("root") == 0
        assert result.index("leaf") == len(result) - 1
        # All intermediate nodes come after root and before leaf
        for node in ("level1_a", "level1_b", "level2_a", "level2_b"):
            assert result.index("root") < result.index(node) < result.index("leaf")

    def test_cycle_raises_valueerror(self):
        """Cyclic graph raises ValueError from validate_dag."""
        tasks = {
            "a": make_task("a", depends_on=("b",)),
            "b": make_task("b", depends_on=("a",)),
        }
        with pytest.raises(ValueError, match=r"Dependency cycle detected"):
            topological_order(tasks)


class TestComputeTiers:
    """Tests for compute_tiers function."""

    def test_empty_graph_returns_empty_list(self):
        """Empty task dict returns empty list."""
        assert compute_tiers({}) == []

    def test_single_node_single_tier(self):
        """Single node forms one tier."""
        tasks = {"a": make_task("a")}
        result = compute_tiers(tasks)
        assert result == [["a"]]

    def test_linear_chain_separate_tiers(self):
        """Linear chain puts each node in separate tier."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b", depends_on=("a",)),
            "c": make_task("c", depends_on=("b",)),
        }
        result = compute_tiers(tasks)
        assert result == [["a"], ["b"], ["c"]]

    def test_diamond_all_ready_at_same_time_in_one_tier(self):
        """Nodes ready at same time go into same tier if no file overlap."""
        tasks = {
            "top": make_task("top"),
            "left": make_task("left", depends_on=("top",)),
            "right": make_task("right", depends_on=("top",)),
            "bottom": make_task("bottom", depends_on=("left", "right")),
        }
        result = compute_tiers(tasks)
        # top in tier 0, left+right in tier 1 (parallel), bottom in tier 2
        assert result[0] == ["top"]
        assert set(result[1]) == {"left", "right"}
        assert result[2] == ["bottom"]

    def test_file_overlap_forces_sequential_tiers(self):
        """Tasks editing same file must be in different tiers."""
        tasks = {
            "a": make_task("a", files_edit=("src/foo.py",)),
            "b": make_task("b", depends_on=("a",), files_edit=("src/foo.py",)),
        }
        result = compute_tiers(tasks)
        assert result == [["a"], ["b"]]

    def test_different_files_allow_parallel_tiers(self):
        """Tasks with different files can be in same tier."""
        tasks = {
            "a": make_task("a", files_edit=("src/a.py",)),
            "b": make_task("b", files_edit=("src/b.py",)),
        }
        result = compute_tiers(tasks)
        assert set(result[0]) == {"a", "b"}

    def test_nested_path_overlap(self):
        """Nested paths like src/foo/ and src/foo/bar.py conflict."""
        tasks = {
            "a": make_task("a", files_edit=("src/foo/",)),
            "b": make_task("b", files_edit=("src/foo/bar.py",)),
        }
        result = compute_tiers(tasks)
        # These overlap, so they must be sequential
        assert len(result) == 2
        assert set(result[0]).issubset({"a", "b"})
        assert set(result[1]).issubset({"a", "b"})

    def test_disconnected_components_parallel(self):
        """Disconnected components can run in parallel."""
        tasks = {
            "a": make_task("a", files_edit=("src/a.py",)),
            "b": make_task("b", files_edit=("src/b.py",)),
            "c": make_task("c", files_edit=("src/c.py",)),
        }
        result = compute_tiers(tasks)
        assert set(result[0]) == {"a", "b", "c"}

    def test_mixed_dependencies_and_overlaps(self):
        """Complex case with both deps and file overlaps."""
        root = make_task("root", files_edit=("src/root.py",))
        left = make_task(
            "left",
            depends_on=("root",),
            files_edit=("src/left.py",),
        )
        right = make_task(
            "right",
            depends_on=("root",),
            files_edit=("src/left.py",),
        )
        bottom = make_task(
            "bottom",
            depends_on=("left", "right"),
            files_edit=("src/bottom.py",),
        )
        tasks = {"root": root, "left": left, "right": right, "bottom": bottom}
        result = compute_tiers(tasks)
        # root in tier 0
        # left and right have file overlap, so only one per tier
        # bottom needs both left and right
        assert result[0] == ["root"]
        # left and right share file, so one in tier 1, one in tier 2
        # bottom needs both, so tier 3
        assert len(result) >= 3

    def test_cycle_raises_valueerror(self):
        """Cyclic graph raises ValueError from validate_dag."""
        tasks = {
            "a": make_task("a", depends_on=("b",)),
            "b": make_task("b", depends_on=("a",)),
        }
        with pytest.raises(ValueError, match=r"Dependency cycle detected"):
            compute_tiers(tasks)

    def test_triple_diamond_parallelism(self):
        """Triple diamond shows proper parallel scheduling."""
        tasks = {
            "t0": make_task("t0"),
            "t1a": make_task("t1a", depends_on=("t0",)),
            "t1b": make_task("t1b", depends_on=("t0",)),
            "t1c": make_task("t1c", depends_on=("t0",)),
            "t2a": make_task("t2a", depends_on=("t1a", "t1b")),
            "t2b": make_task("t2b", depends_on=("t1b", "t1c")),
            "t3": make_task("t3", depends_on=("t2a", "t2b")),
        }
        result = compute_tiers(tasks)
        assert result[0] == ["t0"]
        assert set(result[1]) == {"t1a", "t1b", "t1c"}
        assert set(result[2]) == {"t2a", "t2b"}
        assert result[3] == ["t3"]


class TestTransitiveDependents:
    """Tests for transitive_dependents function."""

    def test_empty_failed_set(self):
        """Empty failed set returns empty dependents."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b", depends_on=("a",)),
        }
        assert transitive_dependents(tasks, set()) == set()

    def test_no_dependents(self):
        """Leaf node failure has no dependents."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b", depends_on=("a",)),
        }
        assert transitive_dependents(tasks, {"b"}) == set()

    def test_direct_dependent(self):
        """Direct dependent is included."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b", depends_on=("a",)),
        }
        assert transitive_dependents(tasks, {"a"}) == {"b"}

    def test_transitive_dependents(self):
        """Chain of dependents all included."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b", depends_on=("a",)),
            "c": make_task("c", depends_on=("b",)),
            "d": make_task("d", depends_on=("c",)),
        }
        assert transitive_dependents(tasks, {"a"}) == {"b", "c", "d"}

    def test_multiple_failed_slugs(self):
        """Union of dependents from multiple failed slugs."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b"),
            "c": make_task("c", depends_on=("a",)),
            "d": make_task("d", depends_on=("b",)),
            "e": make_task("e", depends_on=("c", "d")),
        }
        assert transitive_dependents(tasks, {"a", "b"}) == {"c", "d", "e"}

    def test_diamond_failure(self):
        """Diamond: failure propagates to bottom."""
        tasks = {
            "top": make_task("top"),
            "left": make_task("left", depends_on=("top",)),
            "right": make_task("right", depends_on=("top",)),
            "bottom": make_task("bottom", depends_on=("left", "right")),
        }
        # If top fails, everything depends on it
        assert transitive_dependents(tasks, {"top"}) == {"left", "right", "bottom"}
        # If left fails, only bottom depends on it
        assert transitive_dependents(tasks, {"left"}) == {"bottom"}

    def test_disconnected_component_not_affected(self):
        """Failed task doesn't affect disconnected component."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b", depends_on=("a",)),
            "c": make_task("c"),
            "d": make_task("d"),
        }
        assert transitive_dependents(tasks, {"a"}) == {"b"}
        assert "c" not in transitive_dependents(tasks, {"a"})
        assert "d" not in transitive_dependents(tasks, {"a"})


class TestRenderDryRun:
    """Tests for render_dry_run function."""

    def test_empty_tiers(self):
        """Empty tiers renders header with zero counts."""
        result = render_dry_run([], {})
        assert "DAG (0 tasks, 0 tiers):" in result

    def test_single_tier_single_task(self):
        """Single tier with one task renders correctly."""
        tasks = {"a": make_task("a", summary="task a", agent="qwen-35b")}
        result = render_dry_run([["a"]], tasks)
        assert "DAG (1 tasks, 1 tiers):" in result
        assert "Tier 0: a" in result
        assert "task a [qwen-35b]" in result

    def test_multiple_tiers(self):
        """Multiple tiers render with correct numbering."""
        tasks = {
            "a": make_task("a", summary="task a"),
            "b": make_task("b", summary="task b"),
        }
        result = render_dry_run([["a"], ["b"]], tasks)
        assert "Tier 0: a" in result
        assert "Tier 1: b" in result

    def test_multiple_tasks_per_tier(self):
        """Multiple tasks in same tier listed together."""
        tasks = {
            "a": make_task("a", summary="task a"),
            "b": make_task("b", summary="task b"),
        }
        result = render_dry_run([["a", "b"]], tasks)
        assert "Tier 0:" in result
        assert "task a" in result
        assert "task b" in result

    def test_total_count_correct(self):
        """Total task count is sum of all tiers."""
        tasks = {
            "a": make_task("a"),
            "b": make_task("b"),
            "c": make_task("c"),
        }
        result = render_dry_run([["a", "b"], ["c"]], tasks)
        assert "DAG (3 tasks," in result
