"""Diff-aware Sentrux gate policy."""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from dgov.repo_snapshot import likely_structural_offenders

_FULL_OFFENDER_LIMIT = 10_000
_SECONDS_PER_DAY = 86_400
_SENTRUX_WARN_ONLY = re.compile(
    r"(complex functions increased|coupling increased)",
    re.IGNORECASE,
)
_SENTRUX_HARD_FAIL = re.compile(
    r"(quality.*dropped|cycles increased|god files increased)",
    re.IGNORECASE,
)
_FUNCTION_SECTIONS = (
    ("complex_functions", "Complex functions", "cyclomatic", True),
    ("cog_complex_functions", "Cognitive hotspots", "cognitive", True),
    ("long_functions", "Long functions", "line_count", False),
)


@dataclass(frozen=True, slots=True)
class SentruxBaselineAge:
    baseline_commit: str | None
    commits_behind: int | None
    timestamp: float | None
    seconds_old: float | None

    def stale(self, *, commit_threshold: int, day_threshold: int) -> bool:
        if self.commits_behind is not None and self.commits_behind > commit_threshold:
            return True
        return self.seconds_old is not None and self.seconds_old > day_threshold * _SECONDS_PER_DAY

    def describe(self) -> str:
        parts: list[str] = []
        if self.commits_behind is not None:
            parts.append(f"{self.commits_behind} commits behind HEAD")
        if self.seconds_old is not None:
            parts.append(_format_seconds_old(self.seconds_old))
        if self.baseline_commit:
            parts.append(f"baseline commit {self.baseline_commit[:12]}")
        if not parts:
            return "unknown"
        return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class SentruxOffender:
    kind: str
    label: str
    path: str
    name: str
    lineno: int | None
    end_lineno: int | None
    metric: str
    value: int | None
    hard: bool

    def identity(self) -> tuple[str, str, str]:
        return self.kind, self.path, self.name

    def line(self) -> str:
        location = self.path
        if self.lineno is not None:
            location = f"{location}:{self.lineno}"
        metric = f" ({self.metric}={self.value})" if self.value is not None else ""
        name = f" {self.name}" if self.name else ""
        return f"  {location}{name}{metric}"


@dataclass(frozen=True, slots=True)
class SentruxGateAssessment:
    should_fail: bool
    warning: str | None
    error: str | None
    new_offenders: tuple[SentruxOffender, ...]
    preexisting_offenders: tuple[SentruxOffender, ...]
    current_report: dict[str, object] | None
    baseline_age: SentruxBaselineAge


def sentrux_is_warn_only(output: str) -> bool:
    """Return True when Sentrux reported only configured warning-level deltas."""
    lines = output.splitlines()
    failing = [ln for ln in lines if ln.strip().startswith("✗") and "DEGRADED" not in ln]
    if not failing:
        return False
    return all(_SENTRUX_WARN_ONLY.search(ln) for ln in failing) and not any(
        _SENTRUX_HARD_FAIL.search(ln) for ln in lines
    )


def sentrux_output_degraded(output: str) -> bool:
    lowered = output.lower()
    if "no degradation" in lowered:
        return False
    return "degradation" in lowered or "degraded" in lowered


def changed_files_since(
    project_root: Path,
    base_ref: str,
    extensions: Sequence[str],
) -> list[str]:
    output = _git_stdout(project_root, ["diff", "--name-only", base_ref, "HEAD"])
    if not output:
        return []

    seen: set[str] = set()
    files: list[str] = []
    for path in output.splitlines():
        if path in seen or not any(path.endswith(ext) for ext in extensions):
            continue
        seen.add(path)
        files.append(path)
    return files


