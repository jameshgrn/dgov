"""Tests for DAG-based batch execution."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dgov.batch import (
    _compute_tiers,
    _parse_spec,
    _render_dry_run,
    _transitive_dependents,
    _validate_dag,
    run_batch,
)

# ---------------------------------------------------------------------------
# Spec parsing
# ---------------------------------------------------------------------------


def test_parse_toml_spec(tmp_path: Path):
    spec = tmp_path / "batch.toml"
    spec.write_text(
        textwrap.dedent("""\
        project_root = "/repo"

        [tasks.lint]
        prompt = "Run lint"
        agent = "pi"
        touches = ["src/"]

        [tasks.test]
        prompt = "Run tests"
        depends_on = ["lint"]
        """)
    )

    project_root, tasks = _parse_spec(str(spec))

    assert project_root == "/repo"
    assert "lint" in tasks
    assert "test" in tasks
    assert tasks["lint"]["agent"] == "pi"
    assert tasks["lint"]["touches"] == ["src/"]
    assert tasks["test"]["depends_on"] == ["lint"]
    assert tasks["test"]["timeout"] == 600  # default


def test_parse_json_spec(tmp_path: Path):
    spec = tmp_path / "batch.json"
    spec.write_text(
        json.dumps(
            {
                "project_root": "/repo",
                "tasks": [
                    {"id": "a", "prompt": "do a", "agent": "claude"},
                    {"id": "b", "prompt": "do b", "depends_on": ["a"]},
                ],
            }
        )
    )

    project_root, tasks = _parse_spec(str(spec))

    assert project_root == "/repo"
    assert tasks["a"]["agent"] == "claude"
    assert tasks["b"]["depends_on"] == ["a"]


def test_parse_toml_missing_prompt(tmp_path: Path):
    spec = tmp_path / "bad.toml"
    spec.write_text(
        textwrap.dedent("""\
        [tasks.oops]
        agent = "pi"
        """)
    )
    with pytest.raises(ValueError, match="missing required field 'prompt'"):
        _parse_spec(str(spec))


# ---------------------------------------------------------------------------
# DAG validation
# ---------------------------------------------------------------------------


def test_missing_dep():
    tasks = {
        "a": {"id": "a", "prompt": "x", "depends_on": ["nonexistent"], "touches": []},
    }
    with pytest.raises(ValueError, match="does not exist"):
        _validate_dag(tasks)


def test_cycle_detection():
    tasks = {
        "a": {"id": "a", "prompt": "x", "depends_on": ["b"], "touches": []},
        "b": {"id": "b", "prompt": "y", "depends_on": ["a"], "touches": []},
    }
    with pytest.raises(ValueError, match="cycle"):
        _validate_dag(tasks)


def test_self_cycle():
    tasks = {
        "a": {"id": "a", "prompt": "x", "depends_on": ["a"], "touches": []},
    }
    with pytest.raises(ValueError, match="cycle"):
        _validate_dag(tasks)


# ---------------------------------------------------------------------------
# Tier computation
# ---------------------------------------------------------------------------


def test_depends_on_tiers():
    """Linear chain A -> B -> C produces 3 tiers."""
    tasks = {
        "a": {"id": "a", "prompt": "x", "depends_on": [], "touches": []},
        "b": {"id": "b", "prompt": "y", "depends_on": ["a"], "touches": []},
        "c": {"id": "c", "prompt": "z", "depends_on": ["b"], "touches": []},
    }
    tiers = _compute_tiers(tasks)
    assert len(tiers) == 3
    assert [t["id"] for t in tiers[0]] == ["a"]
    assert [t["id"] for t in tiers[1]] == ["b"]
    assert [t["id"] for t in tiers[2]] == ["c"]


def test_parallel_deps():
    """Diamond: A, then B+C parallel, then D."""
    tasks = {
        "a": {"id": "a", "prompt": "x", "depends_on": [], "touches": []},
        "b": {"id": "b", "prompt": "y", "depends_on": ["a"], "touches": []},
        "c": {"id": "c", "prompt": "z", "depends_on": ["a"], "touches": []},
        "d": {"id": "d", "prompt": "w", "depends_on": ["b", "c"], "touches": []},
    }
    tiers = _compute_tiers(tasks)
    assert len(tiers) == 3
    tier0_ids = {t["id"] for t in tiers[0]}
    tier1_ids = {t["id"] for t in tiers[1]}
    tier2_ids = {t["id"] for t in tiers[2]}
    assert tier0_ids == {"a"}
    assert tier1_ids == {"b", "c"}
    assert tier2_ids == {"d"}


def test_touch_overlap_serializes():
    """Tasks touching same files go in different tiers even without explicit deps."""
    tasks = {
        "a": {"id": "a", "prompt": "x", "depends_on": [], "touches": ["src/"]},
        "b": {"id": "b", "prompt": "y", "depends_on": [], "touches": ["src/foo.py"]},
    }
    tiers = _compute_tiers(tasks)
    assert len(tiers) == 2


def test_no_touch_overlap_parallelizes():
    """Tasks touching different files go in the same tier."""
    tasks = {
        "a": {"id": "a", "prompt": "x", "depends_on": [], "touches": ["src/"]},
        "b": {"id": "b", "prompt": "y", "depends_on": [], "touches": ["tests/"]},
    }
    tiers = _compute_tiers(tasks)
    assert len(tiers) == 1
    assert len(tiers[0]) == 2


def test_no_touches_parallelizes():
    """Tasks with no touches run in the same tier."""
    tasks = {
        "a": {"id": "a", "prompt": "x", "depends_on": [], "touches": []},
        "b": {"id": "b", "prompt": "y", "depends_on": [], "touches": []},
    }
    tiers = _compute_tiers(tasks)
    assert len(tiers) == 1


# ---------------------------------------------------------------------------
# Transitive dependents
# ---------------------------------------------------------------------------


def test_transitive_dependents():
    tasks = {
        "a": {"id": "a", "prompt": "x", "depends_on": [], "touches": []},
        "b": {"id": "b", "prompt": "y", "depends_on": ["a"], "touches": []},
        "c": {"id": "c", "prompt": "z", "depends_on": ["b"], "touches": []},
        "d": {"id": "d", "prompt": "w", "depends_on": [], "touches": []},
    }
    deps = _transitive_dependents(tasks, {"a"})
    assert deps == {"b", "c"}
    assert "d" not in deps


# ---------------------------------------------------------------------------
# Dry-run ASCII
# ---------------------------------------------------------------------------


def test_dry_run_ascii():
    tasks = {
        "lint": {"id": "lint", "prompt": "x", "depends_on": [], "touches": []},
        "test-core": {
            "id": "test-core",
            "prompt": "y",
            "depends_on": ["lint"],
            "touches": [],
        },
        "test-cli": {
            "id": "test-cli",
            "prompt": "z",
            "depends_on": ["lint"],
            "touches": [],
        },
        "docs": {
            "id": "docs",
            "prompt": "w",
            "depends_on": ["test-cli", "test-core"],
            "touches": [],
        },
    }
    tiers = _compute_tiers(tasks)
    output = _render_dry_run(tiers, tasks)

    assert "4 tasks" in output
    assert "3 tiers" in output
    assert "Tier 0" in output
    assert "Tier 1" in output
    assert "Tier 2" in output
    assert "lint" in output
    assert "docs" in output


def test_dry_run_via_run_batch(tmp_path: Path):
    spec = tmp_path / "batch.toml"
    spec.write_text(
        textwrap.dedent("""\
        project_root = "."

        [tasks.a]
        prompt = "do a"

        [tasks.b]
        prompt = "do b"
        depends_on = ["a"]
        """)
    )

    result = run_batch(str(spec), dry_run=True)

    assert result["dry_run"] is True
    assert result["total_tasks"] == 2
    assert "ascii_dag" in result
    assert "Tier 0" in result["ascii_dag"]


# ---------------------------------------------------------------------------
# Failure skipping
# ---------------------------------------------------------------------------


@patch("dgov.waiter.time")
@patch("dgov.merger.merge_worker_pane")
@patch("dgov.lifecycle.create_worker_pane")
@patch("dgov.waiter._is_done", return_value=True)
@patch("dgov.persistence.get_pane", return_value={})
def test_failure_skips_dependents(
    mock_get_pane,
    mock_is_done,
    mock_create,
    mock_merge,
    mock_time,
    tmp_path: Path,
):
    """When a task fails to create, its transitive dependents are skipped."""
    spec = tmp_path / "batch.toml"
    spec.write_text(
        textwrap.dedent("""\
        project_root = "."

        [tasks.a]
        prompt = "do a"

        [tasks.b]
        prompt = "do b"
        depends_on = ["a"]

        [tasks.c]
        prompt = "do c"
        depends_on = ["b"]

        [tasks.d]
        prompt = "do d"
        """)
    )

    # Task "a" fails to create, "d" succeeds
    def create_side_effect(project_root, prompt, agent, permission_mode, slug, session_root):
        if slug == "a":
            raise RuntimeError("boom")
        pane = MagicMock()
        pane.slug = slug
        return pane

    mock_create.side_effect = create_side_effect
    mock_merge.return_value = {"merged": True}
    mock_time.monotonic.return_value = 0

    result = run_batch(str(spec), session_root=str(tmp_path))

    assert "a" in result["failed"]
    skipped = set(result["skipped"])
    assert "b" in skipped
    assert "c" in skipped
    assert "d" not in skipped
    assert "d" in result["merged"]


# ---------------------------------------------------------------------------
# Dry-run tier output tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDryRunOutput:
    def test_dry_run_shows_tiers(self, tmp_path):
        spec = tmp_path / "spec.toml"
        spec.write_text("""
project_root = "."
[tasks.a]
prompt = "do a"
[tasks.b]
prompt = "do b"
depends_on = ["a"]
""")
        from dgov.batch import run_batch

        result = run_batch(str(spec), session_root=str(tmp_path), dry_run=True)
        assert result["dry_run"] is True
        assert result["total_tasks"] == 2
        assert len(result["tiers"]) == 2
        assert result["tiers"][0] == ["a"]
        assert result["tiers"][1] == ["b"]
        assert "DAG" in result["ascii_dag"]
