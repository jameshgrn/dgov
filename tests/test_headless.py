"""Tests for headless worker role dispatch."""

from pathlib import Path

import pytest

from dgov.workers.headless import _script_for_role


def test_script_for_worker_role() -> None:
    script = _script_for_role("worker")
    assert isinstance(script, Path)
    assert script.name == "worker.py"


def test_script_for_researcher_role() -> None:
    script = _script_for_role("researcher")
    assert isinstance(script, Path)
    assert script.name == "researcher.py"


def test_script_for_role_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="Unknown task role"):
        _script_for_role("mystery")
