import textwrap
from pathlib import Path

from dgov.settlement import SmartFixer


def test_smart_fix_b904(tmp_path: Path):
    """Test that bare raise NewError() in except blocks gets 'from exc'."""
    content = textwrap.dedent("""
        def foo():
            try:
                pass
            except Exception as exc:
                raise RuntimeError("wrapped")
    """).strip()

    path = tmp_path / "test.py"
    path.write_text(content)

    sf = SmartFixer(tmp_path, line_length=40)
    sf.fix_all(["test.py"])

    fixed = path.read_text()
    # ast.unparse might change " to '
    assert "raise RuntimeError('wrapped') from exc" in fixed or 'raise RuntimeError("wrapped") from exc' in fixed


def test_smart_fix_e501_prose(tmp_path: Path):
    """Test that long prose comments are wrapped."""
    # A comment that is exactly 60 chars (plus indent)
    content = textwrap.dedent("""
        def foo():
            # This is a very long prose comment that definitely exceeds the forty character limit we set for this test.
            pass
    """).strip()

    path = tmp_path / "test.py"
    path.write_text(content)

    sf = SmartFixer(tmp_path, line_length=40)
    sf.fix_all(["test.py"])

    fixed = path.read_text()
    lines = fixed.splitlines()
    # Check that it wrapped
    assert len(lines) > 2
    for line in lines:
        if "#" in line:
            assert len(line) <= 40


def test_smart_fix_e501_skips_urls(tmp_path: Path):
    """Test that URLs are not wrapped (as they would break)."""
    url = "https://example.com/some/very/long/path/that/should/not/be/wrapped/at/all/it/must/stay/intact"
    content = f"# {url}\n"

    path = tmp_path / "test.py"
    path.write_text(content)

    sf = SmartFixer(tmp_path, line_length=40)
    sf.fix_all(["test.py"])

    fixed = path.read_text()
    assert url in fixed
