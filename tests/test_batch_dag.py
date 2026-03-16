"""Verify batch.py still works after refactoring to use dag.py helpers."""

from __future__ import annotations

import pytest

from dgov.batch import _compute_tiers, _transitive_dependents, _validate_dag

pytestmark = pytest.mark.unit


def _btask(task_id, depends_on=(), touches=()):
    return {
        "id": task_id,
        "prompt": "p",
        "agent": "hunter",
        "depends_on": list(depends_on),
        "touches": list(touches),
    }


class TestBatchDagCompat:
    def test_validate_valid(self):
        tasks = {"T0": _btask("T0"), "T1": _btask("T1", depends_on=["T0"])}
        _validate_dag(tasks)

    def test_validate_missing_dep(self):
        tasks = {"T0": _btask("T0", depends_on=["T_MISSING"])}
        with pytest.raises(ValueError):
            _validate_dag(tasks)

    def test_validate_cycle(self):
        tasks = {"A": _btask("A", depends_on=["B"]), "B": _btask("B", depends_on=["A"])}
        with pytest.raises(ValueError):
            _validate_dag(tasks)

    def test_compute_tiers_parallel(self):
        tasks = {"T0": _btask("T0", touches=["a.py"]), "T1": _btask("T1", touches=["b.py"])}
        tiers = _compute_tiers(tasks)
        assert len(tiers) == 1

    def test_compute_tiers_overlap(self):
        tasks = {"T0": _btask("T0", touches=["f.py"]), "T1": _btask("T1", touches=["f.py"])}
        tiers = _compute_tiers(tasks)
        assert len(tiers) == 2

    def test_transitive_dependents(self):
        tasks = {
            "T0": _btask("T0"),
            "T1": _btask("T1", depends_on=["T0"]),
            "T2": _btask("T2", depends_on=["T1"]),
        }
        deps = _transitive_dependents(tasks, {"T0"})
        assert deps == {"T1", "T2"}
