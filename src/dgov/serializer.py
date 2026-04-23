"""Serializer — converts BundleResult to TOML output for dispatch.

This module handles the final serialization phase of the compile pipeline,
producing a flat PlanSpec TOML compatible with `parse_dag_file`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from dgov.plan import PlanUnit, PlanUnitFiles
from dgov.plan_tree import FlatPlan
from dgov.sop_bundler import BundleResult


def _format_timestamp(source_mtime_max: float) -> str:
    """Format a Unix timestamp as ISO 8601 UTC string."""
    return datetime.fromtimestamp(source_mtime_max, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _format_plan_section(name: str, timestamp: str, sop_set_hash: str) -> list[str]:
    """Format the [plan] section header with metadata."""
    return [
        "[plan]",
        f"name = {_toml_str(name)}",
        f"source_mtime_max = {_toml_str(timestamp)}",
        f"sop_set_hash = {_toml_str(sop_set_hash)}",
        "",
    ]


def _format_prompt_fields(unit: PlanUnit) -> list[str]:
    """Format prompt-related fields (prompt_file and prompt)."""
    lines: list[str] = []
    if unit.prompt_file:
        lines.append(f"prompt_file = {_toml_str(unit.prompt_file)}")
    if unit.prompt:
        lines.append(f"prompt = {_toml_ml_str(unit.prompt)}")
    else:
        lines.append('prompt = ""')
    return lines


def _format_optional_string_field(value: str | None, key: str) -> list[str]:
    """Format an optional string field if value is present."""
    if value:
        return [f"{key} = {_toml_str(value)}"]
    return []


def _format_optional_int_field(value: int | None, key: str) -> list[str]:
    """Format an optional integer field if value is present."""
    if value is not None:
        return [f"{key} = {value}"]
    return []


def _format_string_array_field(items: tuple[str, ...] | list[str], key: str) -> list[str]:
    """Format a string array field if items are present."""
    if items:
        formatted = ", ".join(_toml_str(item) for item in items)
        return [f"{key} = [{formatted}]"]
    return []


def _format_role_field(role: str) -> list[str]:
    """Format role field only if non-default ('worker')."""
    if role != "worker":
        return [f"role = {_toml_str(role)}"]
    return []


def _has_structured_files(files: PlanUnitFiles) -> bool:
    """Check if files has any non-touch categories."""
    return bool(files.create or files.edit or files.delete or files.read)


def _format_files(files: PlanUnitFiles) -> list[str]:
    """Format files section - either flat or structured depending on content."""
    lines: list[str] = []
    has_any = files.create or files.edit or files.delete or files.read or files.touch

    if not has_any:
        return lines

    # Pure touch-only case: use flat format
    if files.touch and not _has_structured_files(files):
        lines.append(f"files = [{', '.join(_toml_str(f) for f in files.touch)}]")
        return lines

    # Structured format with sub-keys
    if files.touch:
        lines.append(f"files.touch = [{', '.join(_toml_str(f) for f in files.touch)}]")
    if files.create:
        lines.append(f"files.create = [{', '.join(_toml_str(f) for f in files.create)}]")
    if files.edit:
        lines.append(f"files.edit = [{', '.join(_toml_str(f) for f in files.edit)}]")
    if files.delete:
        lines.append(f"files.delete = [{', '.join(_toml_str(f) for f in files.delete)}]")
    if files.read:
        lines.append(f"files.read = [{', '.join(_toml_str(f) for f in files.read)}]")

    return lines


def _format_task_section(fq_id: str, unit: PlanUnit, mapping: tuple[str, ...]) -> list[str]:
    """Format a single [tasks."<fq_id>"] section with all its fields."""
    lines: list[str] = []

    # Section header and key metadata
    lines.append(f"[tasks.{_toml_key(fq_id)}]")
    lines.append(f"summary = {_toml_str(unit.summary)}")

    # Prompt fields
    lines.extend(_format_prompt_fields(unit))

    # Core required field
    lines.append(f"commit_message = {_toml_str(unit.commit_message)}")

    # Optional string fields
    lines.extend(_format_optional_string_field(unit.agent, "agent"))
    lines.extend(_format_role_field(unit.role))

    # Arrays
    lines.extend(_format_string_array_field(unit.depends_on, "depends_on"))

    # Optional numeric fields
    lines.extend(_format_optional_int_field(unit.timeout_s, "timeout_s"))
    lines.extend(_format_optional_int_field(unit.iteration_budget, "iteration_budget"))

    # Optional test command
    lines.extend(_format_optional_string_field(unit.test_cmd, "test_cmd"))

    # sop_mapping comes after test_cmd, before files
    lines.extend(_format_string_array_field(mapping, "sop_mapping"))

    # Files section
    lines.extend(_format_files(unit.files))

    lines.append("")
    return lines


def _format_all_task_sections(
    plan: FlatPlan, sop_mapping: dict[str, tuple[str, ...]]
) -> list[str]:
    """Format all task sections in sorted order."""
    lines: list[str] = []
    for fq_id in sorted(plan.units):
        unit = plan.units[fq_id]
        mapping = sop_mapping.get(fq_id, ())
        lines.extend(_format_task_section(fq_id, unit, mapping))
    return lines


def serialize_compiled_toml(
    bundle_result: BundleResult,
    source_mtime_max: float,
) -> str:
    """Serialize a BundleResult into flat PlanSpec TOML (dispatch-ready).

    Produces `[plan]` + `[tasks."<fq_id>"]` sections compatible with
    `parse_dag_file`.
    """
    br = bundle_result
    plan = br.plan
    meta = plan.root_meta

    ts = _format_timestamp(source_mtime_max)

    lines: list[str] = []
    lines.extend(_format_plan_section(meta.name, ts, br.sop_set_hash))
    lines.extend(_format_all_task_sections(plan, br.sop_mapping))

    return "\n".join(lines)


def _toml_str(value: str) -> str:
    """Wrap a string in TOML double quotes, escaping as needed."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _toml_ml_str(value: str) -> str:
    """Use TOML multi-line basic string for prompts."""
    if "\n" not in value:
        return _toml_str(value)
    # Escape backslashes first, then escape any """ sequences so they don't
    # terminate the multi-line string early.
    safe = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'"""\n{safe}"""'


def _toml_key(fq_id: str) -> str:
    """Quote a fq_id as a TOML key if it contains special characters."""
    if re.match(r"^[A-Za-z0-9_-]+$", fq_id):
        return fq_id
    return f'"{fq_id}"'
