"""Tests for compile-time import graph conflict detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dgov.cli import cli
from dgov.dag_parser import DagDefinition, DagFileSpec, DagTaskSpec
from dgov.import_graph import build_import_graph, detect_cross_task_import_conflicts

pytestmark = pytest.mark.unit

_VALID_PROMPT = "Orient:\nRead context.\n\nEdit:\n1. Change files.\n\nVerify:\n- Check."


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _task(slug: str, files: DagFileSpec, depends_on: tuple[str, ...] = ()) -> DagTaskSpec:
    return DagTaskSpec(
        slug=slug,
        summary=slug,
        prompt=_VALID_PROMPT,
        commit_message=slug,
        files=files,
        depends_on=depends_on,
    )


def _dag(tasks: dict[str, DagTaskSpec]) -> DagDefinition:
    return DagDefinition(
        name="test",
        dag_file="test.toml",
        project_root=".",
        session_root=".",
        tasks=tasks,
    )


def test_build_import_graph_resolves_absolute_src_imports(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "dgov" / "__init__.py", "")
    _write(tmp_path / "src" / "dgov" / "models.py", "class UserModel: ...\n")
    _write(
        tmp_path / "src" / "dgov" / "service.py",
        "from dgov.models import UserModel\n",
    )

    graph = build_import_graph(str(tmp_path), ["src/dgov/service.py"])

    assert graph["src/dgov/service.py"] == {"src/dgov/models.py"}
    assert "src/dgov/models.py" in graph


def test_build_import_graph_resolves_relative_imports(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "models.py", "class UserModel: ...\n")
    _write(tmp_path / "src" / "pkg" / "helpers.py", "VALUE = 1\n")
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "from .models import UserModel\nfrom . import helpers\n",
    )

    graph = build_import_graph(str(tmp_path), ["src/pkg/service.py"])

    assert graph["src/pkg/service.py"] == {"src/pkg/helpers.py", "src/pkg/models.py"}


def test_build_import_graph_skips_missing_and_third_party_imports(tmp_path: Path) -> None:
    _write(
        tmp_path / "src" / "pkg" / "service.py",
        "import requests\nfrom missing.module import Thing\n",
    )

    graph = build_import_graph(str(tmp_path), ["src/pkg/service.py"])

    assert graph["src/pkg/service.py"] == set()


def test_detect_import_conflicts_ignores_independent_tasks_without_overlap() -> None:
    dag = _dag({
        "a": _task("a", DagFileSpec(edit=("src/other.py",))),
        "b": _task("b", DagFileSpec(edit=("src/service.py",))),
    })
    graph = {"src/service.py": {"src/models.py"}}

    assert detect_cross_task_import_conflicts(dag, graph) == []


def test_detect_import_conflicts_between_independent_tasks() -> None:
    dag = _dag({
        "a": _task("a", DagFileSpec(edit=("src/models.py",))),
        "b": _task("b", DagFileSpec(edit=("src/service.py",))),
    })
    graph = {"src/service.py": {"src/models.py"}}

    conflicts = detect_cross_task_import_conflicts(dag, graph)

    assert len(conflicts) == 1
    assert conflicts[0].task_a == "a"
    assert conflicts[0].task_b == "b"
    assert conflicts[0].written_file == "src/models.py"
    assert conflicts[0].importing_file == "src/service.py"


def test_detect_import_conflicts_skips_tasks_with_dependency_chain() -> None:
    dag = _dag({
        "a": _task("a", DagFileSpec(edit=("src/models.py",))),
        "b": _task("b", DagFileSpec(edit=("src/service.py",)), depends_on=("a",)),
    })
    graph = {"src/service.py": {"src/models.py"}}

    assert detect_cross_task_import_conflicts(dag, graph) == []


def test_detect_import_conflicts_in_diamond_dependency_pattern() -> None:
    dag = _dag({
        "root": _task("root", DagFileSpec(edit=("src/root.py",))),
        "left": _task(
            "left",
            DagFileSpec(edit=("src/models.py",)),
            depends_on=("root",),
        ),
        "right": _task(
            "right",
            DagFileSpec(edit=("src/service.py",)),
            depends_on=("root",),
        ),
        "join": _task(
            "join",
            DagFileSpec(edit=("src/join.py",)),
            depends_on=("left", "right"),
        ),
    })
    graph = {"src/service.py": {"src/models.py"}, "src/join.py": {"src/service.py"}}

    conflicts = detect_cross_task_import_conflicts(dag, graph)

    assert [(c.task_a, c.task_b) for c in conflicts] == [("left", "right")]


def test_compile_plan_emits_import_conflict_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "src" / "models.py", "class UserModel: ...\n")
    _write(tmp_path / "src" / "service.py", "from models import UserModel\n")

    plan_root = tmp_path / ".dgov" / "plans" / "import-conflict"
    tasks_dir = plan_root / "tasks"
    tasks_dir.mkdir(parents=True)
    _write(
        plan_root / "_root.toml",
        '[plan]\nname = "import-conflict"\nsummary = "test"\nsections = ["tasks"]\n',
    )
    _write(
        tasks_dir / "main.toml",
        f"""
[tasks.models]
summary = "Update models"
prompt = {json.dumps(_VALID_PROMPT)}
commit_message = "Update models"
files.edit = ["src/models.py"]

[tasks.service]
summary = "Update service"
prompt = {json.dumps(_VALID_PROMPT)}
commit_message = "Update service"
files.edit = ["src/service.py"]
""",
    )

    result = CliRunner().invoke(
        cli,
        ["compile", str(plan_root), "--dry-run"],
        env={"DGOV_JSON": "1"},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    warnings = [warning["message"] for warning in payload["warnings"]]
    assert any(
        "tasks 'tasks/main.models' and 'tasks/main.service' may conflict" in warning
        and "'tasks/main.models' writes src/models.py" in warning
        and "src/service.py (written by 'tasks/main.service')" in warning
        for warning in warnings
    )
