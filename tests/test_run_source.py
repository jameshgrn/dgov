from __future__ import annotations

import pytest

from dgov.run_source import current_run_source, normalize_run_source

pytestmark = pytest.mark.unit


def test_current_run_source_defaults_to_manual() -> None:
    assert current_run_source({}) == "manual"
    assert current_run_source({"DGOV_RUN_SOURCE": "  "}) == "manual"


def test_current_run_source_normalizes_env_value() -> None:
    assert current_run_source({"DGOV_RUN_SOURCE": "Workshop"}) == "workshop"
    assert current_run_source({"DGOV_RUN_SOURCE": "workshop:alpha-1"}) == "workshop:alpha-1"


def test_normalize_run_source_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="DGOV_RUN_SOURCE"):
        normalize_run_source("work shop")

    with pytest.raises(ValueError, match="DGOV_RUN_SOURCE"):
        normalize_run_source("-workshop")
