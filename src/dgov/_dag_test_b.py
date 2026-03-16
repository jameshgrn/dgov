"""Smoke-test file B for DAG runner validation."""


def subtract(x: int, y: int) -> int:
    """Return x - y."""
    return x - y


def divide(x: int, y: int) -> float:
    """Return x / y."""
    if y == 0:
        raise ValueError("Cannot divide by zero")
    return x / y
