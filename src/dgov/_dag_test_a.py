"""Smoke-test file A for DAG runner validation."""


def add(x: int, y: int) -> int:
    """Return x + y."""
    return x + y


def multiply(x: int, y: int) -> int:
    """Return x * y."""
    return x * y


def greet(name: str) -> str:
    """Return a greeting string."""
    return f"Hello, {name}!"


def power(base: int, exp: int) -> int:
    """Return base raised to exp."""
    return base**exp


def negate(x: int) -> int:
    """Return -x."""
    return -x
