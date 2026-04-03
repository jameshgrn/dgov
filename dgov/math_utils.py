"""Math utilities for dgov."""


class ModularAddition:
    """Modular addition operation.
    
    Performs addition modulo a prime p.
    
    Args:
        p: The prime modulus.
    """
    
    def __init__(self, p: int):
        self.p = p
    
    def add(self, a: int, b: int) -> int:
        """Add two numbers modulo p.
        
        Args:
            a: First operand.
            b: Second operand.
            
        Returns:
            (a + b) mod p
        """
        return (a + b) % self.p
    
    def __call__(self, a: int, b: int) -> int:
        """Allow instance to be called as a function."""
        return self.add(a, b)
