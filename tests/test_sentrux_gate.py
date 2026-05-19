"""Diff-aware Sentrux gate tests."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from dgov.config import ProjectConfig
from dgov.sentrux_gate import (
    _paths_from_mapping,
    _paths_from_object,
    _paths_from_sequence,
    _paths_from_string,
    changed_files_since,
    sentrux_baseline_age,
)
from dgov.settlement import validate_sandbox


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
        check=True,
    )


def _init_repo(path: Path) -> None:
    _git(path, "init", "-b", "main")
    (path / "README.md").write_text("# test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")


def _commit_file(path: Path, rel_path: str, content: str, message: str) -> str:
    full_path = path / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    _git(path, "add", rel_path)
    _git(path, "commit", "-m", message)
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _save_sentrux_baseline(path: Path) -> str:
    sx_dir = path / ".sentrux"
    sx_dir.mkdir()
    (sx_dir / "baseline.json").write_text(
        json.dumps({
            "timestamp": time.time(),
            "quality_signal": 0.9,
            "total_import_edges": 1,
            "cycle_count": 0,
            "god_file_count": 0,
            "complex_fn_count": 0,
        })
    )
    _git(path, "add", ".sentrux/baseline.json")
    _git(path, "commit", "-m", "save sentrux baseline")
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def _sentrux_only_config(*, sentrux_mode: str = "diff") -> ProjectConfig:
    return ProjectConfig(
        lint_cmd="true {file}",
        format_check_cmd="true {file}",
        test_cmd="",
        type_check_cmd="",
        sentrux_mode=sentrux_mode,
    )


def _mock_degraded_sentrux(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    real_run = subprocess.run

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["sentrux", "gate"]:
            cwd = Path(str(kwargs.get("cwd", ".")))
            sx_dir = cwd / ".sentrux"
            sx_dir.mkdir(exist_ok=True)
            (sx_dir / "current.json").write_text(
                json.dumps({"cycle_count": 0, "cycles": [], "god_file_count": 0, "god_files": []})
            )
            return subprocess.CompletedProcess(args, 1, stdout=stdout, stderr="")
        return real_run(args, **kwargs)

    monkeypatch.setattr("subprocess.run", _run)
    monkeypatch.setattr("dgov.settlement.shutil.which", lambda name: "/usr/bin/sentrux")


@pytest.mark.unit
def test_sentrux_quality_drop_passes_for_trivial_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _save_sentrux_baseline(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _commit_file(tmp_path, "trivial.py", "VALUE = 1\n", "add trivial file")
    _mock_degraded_sentrux(
        monkeypatch,
        "Quality:      6852 -> 6648\n✗ DEGRADED\n  ✗ Quality score dropped: 6852 → 6648\n",
    )

    result = validate_sandbox(tmp_path, base, str(tmp_path), config=_sentrux_only_config())

    assert result.passed is True


@pytest.mark.unit
def test_sentrux_quality_drop_fails_for_new_complex_function(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _save_sentrux_baseline(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    branches = "\n".join(f"    if value == {idx}:\n        total += {idx}" for idx in range(12))
    _commit_file(
        tmp_path,
        "complex.py",
        f"def complex_choice(value: int) -> int:\n    total = 0\n{branches}\n    return total\n",
        "add complex function",
    )
    _mock_degraded_sentrux(
        monkeypatch,
        "Quality:      6852 -> 6648\n✗ DEGRADED\n  ✗ Quality score dropped: 6852 → 6648\n",
    )

    result = validate_sandbox(tmp_path, base, str(tmp_path), config=_sentrux_only_config())

    assert result.passed is False
    assert result.error is not None
    assert "NEW Sentrux offenders" in result.error
    assert "complex.py" in result.error
    assert "complex_choice" in result.error


@pytest.mark.unit
def test_stale_sentrux_baseline_warns_without_failing_clean_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _init_repo(tmp_path)
    _save_sentrux_baseline(tmp_path)
    for idx in range(11):
        _commit_file(tmp_path, f"notes/{idx}.txt", f"{idx}\n", f"advance {idx}")
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _commit_file(tmp_path, "trivial.py", "VALUE = 1\n", "add trivial file")
    _mock_degraded_sentrux(
        monkeypatch,
        "Quality:      6852 -> 6648\n✗ DEGRADED\n  ✗ Quality score dropped: 6852 → 6648\n",
    )

    result = validate_sandbox(tmp_path, base, str(tmp_path), config=_sentrux_only_config())

    captured = capsys.readouterr()
    assert result.passed is True
    assert "WARNING: Sentrux baseline is stale" in captured.err
    assert "clean complete full-plan `dgov run` refreshes" in captured.err
    assert "dgov sentrux gate-save" in captured.err


@pytest.mark.unit
def test_dgov_sentrux_metadata_records_current_accepted_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _save_sentrux_baseline(tmp_path)
    for idx in range(11):
        _commit_file(tmp_path, f"notes/{idx}.txt", f"{idx}\n", f"advance {idx}")

    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    meta_path = tmp_path / ".sentrux" / "dgov-baseline.json"
    meta_path.write_text(
        json.dumps({
            "accepted_head": head,
            "timestamp": time.time(),
        })
    )

    age = sentrux_baseline_age(tmp_path, tmp_path / ".sentrux" / "baseline.json")

    assert age.baseline_commit == head
    assert age.commits_behind == 0
    assert not age.stale(commit_threshold=10, day_threshold=14)


@pytest.mark.unit
def test_invalid_dgov_sentrux_metadata_falls_back_to_baseline_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    baseline_head = _save_sentrux_baseline(tmp_path)
    for idx in range(11):
        _commit_file(tmp_path, f"notes/{idx}.txt", f"{idx}\n", f"advance {idx}")

    meta_path = tmp_path / ".sentrux" / "dgov-baseline.json"
    meta_path.write_text(
        json.dumps({
            "accepted_head": "not-a-real-commit",
            "timestamp": time.time(),
        })
    )

    age = sentrux_baseline_age(tmp_path, tmp_path / ".sentrux" / "baseline.json")

    assert age.baseline_commit == baseline_head
    assert age.commits_behind == 11
    assert age.stale(commit_threshold=10, day_threshold=14)


@pytest.mark.unit
def test_strict_sentrux_mode_keeps_absolute_quality_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_repo(tmp_path)
    _save_sentrux_baseline(tmp_path)
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _commit_file(tmp_path, "trivial.py", "VALUE = 1\n", "add trivial file")
    _mock_degraded_sentrux(
        monkeypatch,
        "Quality:      6852 -> 6648\n✗ DEGRADED\n  ✗ Quality score dropped: 6852 → 6648\n",
    )

    result = validate_sandbox(
        tmp_path,
        base,
        str(tmp_path),
        config=_sentrux_only_config(sentrux_mode="strict"),
    )

    assert result.passed is False
    assert result.error is not None
    assert "Sentrux architectural degradation" in result.error


@pytest.mark.unit
def test_changed_files_since_decodes_unicode_source_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    name = "caf\u00e9.py"
    _commit_file(tmp_path, name, "x = 1\n", "add unicode path")
    base = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    (tmp_path / name).write_text("x = 2\n")
    _git(tmp_path, "add", name)
    _git(tmp_path, "commit", "-m", "change unicode path")

    assert changed_files_since(tmp_path, base, (".py",)) == [name]


@pytest.mark.unit
def test_paths_from_object_extracts_from_string() -> None:
    assert _paths_from_object("src/foo.py") == {"src/foo.py"}


@pytest.mark.unit
def test_paths_from_object_expands_module_string() -> None:
    result = _paths_from_object("dgov.sentrux_gate")
    assert "dgov/sentrux_gate.py" in result
    assert "src/dgov/sentrux_gate.py" in result


@pytest.mark.unit
def test_paths_from_object_extracts_from_mapping_path_keys() -> None:
    value = {"path": "a/b.py", "file": "c/d.py", "module": "e.f", "name": "g.h"}
    result = _paths_from_object(value)
    assert "a/b.py" in result
    assert "c/d.py" in result
    assert "e/f.py" in result
    assert "src/e/f.py" in result
    assert "g/h.py" in result
    assert "src/g/h.py" in result


@pytest.mark.unit
def test_paths_from_object_extracts_from_nested_mapping_values() -> None:
    value = {"details": {"path": "nested.py"}, "other": {"inner": ["x.py"]}}
    result = _paths_from_object(value)
    assert "nested.py" in result
    assert "x.py" in result


@pytest.mark.unit
def test_paths_from_object_extracts_from_sequence() -> None:
    value = ["a.py", "b.py"]
    assert _paths_from_object(value) == {"a.py", "b.py"}


@pytest.mark.unit
def test_paths_from_object_extracts_deeply_nested() -> None:
    value = [{"cycles": [{"path": "loop.py"}]}, ["flat.py"]]
    result = _paths_from_object(value)
    assert "loop.py" in result
    assert "flat.py" in result


@pytest.mark.unit
def test_paths_from_object_returns_empty_for_unknown_types() -> None:
    assert _paths_from_object(42) == set()
    assert _paths_from_object(None) == set()


@pytest.mark.unit
def test_paths_from_string_delegates_to_candidates() -> None:
    assert _paths_from_string("foo.py") == {"foo.py"}


@pytest.mark.unit
def test_paths_from_mapping_skips_non_path_scalar_values() -> None:
    value = {"count": 42, "ratio": 0.5, "ok": True}
    assert _paths_from_mapping(value) == set()


@pytest.mark.unit
def test_paths_from_sequence_ignores_non_path_items() -> None:
    value = [42, None, {"path": "found.py"}]
    result = _paths_from_sequence(value)
    assert "found.py" in result
