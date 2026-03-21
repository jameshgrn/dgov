"""Compiled task context shared across preflight, prompts, and instructions."""

from __future__ import annotations

import re
from dataclasses import dataclass


def _dedupe(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


@dataclass(frozen=True)
class ContextPacket:
    prompt: str
    primary_files: tuple[str, ...] = ()
    also_check: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    file_claims: tuple[str, ...] = ()
    architecture_context: tuple[str, ...] = ()
    commit_message: str = ""

    @property
    def read_files(self) -> tuple[str, ...]:
        if self.file_claims:
            return _dedupe([*self.file_claims, *self.also_check])
        return _dedupe([*self.primary_files, *self.also_check])

    @property
    def edit_files(self) -> tuple[str, ...]:
        if self.file_claims:
            return self.file_claims
        return self.primary_files

    @property
    def touches(self) -> tuple[str, ...]:
        if self.file_claims:
            return self.file_claims
        return _dedupe([*self.primary_files, *self.also_check, *self.tests])


def infer_commit_message(prompt: str, explicit: str | None = None, max_len: int = 50) -> str:
    if explicit:
        return explicit.strip()
    first_line = prompt.strip().splitlines()[0] if prompt.strip() else "Worker changes"
    return first_line[:max_len].strip().rstrip(".") or "Worker changes"


def _extract_prompt_files(prompt: str) -> tuple[str, ...]:
    matches = re.findall(r"\b(?:src/|tests/)[\w\-\./]+|[\w\-\./]+\.\w+", prompt)
    files: list[str] = []
    seen: set[str] = set()
    for file_path in matches:
        normalized = file_path.strip("./")
        if (
            not normalized
            or normalized in seen
            or ("/" not in normalized and "." not in normalized)
        ):
            continue
        if re.match(r"^\d+(\.\d+)+$", normalized):
            continue
        files.append(normalized)
        seen.add(normalized)
    return tuple(files)


def build_context_packet(
    prompt: str,
    *,
    file_claims: list[str] | tuple[str, ...] | None = None,
    tests: list[str] | tuple[str, ...] | None = None,
    hints: list[str] | tuple[str, ...] | None = None,
    architecture_context: list[str] | tuple[str, ...] | None = None,
    commit_message: str | None = None,
) -> ContextPacket:
    from dgov.strategy import extract_task_context

    inferred = extract_task_context(prompt)
    claims = _dedupe(list(file_claims or ()))
    primary = claims or _dedupe(
        list(inferred.get("primary_files", [])) or list(_extract_prompt_files(prompt))
    )
    also_check = _dedupe(list(inferred.get("also_check", [])))
    packet_tests = _dedupe([*list(inferred.get("tests", [])), *list(tests or ())])
    packet_hints = _dedupe([*list(inferred.get("hints", [])), *list(hints or ())])
    packet_architecture_context = _dedupe(
        list(inferred.get("architecture_context", [])) or list(architecture_context or ())
    )

    return ContextPacket(
        prompt=prompt,
        primary_files=primary,
        also_check=also_check,
        tests=packet_tests,
        hints=packet_hints,
        architecture_context=packet_architecture_context,
        file_claims=claims,
        commit_message=infer_commit_message(prompt, explicit=commit_message),
    )


def render_start_here_section(packet: ContextPacket) -> str:
    if not any(
        [
            packet.file_claims,
            packet.primary_files,
            packet.also_check,
            packet.tests,
            packet.hints,
            packet.commit_message,
        ]
    ):
        return ""

    lines = ["## Start here\n"]
    if packet.file_claims:
        lines.append("Exact edit claims:\n")
        for file_path in packet.file_claims:
            lines.append(f"- {file_path}\n")
        lines.append("\n")
    if packet.read_files:
        lines.append("Read first:\n")
        for file_path in packet.read_files:
            lines.append(f"- {file_path}\n")
        lines.append("\n")
    if packet.tests:
        lines.append("Tests:\n")
        for file_path in packet.tests:
            lines.append(f"- {file_path}\n")
        lines.append("\n")
    if packet.hints:
        lines.append("Hints:\n")
        for hint in packet.hints:
            lines.append(f"- {hint}\n")
        lines.append("\n")
    if packet.architecture_context:
        lines.append("Architecture context:\n")
        for ctx in packet.architecture_context:
            lines.append(f"- {ctx}\n")
        lines.append("\n")
    if packet.commit_message:
        lines.append(f"Commit message: `{packet.commit_message}`\n\n")
    return "".join(lines)
