"""Type-check diagnostic parsing helpers."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

_DIAG_COUNT_RE = re.compile(r"Found (\d+) diagnostics?")
_DIAG_ERROR_CODE_RE = re.compile(r"^error\[([^\]]+)\]:", re.MULTILINE)
_DIAG_FILE_PATH_RE = re.compile(r"^\s+-->\s+([^:]+):\d+:\d+", re.MULTILINE)


def count_diagnostics(output: str) -> int:
    """Parse 'Found N diagnostics' from type checker output."""
    m = _DIAG_COUNT_RE.search(output)
    return int(m.group(1)) if m else 0


def parse_diagnostic_identities(
    output: str, project_root: Path | None = None
) -> set[tuple[str, str]]:
    """Extract stable (relative_file, error_code) pairs from type checker output."""
    identities: set[tuple[str, str]] = set()
    error_codes = _DIAG_ERROR_CODE_RE.findall(output)
    file_paths = _DIAG_FILE_PATH_RE.findall(output)

    for i, code in enumerate(error_codes):
        if i >= len(file_paths):
            continue
        file_path = file_paths[i]
        if project_root is not None:
            with contextlib.suppress(ValueError):
                file_path = str(Path(file_path).relative_to(project_root))
        identities.add((file_path, code))

    return identities
