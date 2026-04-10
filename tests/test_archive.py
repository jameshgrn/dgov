"""Tests for plan archiving."""

from __future__ import annotations

from pathlib import Path

import pytest

from dgov.archive import archive_plan

pytestmark = pytest.mark.unit


def _make_plan_dir(base: Path, name: str) -> Path:
    plan_dir = base / name
    plan_dir.mkdir(parents=True)
    (plan_dir / "_compiled.toml").write_text('[plan]\nname = "test"\n')
    return plan_dir


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
