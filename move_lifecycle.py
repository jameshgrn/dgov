#!/usr/bin/env python3
"""Move lifecycle.py to lifecycle/_lifecycle.py"""
import shutil
import os

src = "/Users/jakegearon/projects/dgov/src/dgov/lifecycle.py"
dst = "/Users/jakegearon/projects/dgov/src/dgov/lifecycle/_lifecycle.py"

if os.path.exists(src) and not os.path.exists(dst):
    shutil.move(src, dst)
    print(f"Moved {src} to {dst}")
else:
    print(f"Source exists: {os.path.exists(src)}, Dest exists: {os.path.exists(dst)}")
