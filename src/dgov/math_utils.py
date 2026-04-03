"""Math utilities for dgov."""


class ModularAddition:
    """Class for performing modular addition with correct handling of negative results."""

    def __init__(self, p: int):
        """Initialize with modulus p.

        Args:
            p: Modulus (must be positive).

        Raises:
            ValueError: If p is not positive.
        """
        if p <= 0:
            raise ValueError("Modulus p must be positive")
        self.p = p

    def add(self, a: int, b: int) -> int:
        """Compute (a + b) mod p, ensuring the result is in [0, p-1].

        Args:
            a: First operand.
            b: Second operand.

        Returns:
            The result of (a + b) mod p, normalized to [0, p-1].
        """
        return (a + b) % self.p

    def __call__(self, a: int, b: int) -> int:
        """Compute (a + b) mod p, ensuring the result is in [0, p-1].

        Args:
            a: First operand.
            b: Second operand.

        Returns:
            The result of (a + b) mod p, normalized to [0, p-1].
        """
        return self.add(a, b)
