"""Tests for dgov.plan.scaffold_plan function."""

import tomllib

import pytest

from dgov.plan import parse_plan_file, scaffold_plan, validate_plan


@pytest.mark.unit
def test_scaffold_produces_valid_toml():
    """scaffold_plan produces parseable TOML that passes validation."""
    import os
    import tempfile

    toml_text = scaffold_plan("Fix the widget", ["src/widget.py", "tests/test_widget.py"])
    # Write to temp file and parse
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(toml_text)
        tmp_path = f.name
    try:
        plan = parse_plan_file(tmp_path)
        issues = validate_plan(plan)
        errors = [i for i in issues if i.severity == "error"]
        assert not errors, f"Scaffold produced invalid plan: {errors}"
    finally:
        os.unlink(tmp_path)


@pytest.mark.unit
def test_scaffold_includes_files():
    """scaffold_plan includes the specified files in edit list."""
    toml_text = scaffold_plan("Add feature", ["src/foo.py", "src/bar.py"])
    parsed = tomllib.loads(toml_text)
    # TOML nested tables: parsed["units"] is a dict of unit dicts
    units = parsed.get("units", {})
    assert len(units) == 1
    unit = next(iter(units.values()))
    assert "src/foo.py" in unit["files"]["edit"]
    assert "src/bar.py" in unit["files"]["edit"]


@pytest.mark.unit
def test_scaffold_derives_name():
    """scaffold_plan derives name from goal when not provided."""
    toml_text = scaffold_plan("Fix the broken parser module", ["src/parser.py"])
    parsed = tomllib.loads(toml_text)
    name = parsed["plan"]["name"]
    assert name  # not empty
    assert " " not in name  # slugified
    assert name.islower() or "-" in name  # lowercase with hyphens


@pytest.mark.unit
def test_scaffold_provides_custom_name():
    """scaffold_plan uses provided name when given."""
    toml_text = scaffold_plan("This goal will be ignored", ["src/parser.py"], name="custom-name")
    parsed = tomllib.loads(toml_text)
    assert parsed["plan"]["name"] == "custom-name"


@pytest.mark.unit
def test_scaffold_slugifies_truncates_to_40():
    """scaffold_plan truncates slugified names to 40 characters."""
    long_goal = "This is a very long goal description that will be truncated to forty chars"
    toml_text = scaffold_plan(long_goal, ["src/parser.py"])
    parsed = tomllib.loads(toml_text)
    name = parsed["plan"]["name"]
    assert len(name) <= 40


@pytest.mark.unit
def test_scaffold_has_two_evals():
    """scaffold_plan generates exactly two placeholder evals."""
    toml_text = scaffold_plan("Add feature", ["src/foo.py"])
    parsed = tomllib.loads(toml_text)
    evals = parsed.get("evals", [])
    assert len(evals) == 2
    eval_ids = [e["id"] for e in evals]
    assert "E1" in eval_ids
    assert "E2" in eval_ids


@pytest.mark.unit
def test_scaffold_sets_defaults():
    """scaffold_plan sets default_agent, default_timeout_s, max_retries."""
    toml_text = scaffold_plan("Add feature", ["src/foo.py"])
    parsed = tomllib.loads(toml_text)
    plan = parsed["plan"]
    assert plan["default_agent"] == "qwen-35b"
    assert plan["default_timeout_s"] == 300
    assert plan["max_retries"] == 2


@pytest.mark.unit
def test_scaffold_unit_satisfies_evals():
    """The generated unit satisfies both E1 and E2."""
    toml_text = scaffold_plan("Add feature", ["src/foo.py"])
    parsed = tomllib.loads(toml_text)
    units = parsed.get("units", {})
    unit = next(iter(units.values()))
    assert set(unit["satisfies"]) == {"E1", "E2"}