def assess_sentrux_gate(
    *,
    scan_root: Path,
    project_root: Path,
    baseline_path: Path,
    sentrux_output: str,
    sentrux_returncode: int,
    changed_files: Sequence[str],
    base_ref: str | None,
    mode: str,
    stale_commits: int,
    stale_days: int,
) -> SentruxGateAssessment:
    baseline_age = sentrux_baseline_age(project_root, baseline_path)
    current_report: dict[str, object] | None = None
    new_offenders: tuple[SentruxOffender, ...] = ()
    preexisting_offenders: tuple[SentruxOffender, ...] = ()

    clean_return = sentrux_returncode == 0 and not sentrux_output_degraded(sentrux_output)
    if clean_return or sentrux_is_warn_only(sentrux_output):
        return SentruxGateAssessment(
            should_fail=False,
            warning=None,
            error=None,
            new_offenders=new_offenders,
            preexisting_offenders=preexisting_offenders,
            current_report=current_report,
            baseline_age=baseline_age,
        )

    current_report = _current_offender_report(scan_root, project_root)
    baseline_report = _offender_report_at_ref(project_root, base_ref) if base_ref else None
    current_offenders = _offenders_from_reports(current_report, _current_sentrux_json(scan_root))
    baseline_ids = {
        offender.identity() for offender in _offenders_from_reports(baseline_report, None)
    }
    changed_lines = (
        _changed_lines_by_file(project_root, base_ref, changed_files) if base_ref else {}
    )
    new_offenders, preexisting_offenders = _classify_offenders(
        current_offenders,
        baseline_ids=baseline_ids,
        baseline_known=baseline_report is not None,
        changed_files=changed_files,
        changed_lines=changed_lines,
    )

    strict_mode = mode == "strict"
    hard_new_offenders = tuple(offender for offender in new_offenders if offender.hard)
    if strict_mode or hard_new_offenders:
        error = _format_degradation_error(
            sentrux_output,
            baseline_age,
            new_offenders,
            preexisting_offenders,
        )
        return SentruxGateAssessment(
            should_fail=True,
            warning=None,
            error=error,
            new_offenders=new_offenders,
            preexisting_offenders=preexisting_offenders,
            current_report=current_report,
            baseline_age=baseline_age,
        )

    warning = None
    if baseline_age.stale(commit_threshold=stale_commits, day_threshold=stale_days):
        warning = (
            "WARNING: Sentrux baseline is stale "
            f"({baseline_age.describe()}); no diff-attributable structural offenders "
            "were found. Run `dgov sentrux gate-save` if this drift is intentional."
        )

    return SentruxGateAssessment(
        should_fail=False,
        warning=warning,
        error=None,
        new_offenders=new_offenders,
        preexisting_offenders=preexisting_offenders,
        current_report=current_report,
        baseline_age=baseline_age,
    )


def sentrux_baseline_age(project_root: Path, baseline_path: Path) -> SentruxBaselineAge:
    baseline_commit = _baseline_commit(project_root, baseline_path)
    commits_behind = _commits_behind(project_root, baseline_commit)
    timestamp = _baseline_timestamp(baseline_path)
    seconds_old = max(0.0, time.time() - timestamp) if timestamp is not None else None
    return SentruxBaselineAge(
        baseline_commit=baseline_commit,
        commits_behind=commits_behind,
        timestamp=timestamp,
        seconds_old=seconds_old,
    )


