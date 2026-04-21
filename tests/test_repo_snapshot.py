from __future__ import annotations

import subprocess
from pathlib import Path

from dgov.repo_snapshot import (
    build_repo_snapshot,
    format_structural_offender_report,
    likely_structural_offenders,
)


def _init_git_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)


def test_build_repo_snapshot_caches_commit_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "mod.py").write_text(
        "class A:\n"
        "    def method(self):\n"
        "        return 1\n"
        "\n"
        "def helper(x):\n"
        "    return x + 1\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    snapshot = build_repo_snapshot(repo)
    commit_sha = (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )
    cache_path = repo / ".dgov" / "runtime" / "repo_snapshot" / f"{commit_sha}.json"

    assert cache_path.exists()
    assert any(fn.qualname == "A.method" for fn in snapshot.functions)
    assert any(fn.qualname == "helper" for fn in snapshot.functions)


def test_likely_structural_offenders_reports_long_and_complex_functions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "src").mkdir()
    body = "\n".join(
        [
            "def hotspot(value):",
            "    total = 0",
            "    for i in range(value):",
            "        if i % 2 == 0:",
            "            total += i",
            "        else:",
            "            total -= i",
        ]
        + [f"    total += {i}" for i in range(50)]
        + ["    return total"]
    )
    (repo / "src" / "hotspot.py").write_text(body + "\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    report = likely_structural_offenders(repo)
    text = format_structural_offender_report(report)

    assert report["long_functions"]
    assert "hotspot.py" in text
    assert "hotspot" in text
