"""Tests for dag_graph and dag_parser internals."""

from __future__ import annotations

import pytest

from dgov.dag_graph import compute_tiers, topological_order, transitive_dependents, validate_dag
from dgov.dag_parser import DagFileSpec, DagTaskSpec


def _task(slug: str, depends_on: tuple[str, ...] = (), **kwargs) -> DagTaskSpec:
    return DagTaskSpec(
        slug=slug,
        summary=f"Task {slug}",
        prompt=f"Do {slug}",
        commit_message=f"Commit {slug}",
        agent="pi",
        escalation=(),
        depends_on=depends_on,
        files=kwargs.get("files", DagFileSpec()),
        permission_mode="bypassPermissions",
        timeout_s=600,
    )


# -- validate_dag --


@pytest.mark.unit
def test_validate_dag_passes_for_valid_dag() -> None:
    tasks = {"a": _task("a"), "b": _task("b", ("a",)), "c": _task("c", ("b",))}
    validate_dag(tasks)  # should not raise


@pytest.mark.unit
def test_validate_dag_raises_on_missing_dependency() -> None:
    tasks = {"a": _task("a", ("nonexistent",))}
    with pytest.raises(ValueError, match="does not exist"):
        validate_dag(tasks)


@pytest.mark.unit
def test_validate_dag_raises_on_cycle() -> None:
    tasks = {"a": _task("a", ("b",)), "b": _task("b", ("a",))}
    with pytest.raises(ValueError, match="cycle"):
        validate_dag(tasks)


# -- topological_order --


@pytest.mark.unit
def test_topological_order_linear_chain() -> None:
    tasks = {"a": _task("a"), "b": _task("b", ("a",)), "c": _task("c", ("b",))}
    order = topological_order(tasks)
    assert order.index("a") < order.index("b") < order.index("c")


@pytest.mark.unit
def test_topological_order_independent_tasks() -> None:
    tasks = {"x": _task("x"), "y": _task("y"), "z": _task("z")}
    order = topological_order(tasks)
    assert set(order) == {"x", "y", "z"}


@pytest.mark.unit
def test_topological_order_diamond() -> None:
    tasks = {
        "a": _task("a"),
        "b": _task("b", ("a",)),
        "c": _task("c", ("a",)),
        "d": _task("d", ("b", "c")),
    }
    order = topological_order(tasks)
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("c")
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


# -- transitive_dependents --


@pytest.mark.unit
def test_transitive_dependents_returns_downstream() -> None:
    tasks = {"a": _task("a"), "b": _task("b", ("a",)), "c": _task("c", ("b",))}
    deps = transitive_dependents(tasks, "a")
    assert deps == {"b", "c"}


@pytest.mark.unit
def test_transitive_dependents_leaf_has_none() -> None:
    tasks = {"a": _task("a"), "b": _task("b", ("a",))}
    deps = transitive_dependents(tasks, "b")
    assert deps == set()


# -- compute_tiers --


@pytest.mark.unit
def test_compute_tiers_parallel_tasks() -> None:
    tasks = {"a": _task("a"), "b": _task("b"), "c": _task("c")}
    tiers = compute_tiers(tasks)
    assert len(tiers) == 1
    assert set(tiers[0]) == {"a", "b", "c"}


@pytest.mark.unit
def test_compute_tiers_serial_chain() -> None:
    tasks = {"a": _task("a"), "b": _task("b", ("a",)), "c": _task("c", ("b",))}
    tiers = compute_tiers(tasks)
    assert len(tiers) == 3


@pytest.mark.unit
def test_compute_tiers_file_overlap_serializes() -> None:
    f = DagFileSpec(edit=("shared.py",))
    tasks = {"a": _task("a", files=f), "b": _task("b", files=f)}
    tiers = compute_tiers(tasks)
    assert len(tiers) == 2  # can't be parallel due to file overlap


# -- DagFileSpec --


@pytest.mark.unit
def test_dag_file_spec_defaults() -> None:
    fs = DagFileSpec()
    assert fs.create == ()
    assert fs.edit == ()
    assert fs.delete == ()


# -- DagTaskSpec --


@pytest.mark.unit
def test_dag_task_spec_attributes() -> None:
    t = _task("test", ("dep1",))
    assert t.slug == "test"
    assert t.depends_on == ("dep1",)
    assert t.agent == "pi"
    assert t.timeout_s == 600
