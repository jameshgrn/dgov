#!/usr/bin/env python3
import py_compile
import sys

try:
    py_compile.compile('/Users/jakegearon/projects/dgov/src/dgov/lifecycle/create.py', doraise=True)
    print("Syntax OK")
except Exception as e:
    print(f"Syntax error: {e}")
    sys.exit(1)
