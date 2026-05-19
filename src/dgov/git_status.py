"""Helpers for parsing git status porcelain output."""

from __future__ import annotations


def decode_porcelain_path(path: str) -> str:
    """Decode a path from git status porcelain output."""
    value = path.strip()
    if not (value.startswith('"') and value.endswith('"')):
        return value
    return _decode_c_style_path(value[1:-1])


def porcelain_status_paths(
    output: str,
    *,
    include_rename_sources: bool = False,
) -> tuple[str, ...]:
    """Return changed paths from `git status --porcelain` output."""
    paths: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        path_part = _porcelain_path_part(line)
        rename_paths = _split_rename_paths(path_part)
        if rename_paths is not None:
            old_path, new_path = rename_paths
            if include_rename_sources:
                paths.append(decode_porcelain_path(old_path))
            paths.append(decode_porcelain_path(new_path))
            continue
        paths.append(decode_porcelain_path(path_part))
    return tuple(paths)


def _porcelain_path_part(line: str) -> str:
    if len(line) > 2 and line[2] == " ":
        return line[3:]
    return line[2:].lstrip()


def _split_rename_paths(path_part: str) -> tuple[str, str] | None:
    in_quotes = False
    escaped = False
    for index, char in enumerate(path_part):
        if escaped:
            escaped = False
            continue
        if in_quotes and char == "\\":
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes and path_part.startswith(" -> ", index):
            return path_part[:index], path_part[index + 4 :]
    return None


def _decode_c_style_path(path: str) -> str:
    decoded = bytearray()
    index = 0
    while index < len(path):
        char = path[index]
        if char != "\\":
            decoded.extend(char.encode("utf-8", errors="surrogateescape"))
            index += 1
            continue
        index = _append_escaped_byte(path, index + 1, decoded)
    return bytes(decoded).decode("utf-8", errors="surrogateescape")


def _append_escaped_byte(path: str, index: int, decoded: bytearray) -> int:
    if index >= len(path):
        decoded.append(ord("\\"))
        return index

    char = path[index]
    if char in "01234567":
        end = index + 1
        while end < len(path) and end < index + 3 and path[end] in "01234567":
            end += 1
        byte_value = int(path[index:end], 8)
        if byte_value <= 0xFF:
            decoded.append(byte_value)
        else:
            decoded.extend(path[index - 1 : end].encode("utf-8", errors="surrogateescape"))
        return end

    escapes = {
        "a": b"\a",
        "b": b"\b",
        "f": b"\f",
        "n": b"\n",
        "r": b"\r",
        "t": b"\t",
        "v": b"\v",
        '"': b'"',
        "\\": b"\\",
    }
    decoded.extend(escapes.get(char, char.encode("utf-8", errors="surrogateescape")))
    return index + 1
