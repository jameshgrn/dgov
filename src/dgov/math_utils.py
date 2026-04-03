class ModularAddition:
    """A utility class for modular addition operations."""

    @staticmethod
    def add(a: int, b: int, p: int) -> int:
        """
        Compute (a + b) mod p, handling negative results correctly.
        
        Args:
            a: First integer operand
            b: Second integer operand
            p: Modulus (must be positive)
        
        Returns:
            The result of (a + b) mod p, always in range [0, p-1]
        
        Raises:
            ValueError: If modulus p is not positive
        """
        if p <= 0:
            raise ValueError(f"Modulus p must be positive, got {p}")
        
        result = (a + b) % p
        return result
