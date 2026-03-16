"""Smoke tests for DAG runner validation files."""

import pytest

from dgov._dag_test_a import add, multiply
from dgov._dag_test_b import divide, subtract


class TestDagTestA:
    def test_add(self):
        assert add(2, 3) == 5

    def test_multiply(self):
        assert multiply(4, 5) == 20


class TestDagTestB:
    def test_subtract(self):
        assert subtract(10, 3) == 7

    def test_divide(self):
        assert divide(10, 2) == 5.0

    def test_divide_by_zero(self):
        with pytest.raises(ValueError, match="Cannot divide by zero"):
            divide(1, 0)
