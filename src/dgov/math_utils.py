"""Math utilities for modular arithmetic operations."""


class ModularAddition:
    """Provides modular addition operations with proper negative result handling."""

    @staticmethod
    def add(a: int, b: int, p: int) -> int:
        """
        Compute (a + b) mod p with proper handling of negative results.

        Args:
            a: First operand
            b: Second operand
            p: Modulus (must be positive)

        Returns:
            The result of (a + b) % p in the range [0, p-1]

        Raises:
            ValueError: If p is not positive
        """
        if p <= 0:
            raise ValueError(f"Modulus p must be positive, got {p}")

        result = (a + b) % p
        # Ensure result is non-negative even with Python's modulo behavior
        if result < 0:
            result += p

        return result
