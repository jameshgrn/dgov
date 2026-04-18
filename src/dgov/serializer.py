"""Serializer — converts BundleResult to TOML output for dispatch.

This module handles the final serialization phase of the compile pipeline,
producing a flat PlanSpec TOML compatible with `parse_dag_file`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from dgov.sop_bundler import BundleResult


def serialize_compiled_toml(
    bundle_result: BundleResult,
    source_mtime_max: float,
) -> str:
    """Serialize a BundleResult into flat PlanSpec TOML (dispatch-ready).

    Produces `[plan]` + `[tasks."<fq_id>"]` sections compatible with
    `parse_dag_file`. Imports BundleResult lazily to avoid circular deps.
    """
    br = bundle_result
    plan = br.plan
    meta = plan.root_meta

    ts = datetime.fromtimestamp(source_mtime_max, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    lines: list[str] = [
        "[plan]",
        f"name = {_toml_str(meta.name)}",
        f"source_mtime_max = {_toml_str(ts)}",
        f"sop_set_hash = {_toml_str(br.sop_set_hash)}",
        "",
    ]

    for fq_id in sorted(plan.units):
        unit = plan.units[fq_id]
        mapping = br.sop_mapping.get(fq_id, ())
        lines.append(f"[tasks.{_toml_key(fq_id)}]")
        lines.append(f"summary = {_toml_str(unit.summary)}")
        lines.append(f"prompt = {_toml_ml_str(unit.prompt)}")
        lines.append(f"commit_message = {_toml_str(unit.commit_message)}")
        if unit.agent:
            lines.append(f"agent = {_toml_str(unit.agent)}")
        if unit.role != "worker":
            lines.append(f"role = {_toml_str(unit.role)}")
        if unit.depends_on:
            deps = ", ".join(_toml_str(d) for d in unit.depends_on)
            lines.append(f"depends_on = [{deps}]")
        if unit.timeout_s:
            lines.append(f"timeout_s = {unit.timeout_s}")
        if unit.iteration_budget is not None:
            lines.append(f"iteration_budget = {unit.iteration_budget}")
        if unit.test_cmd:
            lines.append(f"test_cmd = {_toml_str(unit.test_cmd)}")
        if mapping:
            items = ", ".join(_toml_str(m) for m in mapping)
            lines.append(f"sop_mapping = [{items}]")
        # files — flat list (touch) or structured sub-table
        has_files = (
            unit.files.create
            or unit.files.edit
            or unit.files.delete
            or unit.files.read
            or unit.files.touch
        )
        if has_files:
            if unit.files.touch and not (
                unit.files.create or unit.files.edit or unit.files.delete or unit.files.read
            ):
                # Pure flat list — serialize as `files = [...]`
                lines.append(f"files = [{', '.join(_toml_str(f) for f in unit.files.touch)}]")
            else:
                if unit.files.touch:
                    lines.append(
                        f"files.touch = [{', '.join(_toml_str(f) for f in unit.files.touch)}]"
                    )
                if unit.files.create:
                    lines.append(
                        f"files.create = [{', '.join(_toml_str(f) for f in unit.files.create)}]"
                    )
                if unit.files.edit:
                    lines.append(
                        f"files.edit = [{', '.join(_toml_str(f) for f in unit.files.edit)}]"
                    )
                if unit.files.delete:
                    lines.append(
                        f"files.delete = [{', '.join(_toml_str(f) for f in unit.files.delete)}]"
                    )
                if unit.files.read:
                    lines.append(
                        f"files.read = [{', '.join(_toml_str(f) for f in unit.files.read)}]"
                    )
        lines.append("")

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
