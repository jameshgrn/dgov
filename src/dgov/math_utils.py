"""Math utilities for modular arithmetic."""


class ModularAddition:
    """Performs modular addition with proper handling of negative values."""

    @staticmethod
    def add(a: int, b: int, p: int) -> int:
        """
        Compute (a + b) mod p, ensuring the result is always non-negative.

        Args:
            a: First operand
            b: Second operand
            p: Modulus (must be positive)

        Returns:
            The result of (a + b) mod p in the range [0, p-1]

        Raises:
            ValueError: If p is not positive
        """
        if p <= 0:
            raise ValueError(f"Modulus p must be positive, got {p}")

        result = (a + b) % p
        return result
