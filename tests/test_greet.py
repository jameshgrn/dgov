import pytest

from dgov.greet import greet

pytestmark = pytest.mark.unit


def test_greet_returns_message():
    assert greet("Jake") == "Hello, Jake! Welcome to dgov."


def test_greet_empty_name():
    assert greet("") == "Hello, ! Welcome to dgov."