def _format_seconds_old(seconds: float) -> str:
    days = int(seconds // _SECONDS_PER_DAY)
    if days >= 1:
        return f"{days} days old"
    hours = int(seconds // 3600)
    if hours >= 1:
        return f"{hours} hours old"
    minutes = int(seconds // 60)
    return f"{minutes} minutes old"


def _git_stdout(project_root: Path, args: list[str]) -> str | None:
    result = subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _baseline_commit(project_root: Path, baseline_path: Path) -> str | None:
    with contextlib.suppress(ValueError):
        rel_path = baseline_path.resolve().relative_to(project_root.resolve()).as_posix()
        output = _git_stdout(project_root, ["log", "-1", "--format=%H", "--", rel_path])
        return output or None
    return None


def _commits_behind(project_root: Path, baseline_commit: str | None) -> int | None:
    if baseline_commit is None:
        return None
    output = _git_stdout(project_root, ["rev-list", "--count", f"{baseline_commit}..HEAD"])
    if output is None:
        return None
    with contextlib.suppress(ValueError):
        return int(output)
    return None


def _baseline_timestamp(baseline_path: Path) -> float | None:
    data = _json_object(baseline_path)
    value = data.get("timestamp")
    if isinstance(value, int | float):
        return float(value)
    return None


def _json_object(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _current_offender_report(scan_root: Path, project_root: Path) -> dict[str, object] | None:
    try:
        return likely_structural_offenders(
            scan_root,
            cache_root=project_root,
            limit=_FULL_OFFENDER_LIMIT,
        )
    except Exception:
        return None


def _offender_report_at_ref(project_root: Path, ref: str | None) -> dict[str, object] | None:
    if not ref:
        return None
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=project_root,
        capture_output=True,
        check=False,
    )
    if archive.returncode != 0:
        return None

    with tempfile.TemporaryDirectory(prefix="dgov-sentrux-base-") as tmp:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
            tar.extractall(tmp, filter="data")
        try:
            return likely_structural_offenders(
                Path(tmp),
                limit=_FULL_OFFENDER_LIMIT,
                commit_sha=ref,
            )
        except Exception:
            return None


def _current_sentrux_json(scan_root: Path) -> dict[str, object]:
    return _json_object(scan_root / ".sentrux" / "current.json")


def _offenders_from_reports(
    repo_report: dict[str, object] | None,
    sentrux_current: dict[str, object] | None,
) -> tuple[SentruxOffender, ...]:
    offenders: list[SentruxOffender] = []
    if repo_report is not None:
        offenders.extend(_function_offenders(repo_report))
    if sentrux_current is not None:
        offenders.extend(_cycle_offenders(sentrux_current))
        offenders.extend(_god_file_offenders(sentrux_current))
    return tuple(offenders)


def _function_offenders(report: Mapping[str, object]) -> Iterable[SentruxOffender]:
    for key, label, metric, hard in _FUNCTION_SECTIONS:
        raw_items = report.get(key)
        if not isinstance(raw_items, list):
            continue
        for raw in raw_items:
            if not isinstance(raw, Mapping):
                continue
            item = cast(Mapping[str, object], raw)
            path = str(item.get("path") or "")
            qualname = str(item.get("qualname") or "")
            if not path or not qualname:
                continue
            yield SentruxOffender(
                kind=key,
                label=label,
                path=_normalize_rel_path(path),
                name=qualname,
                lineno=_int_or_none(item.get("lineno")),
                end_lineno=_int_or_none(item.get("end_lineno")),
                metric=metric,
                value=_int_or_none(item.get(metric)),
                hard=hard,
            )


def _cycle_offenders(current: Mapping[str, object]) -> Iterable[SentruxOffender]:
    cycles = current.get("cycles")
    if not isinstance(cycles, list):
        return ()
    offenders: list[SentruxOffender] = []
    for idx, raw in enumerate(cycles, start=1):
        paths = sorted(_paths_from_object(raw))
        offenders.append(
            SentruxOffender(
                kind="cycles",
                label="Cycles",
                path=paths[0] if paths else "",
                name=_short_object_name(raw, fallback=f"cycle {idx}"),
                lineno=None,
                end_lineno=None,
                metric="cycle",
                value=None,
                hard=True,
            )
        )
    return tuple(offenders)


def _god_file_offenders(current: Mapping[str, object]) -> Iterable[SentruxOffender]:
    god_files = current.get("god_files")
    if not isinstance(god_files, list):
        return ()
    offenders: list[SentruxOffender] = []
    for raw in god_files:
        paths = sorted(_paths_from_object(raw))
        path = paths[0] if paths else _short_object_name(raw, fallback="")
        if not path:
            continue
        offenders.append(
            SentruxOffender(
                kind="god_files",
                label="God files",
                path=path,
                name="" if path else _short_object_name(raw, fallback="god file"),
                lineno=None,
                end_lineno=None,
                metric="god_file",
                value=None,
                hard=True,
            )
        )
    return tuple(offenders)


def _short_object_name(value: object, *, fallback: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        value_map = cast(Mapping[str, object], value)
        for key in ("path", "file", "module", "name"):
            raw = value_map.get(key)
            if isinstance(raw, str) and raw:
                return raw
    if isinstance(value, Sequence) and not isinstance(value, str):
        names = [item for item in value if isinstance(item, str)]
        if names:
            return " -> ".join(names[:4])
    return fallback


def _paths_from_string(value: str) -> set[str]:
    return _path_candidates(value)


def _paths_from_mapping(value: Mapping) -> set[str]:
    paths: set[str] = set()
    for key, raw in value.items():
        path_key = isinstance(key, str) and key in {"path", "file", "module", "name"}
        nested_value = isinstance(raw, Mapping | Sequence) and not isinstance(raw, str)
        if path_key or nested_value:
            paths.update(_paths_from_object(raw))
    return paths


def _paths_from_sequence(value: Sequence) -> set[str]:
    paths: set[str] = set()
    for item in value:
        paths.update(_paths_from_object(item))
    return paths


def _paths_from_object(value: object) -> set[str]:
    if isinstance(value, str):
        return _paths_from_string(value)
    if isinstance(value, Mapping):
        return _paths_from_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, str):
        return _paths_from_sequence(value)
    return set()


def _path_candidates(value: str) -> set[str]:
    raw = value.strip()
    if not raw:
        return set()
    normalized = _normalize_rel_path(raw)
    candidates = {normalized}
    if "." in raw and "/" not in raw and not raw.endswith(".py"):
        module_path = raw.replace(".", "/") + ".py"
        candidates.add(module_path)
        candidates.add(f"src/{module_path}")
    return candidates


def _classify_offenders(
    offenders: Sequence[SentruxOffender],
    *,
    baseline_ids: set[tuple[str, str, str]],
    baseline_known: bool,
    changed_files: Sequence[str],
    changed_lines: Mapping[str, set[int]],
) -> tuple[tuple[SentruxOffender, ...], tuple[SentruxOffender, ...]]:
    changed_set = {_normalize_rel_path(path) for path in changed_files}
    new: list[SentruxOffender] = []
    preexisting: list[SentruxOffender] = []
    for offender in offenders:
        new_in_head = offender.identity() not in baseline_ids if baseline_known else False
        attributable = _offender_attributable(offender, changed_set, changed_lines)
        if new_in_head or attributable:
            new.append(offender)
        else:
            preexisting.append(offender)
    return tuple(new), tuple(preexisting)


def _offender_attributable(
    offender: SentruxOffender,
    changed_files: set[str],
    changed_lines: Mapping[str, set[int]],
) -> bool:
    path_matches = _matching_changed_path(offender.path, changed_files)
    if path_matches is None:
        return False
    if offender.lineno is None or offender.end_lineno is None:
        return True
    lines = changed_lines.get(path_matches)
    if not lines:
        return True
    return any(offender.lineno <= line <= offender.end_lineno for line in lines)


def _matching_changed_path(path: str, changed_files: set[str]) -> str | None:
    for candidate in _path_candidates(path):
        if candidate in changed_files:
            return candidate
    return None


def _changed_lines_by_file(
    project_root: Path,
    base_ref: str | None,
    changed_files: Sequence[str],
) -> dict[str, set[int]]:
    if not base_ref:
        return {}
    changed: dict[str, set[int]] = {}
    for rel_path in changed_files:
        lines = _changed_lines_for_file(project_root, base_ref, rel_path)
        if lines:
            changed[_normalize_rel_path(rel_path)] = lines
    return changed


def _changed_lines_for_file(project_root: Path, base_ref: str, rel_path: str) -> set[int]:
    result = subprocess.run(
        ["git", "diff", "--unified=0", base_ref, "HEAD", "--", rel_path],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return set()
    lines: set[int] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("@@"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        lines.update(range(start, start + count))
    return lines


def _format_degradation_error(
    sentrux_output: str,
    baseline_age: SentruxBaselineAge,
    new_offenders: Sequence[SentruxOffender],
    preexisting_offenders: Sequence[SentruxOffender],
) -> str:
    parts = ["Sentrux architectural degradation:"]
    output = sentrux_output.strip()
    if output:
        parts.append(output)
    parts.append(f"Baseline age: {baseline_age.describe()}")
    parts.append(_format_offender_group("NEW Sentrux offenders", new_offenders))
    parts.append(_format_offender_group("PRE-EXISTING Sentrux offenders", preexisting_offenders))
    return "\n".join(parts)


def _format_offender_group(title: str, offenders: Sequence[SentruxOffender]) -> str:
    lines = [f"{title}:"]
    if not offenders:
        lines.append("  none")
        return "\n".join(lines)

    grouped: dict[str, list[SentruxOffender]] = {}
    for offender in offenders:
        grouped.setdefault(offender.label, []).append(offender)

    for label, items in grouped.items():
        lines.append(f"- {label}:")
        for offender in items[:10]:
            lines.append(offender.line())
        if len(items) > 10:
            lines.append(f"  ... and {len(items) - 10} more")
    return "\n".join(lines)


def _normalize_rel_path(path: str) -> str:
    normalized = PurePosixPath(path.replace("\\", "/")).as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return int(value)
    return None


__all__ = [
    "SentruxBaselineAge",
    "SentruxGateAssessment",
    "SentruxOffender",
    "assess_sentrux_gate",
    "changed_files_since",
    "sentrux_baseline_age",
    "sentrux_is_warn_only",
    "sentrux_output_degraded",
]
