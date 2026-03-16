"""Smoke-test file B for DAG runner validation."""


def subtract(x: int, y: int) -> int:
    """Return x - y."""
    return x - y


def divide(x: int, y: int) -> float:
    """Return x / y."""
    if y == 0:
        raise ValueError("Cannot divide by zero")
    return x / y


def modulo(x: int, y: int) -> int:
    """Return x mod y."""
    if y == 0:
        raise ValueError("Cannot modulo by zero")
    return x % y
