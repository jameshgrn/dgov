"""Math utilities for dgov."""


class ModularAddition:
    """Class for performing modular addition with correct handling of negative results."""

    @staticmethod
    def add(a: int, b: int, p: int) -> int:
        """Compute (a + b) mod p, ensuring the result is in [0, p-1].

        Args:
            a: First operand.
            b: Second operand.
            p: Modulus (must be positive).

        Returns:
            The result of (a + b) mod p, normalized to [0, p-1].

        Raises:
            ValueError: If p is not positive.
        """
        if p <= 0:
            raise ValueError("Modulus p must be positive")
        return (a + b) % p
