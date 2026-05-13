"""Tests for plan archiving."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from dgov.archive import ArchiveError, archive_plan

pytestmark = pytest.mark.unit


def _make_plan_dir(base: Path, name: str) -> Path:
    plan_dir = base / name
    plan_dir.mkdir(parents=True)
    (plan_dir / "_compiled.toml").write_text('[plan]\nname = "test"\n')
    return plan_dir


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=path, check=True)


# -- archive_plan --


def test_archive_plan_moves_directory(tmp_path: Path) -> None:
    plan_dir = _make_plan_dir(tmp_path, "my-plan")
    dest = archive_plan(plan_dir)
    assert dest == tmp_path / "archive" / "my-plan"
    assert dest.exists()
    assert not plan_dir.exists()


def test_archive_plan_creates_archive_dir(tmp_path: Path) -> None:
    plan_dir = _make_plan_dir(tmp_path, "my-plan")
    assert not (tmp_path / "archive").exists()
    archive_plan(plan_dir)
    assert (tmp_path / "archive").is_dir()


def test_archive_plan_returns_dest_path(tmp_path: Path) -> None:
    plan_dir = _make_plan_dir(tmp_path, "feature-x")
    dest = archive_plan(plan_dir)
    assert dest == tmp_path / "archive" / "feature-x"


def test_archive_plan_preserves_contents(tmp_path: Path) -> None:
    plan_dir = _make_plan_dir(tmp_path, "my-plan")
    (plan_dir / "section").mkdir()
    (plan_dir / "section" / "task.toml").write_text("content")
    dest = archive_plan(plan_dir)
    assert (dest / "_compiled.toml").exists()
    assert (dest / "section" / "task.toml").read_text() == "content"


def test_archive_plan_multiple_plans(tmp_path: Path) -> None:
    plan_a = _make_plan_dir(tmp_path, "plan-a")
    plan_b = _make_plan_dir(tmp_path, "plan-b")
    archive_plan(plan_a)
    archive_plan(plan_b)
    assert (tmp_path / "archive" / "plan-a").exists()
    assert (tmp_path / "archive" / "plan-b").exists()


def test_archive_plan_refuses_ignored_durable_plan_archive(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    dgov_dir = tmp_path / ".dgov"
    plans_dir = dgov_dir / "plans"
    plans_dir.mkdir(parents=True)
    (dgov_dir / ".gitignore").write_text("plans/archive/\n")
    plan_dir = _make_plan_dir(plans_dir, "my-plan")

    with pytest.raises(ArchiveError, match=r"ignored \.dgov/plans/archive"):
        archive_plan(plan_dir)

    assert plan_dir.exists()
    assert not (plans_dir / "archive" / "my-plan").exists()


def test_archive_plan_allows_ignored_runtime_fix_archive(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    dgov_dir = tmp_path / ".dgov"
    runtime_fix_dir = dgov_dir / "runtime" / "fix-plans"
    runtime_fix_dir.mkdir(parents=True)
    (dgov_dir / ".gitignore").write_text("runtime/\n")
    plan_dir = _make_plan_dir(runtime_fix_dir, "fix-my-plan")

    dest = archive_plan(plan_dir)

    assert dest == runtime_fix_dir / "archive" / "fix-my-plan"
    assert dest.exists()
