"""Math utilities for dgov."""


class ModularAddition:
    """Provides modular addition functionality."""

    @staticmethod
    def add(a: int, b: int, p: int) -> int:
        """
        Perform modular addition: (a + b) mod p.

        Args:
            a: First operand.
            b: Second operand.
            p: Modulus (must be positive).

        Returns:
            The result of (a + b) mod p, always in range [0, p-1].

        Raises:
            ValueError: If p is not positive.
        """
        if p <= 0:
            raise ValueError("Modulus p must be positive")
        return (a + b) % p
