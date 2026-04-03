#!/usr/bin/env python3
"""Create the classic dgov layout: agent left, terrain + attach right."""

import subprocess
import sys

def create_layout():
    session = "dgov-layout-demo"
    
    # Kill existing
    subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
    
    # Create new session
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", "main"], check=True)
    
    # Split into left (60%) and right (40%)
    subprocess.run(["tmux", "split-window", "-h", "-p", "40", "-t", f"{session}:0"], check=True)
    
    # Split right side into top (terrain) and bottom (attach)
    subprocess.run(["tmux", "split-window", "-v", "-p", "50", "-t", f"{session}:0.1"], check=True)
    
    # Set pane titles
    subprocess.run(["tmux", "select-pane", "-t", f"{session}:0.0", "-T", "pi-agent-coder"], check=True)
    subprocess.run(["tmux", "select-pane", "-t", f"{session}:0.1", "-T", "terrain-panes"], check=True)
    subprocess.run(["tmux", "select-pane", "-t", f"{session}:0.2", "-T", "dgov-attach"], check=True)
    
    # Send commands to panes
    # Left: placeholder for pi agent coder
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.0", 
                   "echo '=== pi agent coder ===' && echo 'Working on: src/dgov/cli.py'", "C-m"], check=True)
    
    # Right top: placeholder for terrain (worker panes)
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.1",
                   "echo '=== terrain ===' && echo 'task-a: DISPATCHED (cli.py)' && echo 'task-b: PENDING (cli.py)'", "C-m"], check=True)
    
    # Right bottom: run attach
    subprocess.run(["tmux", "send-keys", "-t", f"{session}:0.2",
                   f"cd /Users/jakegearon/projects/dgov && uv run python3 -c '\
import json; \
from dgov.kernel import DagKernel; \
data = json.load(open(\"/tmp/demo-kernel.json\")); \
k = DagKernel.from_dict(data); \
print(\"=== dgov attach ===\"); \
print(); \
print(\"WORKERS:\"); \
[print(f\"  {s.value:12} {n}\") for n,s in k.task_states.items()]; \
print(); \
print(\"FILES:\"); \
[print(f\"  {n} -> {f}\") for n,f in k.task_files.items()]'", "C-m"], check=True)
    
    # Focus on left pane
    subprocess.run(["tmux", "select-pane", "-t", f"{session}:0.0"], check=True)
    
    print(f"""
╔══════════════════════════════════════════════════════════════════════════╗
║                     LAYOUT CREATED: {session:20}                      ║
╠══════════════════════════════════════════════════════════════════════════╣
║                                                                           ║
║   ┌──────────────────────────────┬──────────────────┐                  ║
║   │                              │  terrain-panes   │                  ║
║   │                              │  (worker status) │                  ║
║   │   pi-agent-coder             ├──────────────────┤                  ║
║   │   (main coding pane)         │  dgov-attach     │                  ║
║   │                              │  (read-only view)│                  ║
║   │                              │                  │                  ║
║   └──────────────────────────────┴──────────────────┘                  ║
║                                                                           ║
║   Attach to view:  tmux attach -t {session}                                ║
║   Kill layout:     tmux kill-session -t {session}                        ║
║                                                                           ║
╚══════════════════════════════════════════════════════════════════════════╝
""")

if __name__ == "__main__":
    create_layout()
