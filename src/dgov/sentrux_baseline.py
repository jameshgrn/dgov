"""Governor-owned sentrux baseline refresh helpers."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

SENTRUX_BASELINE_REL_PATH = ".sentrux/baseline.json"
DGOV_SENTRUX_BASELINE_META_REL_PATH = ".sentrux/dgov-baseline.json"


class SentruxRunner(Protocol):
    def __call__(
        self,
        args: list[str],
        cwd: str | None = None,
        timeout: float = 30.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]: ...


class SentruxBaselineRefreshError(RuntimeError):
    """Raised when dgov cannot refresh and commit accepted sentrux state."""


def canonical_project_root(project_root: str) -> Path:
    return Path(project_root).resolve()


def sentrux_baseline_path(project_root: str) -> Path:
    return canonical_project_root(project_root) / SENTRUX_BASELINE_REL_PATH


def sentrux_baseline_meta_path(project_root: str) -> Path:
    return canonical_project_root(project_root) / DGOV_SENTRUX_BASELINE_META_REL_PATH


def refresh_sentrux_baseline_after_clean_run(
    project_root: str,
    *,
    run_sentrux: SentruxRunner,
) -> bool:
    """Refresh sentrux baseline files for an already accepted full-plan run."""
    root = canonical_project_root(project_root)
    result = _run_sentrux_gate_save(root, run_sentrux)
    output = (result.stdout or "") + (result.stderr or "")
    _write_dgov_sentrux_baseline_metadata(
        root,
        accepted_head=_accepted_head(root),
        quality=_parse_sentrux_save_quality(output),
    )
    return _commit_sentrux_baseline_refresh(root)


def record_sentrux_baseline_metadata(project_root: str, output: str) -> Path | None:
    """Record accepted sentrux metadata when HEAD matches the scanned source tree."""
    root = canonical_project_root(project_root)
    try:
        accepted_head = _accepted_head(root)
    except SentruxBaselineRefreshError:
        return None
    if _has_non_baseline_worktree_changes(root):
        return None
    _write_dgov_sentrux_baseline_metadata(
        root,
        accepted_head=accepted_head,
        quality=_parse_sentrux_save_quality(output),
    )
    return sentrux_baseline_meta_path(str(root))


def _run_sentrux_gate_save(
    root: Path,
    run_sentrux: SentruxRunner,
) -> subprocess.CompletedProcess[str]:
    try:
        return run_sentrux(["gate", "--save", str(root)], timeout=30.0)
    except subprocess.CalledProcessError as exc:
        raise SentruxBaselineRefreshError(_called_process_message(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        message = f"timed out refreshing sentrux baseline after clean run: {exc}"
        raise SentruxBaselineRefreshError(message) from exc


def _called_process_message(exc: subprocess.CalledProcessError) -> str:
    details = (exc.stderr or exc.stdout or str(exc)).strip()
    message = "failed to refresh sentrux baseline after clean run"
    if details:
        return f"{message}: {details}"
    return message


def _accepted_head(root: Path) -> str:
    head = _git_stdout(root, ["rev-parse", "HEAD"])
    if head is None:
        raise SentruxBaselineRefreshError(
            "failed to refresh sentrux baseline: unable to resolve HEAD"
        )
    return head


def _write_dgov_sentrux_baseline_metadata(
    root: Path,
    *,
    accepted_head: str,
    quality: int | None,
) -> None:
    refreshed_at = datetime.now(UTC)
    payload = _baseline_metadata_payload(
        accepted_head=accepted_head,
        refreshed_at=refreshed_at,
        quality=quality,
    )
    meta_path = sentrux_baseline_meta_path(str(root))
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _baseline_metadata_payload(
    *,
    accepted_head: str,
    refreshed_at: datetime,
    quality: int | None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": 1,
        "accepted_head": accepted_head,
        "refreshed_at": refreshed_at.isoformat(),
        "timestamp": refreshed_at.timestamp(),
        "baseline": "baseline.json",
    }
    if quality is not None:
        payload["quality"] = quality
    return payload


def _commit_sentrux_baseline_refresh(root: Path) -> bool:
    paths = [SENTRUX_BASELINE_REL_PATH, DGOV_SENTRUX_BASELINE_META_REL_PATH]
    if not _git_has_path_changes(root, paths):
        return False
    _run_git_or_raise(root, ["add", "--", *paths], action="stage sentrux baseline")
    _run_git_or_raise(
        root,
        ["commit", "-m", "chore: refresh sentrux baseline", "--", *paths],
        action="commit sentrux baseline refresh",
    )
    return True


def _git_has_path_changes(root: Path, paths: list[str]) -> bool:
    status = _git_stdout(
        root,
        ["status", "--porcelain", "--untracked-files=all", "--", *paths],
    )
    return bool(status)


def _has_non_baseline_worktree_changes(root: Path) -> bool:
    status = _git_stdout(root, ["status", "--porcelain", "--untracked-files=all"])
    if status is None:
        return True
    baseline_paths = {SENTRUX_BASELINE_REL_PATH, DGOV_SENTRUX_BASELINE_META_REL_PATH}
    return any(_status_path(line) not in baseline_paths for line in status.splitlines() if line)


def _status_path(line: str) -> str:
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path


def _run_git_or_raise(
    root: Path,
    args: list[str],
    *,
    action: str,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=_git_env(root),
        check=False,
    )
    if result.returncode != 0:
        raise SentruxBaselineRefreshError(_git_error_message(result, action))
    return result


def _git_error_message(result: subprocess.CompletedProcess[str], action: str) -> str:
    details = (result.stderr or result.stdout or "").strip()
    message = f"failed to {action}"
    if details:
        return f"{message}: {details}"
    return message


def _git_stdout(root: Path, args: list[str]) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        env=_git_env(root),
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_env(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["PWD"] = str(root)
    return env


def _parse_sentrux_save_quality(output: str) -> int | None:
    for line in output.splitlines():
        if line.startswith("Quality:"):
            return _parse_quality_token(line.split(":", 1)[1].strip())
    return None


def _parse_quality_token(token: str) -> int | None:
    value = token.split("->")[-1].strip() if "->" in token else token
    try:
        return int(value)
    except ValueError:
        return _parse_float_quality(value)


def _parse_float_quality(value: str) -> int | None:
    try:
        parsed = float(value)
    except ValueError:
        return None
    if parsed <= 1.0:
        return int(parsed * 10000)
    return int(parsed)
