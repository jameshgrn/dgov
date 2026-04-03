"""Tests for math_utils module."""

import pytest
from dgov.math_utils import ModularAddition


class TestModularAddition:
    """Test cases for ModularAddition with p=113."""
    
    def test_basic_addition_no_wrap(self):
        """Test basic addition that doesn't wrap around."""
        mod_add = ModularAddition(p=113)
        # 50 + 30 = 80 (no wrap needed)
        result = mod_add.add(50, 30)
        assert result == 80
        
    def test_addition_with_wraparound(self):
        """Test addition that wraps around modulo 113."""
        mod_add = ModularAddition(p=113)
        # 100 + 50 = 150 mod 113 = 37
        result = mod_add.add(100, 50)
        assert result == 37
        
    def test_addition_at_boundary(self):
        """Test addition at the exact boundary."""
        mod_add = ModularAddition(p=113)
        # 56 + 57 = 113 mod 113 = 0
        result = mod_add.add(56, 57)
        assert result == 0
        
    def test_callable_interface(self):
        """Test that ModularAddition instance is callable."""
        mod_add = ModularAddition(p=113)
        # Test using __call__ interface
        result = mod_add(70, 60)
        assert result == (70 + 60) % 113
        assert result == 17
        
    def test_zero_addition(self):
        """Test adding zero."""
        mod_add = ModularAddition(p=113)
        # 42 + 0 = 42
        result = mod_add.add(42, 0)
        assert result == 42
