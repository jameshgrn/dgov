"""Tests for deploy log — append-only JSONL tracking of shipped units."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dgov.deploy_log import DeployRecord, append, is_deployed, is_plan_complete, read

pytestmark = pytest.mark.unit


def _log_path(project_root: str) -> Path:
    return Path(project_root) / ".dgov" / "plans" / "deployed.jsonl"


# -- append --


def test_append_creates_file(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "my-plan", "core/setup.init", "abc123")
    path = _log_path(root)
    assert path.exists()
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["plan"] == "my-plan"
    assert data["unit"] == "core/setup.init"
    assert data["sha"] == "abc123"
    assert "ts" in data


def test_append_is_additive(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "p", "a", "sha1")
    append(root, "p", "b", "sha2")
    lines = _log_path(root).read_text().strip().splitlines()
    assert len(lines) == 2


def test_append_custom_timestamp(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "p", "u", "sha", timestamp="2026-01-01T00:00:00Z")
    data = json.loads(_log_path(root).read_text().strip())
    assert data["ts"] == "2026-01-01T00:00:00Z"


# -- read --


def test_read_filters_by_plan(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "alpha", "u1", "s1")
    append(root, "beta", "u2", "s2")
    append(root, "alpha", "u3", "s3")
    records = read(root, "alpha")
    assert len(records) == 2
    assert all(r.plan == "alpha" for r in records)
    assert [r.unit for r in records] == ["u1", "u3"]


def test_read_returns_deploy_records(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "p", "unit/id", "deadbeef", timestamp="2026-04-06T12:00:00Z")
    records = read(root, "p")
    assert len(records) == 1
    r = records[0]
    assert isinstance(r, DeployRecord)
    assert r.plan == "p"
    assert r.unit == "unit/id"
    assert r.sha == "deadbeef"
    assert r.ts == "2026-04-06T12:00:00Z"


def test_read_missing_file(tmp_path: Path) -> None:
    assert read(str(tmp_path), "anything") == []


def test_read_empty_file(tmp_path: Path) -> None:
    root = str(tmp_path)
    path = _log_path(root)
    path.parent.mkdir(parents=True)
    path.write_text("")
    assert read(root, "p") == []


def test_read_skips_malformed_lines(tmp_path: Path) -> None:
    root = str(tmp_path)
    path = _log_path(root)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"plan":"p","unit":"good","sha":"a","ts":"t"}\n'
        "not json at all\n"
        '{"plan":"p","unit":"also-good","sha":"b","ts":"t"}\n'
    )
    records = read(root, "p")
    assert len(records) == 2
    assert records[0].unit == "good"
    assert records[1].unit == "also-good"


def test_read_skips_blank_lines(tmp_path: Path) -> None:
    root = str(tmp_path)
    path = _log_path(root)
    path.parent.mkdir(parents=True)
    path.write_text(
        '{"plan":"p","unit":"u","sha":"s","ts":"t"}\n'
        "\n"
        "\n"
        '{"plan":"p","unit":"v","sha":"s","ts":"t"}\n'
    )
    assert len(read(root, "p")) == 2


# -- is_deployed --


def test_is_deployed_true(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "plan", "core/setup.init", "sha")
    assert is_deployed(root, "plan", "core/setup.init") is True


def test_is_deployed_false(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "plan", "core/setup.init", "sha")
    assert is_deployed(root, "plan", "core/setup.config") is False


def test_is_deployed_wrong_plan(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "alpha", "u", "sha")
    assert is_deployed(root, "beta", "u") is False


def test_is_deployed_no_file(tmp_path: Path) -> None:
    assert is_deployed(str(tmp_path), "p", "u") is False


# -- is_plan_complete --


def test_is_plan_complete_all_deployed(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "plan", "core/a.init", "sha1")
    append(root, "plan", "core/b.init", "sha2")
    assert is_plan_complete(root, "plan", {"core/a.init", "core/b.init"}) is True


def test_is_plan_complete_partial(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "plan", "core/a.init", "sha1")
    assert is_plan_complete(root, "plan", {"core/a.init", "core/b.init"}) is False


def test_is_plan_complete_none_deployed(tmp_path: Path) -> None:
    assert is_plan_complete(str(tmp_path), "plan", {"core/a.init"}) is False


def test_is_plan_complete_extra_deployed_still_true(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "plan", "core/a.init", "sha1")
    append(root, "plan", "core/b.init", "sha2")
    append(root, "plan", "core/c.init", "sha3")
    assert is_plan_complete(root, "plan", {"core/a.init", "core/b.init"}) is True


def test_is_plan_complete_wrong_plan(tmp_path: Path) -> None:
    root = str(tmp_path)
    append(root, "other-plan", "core/a.init", "sha1")
    assert is_plan_complete(root, "plan", {"core/a.init"}) is False
