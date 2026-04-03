"""Mathematical utilities for dgov."""


class ModularAddition:
    """Class for performing modular addition operations."""

    @staticmethod
    def add(a: int, b: int, p: int) -> int:
        """
        Compute (a + b) mod p.

        Args:
            a: First operand
            b: Second operand
            p: Modulus (must be positive)

        Returns:
            The result of (a + b) mod p, guaranteed to be in [0, p-1].

        Raises:
            ValueError: If p is not positive.
        """
        if p <= 0:
            raise ValueError("Modulus p must be positive")
        
        result = (a + b) % p
        # Ensure result is non-negative (Python's % already does this, but explicit for clarity)
        if result < 0:
            result += p
        return result
