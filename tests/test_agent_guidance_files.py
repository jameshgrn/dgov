import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
GUIDANCE_FILES = [ROOT / "AGENTS.md", ROOT / "CLAUDE.md", ROOT / "GEMINI.md"]
REQUIRED_SNIPPETS = (
    "Status: `LOCKED`",
    "Canonical Source: `AGENTS.md`",
    ".dgov/governor.md",
)


def test_agent_guidance_files_are_identical_and_locked() -> None:
    contents = [path.read_text() for path in GUIDANCE_FILES]
    assert contents[1:] == [contents[0], contents[0]]
    assert re.search(r"Instruction Pack Version: `\d+\.\d+\.\d+`", contents[0])
    for snippet in REQUIRED_SNIPPETS:
        assert snippet in contents[0]
