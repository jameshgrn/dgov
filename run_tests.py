#!/usr/bin/env python3
"""Simple test runner for observation and kernel_service tests."""
import sys
sys.path.insert(0, '/Users/jakegearon/projects/dgov/src')

import pytest

if __name__ == "__main__":
    result = pytest.main([
        "/Users/jakegearon/projects/dgov/tests/test_observation.py",
        "/Users/jakegearon/projects/dgov/tests/test_kernel_service.py",
        "-q",
        "-m", "unit",
        "--tb=short"
    ])
    sys.exit(result)