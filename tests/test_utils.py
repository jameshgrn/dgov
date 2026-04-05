import pytest

from dgov.utils import slug_safe, truncate

pytestmark = pytest.mark.unit


def test_truncate_short():
    assert truncate("hello") == "hello"


def test_truncate_long():
    assert truncate("a" * 100, max_len=10) == "aaaaaaa..."


def test_truncate_exact():
    assert truncate("a" * 80) == "a" * 80


def test_slug_safe_basic():
    assert slug_safe("Hello World") == "hello-world"


def test_slug_safe_special_chars():
    assert slug_safe("fix/bug #42") == "fix-bug--42"
