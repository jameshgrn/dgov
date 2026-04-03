#!/usr/bin/env python3
"""Visual demo: Show tmux panes being created with conflict visualization."""

import subprocess
import time
import tempfile
from pathlib import Path

from dgov.cli.run import parse_unit_toml
from dgov.tmux import create_background_pane, send_keys_with_session
from dgov.unit import UnitSpec
from dgov.unit_compile import compile_unit


def get_tmux_panes(session: str = "dgov"):
    """Get list of pane titles in session."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-s", "-t", session, "-F", "#{pane_id}:#{pane_title}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    except subprocess.CalledProcessError:
        return []


def visual_demo():
    print("=" * 70)
    print("VISUAL DEMO: Tmux Panes + Conflict Detection")
    print("=" * 70)

    session = "dgov-demo"

    # Check if tmux is running
    try:
        subprocess.run(["tmux", "has-session", "-t", session], check=True, capture_output=True)
        print(f"\n⚠️  Session '{session}' already exists. Kill it first?")
        resp = input("Kill existing session? [y/N]: ")
        if resp.lower() == "y":
            subprocess.run(["tmux", "kill-session", "-t", session], capture_output=True)
        else:
            print("Aborting demo.")
            return
    except subprocess.CalledProcessError:
        pass  # Session doesn't exist, good

    # Create session
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-n", "governor"], capture_output=True)
    print(f"\n✅ Created tmux session: {session}")

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create 4 units: A,B conflict; C,D conflict; no cross-conflicts
        units = [
            ("worker-a", "tests/file-x.txt", "Content A", "touch tests/file-x.txt && echo 'A' > tests/file-x.txt"),
            ("worker-b", "tests/file-x.txt", "Content B", "touch tests/file-x.txt && echo 'B' >> tests/file-x.txt"),  # CONFLICT
            ("worker-c", "tests/file-y.txt", "Content C", "touch tests/file-y.txt && echo 'C' > tests/file-y.txt"),
            ("worker-d", "tests/file-y.txt", "Content D", "touch tests/file-y.txt && echo 'D' >> tests/file-y.txt"),  # CONFLICT
        ]

        print("\n" + "-" * 70)
        print("CONFLICT MATRIX")
        print("-" * 70)
        print("  Worker A → tests/file-x.txt")
        print("  Worker B → tests/file-x.txt  ⚠️  CONFLICTS with A")
        print("  Worker C → tests/file-y.txt")
        print("  Worker D → tests/file-y.txt  ⚠️  CONFLICTS with C")
        print()
        print("  Expected: A and C run in parallel (different files)")
        print("            B waits for A, D waits for C")

        print("\n" + "-" * 70)
        print("CREATING PANES")
        print("-" * 70)

        panes = []
        for name, file, content, cmd in units:
            # Create a simple demo command that shows the file claim
            display_cmd = f'''
echo "=== {name} ==="
echo "File: {file}"
echo "Status: Starting..."
sleep 2
echo "Status: Complete!"
sleep 100
'''
            pane_id = create_background_pane(
                command=f"bash -c {repr(display_cmd)}",
                title=f"[{name}] {file}",
                cwd=tmpdir,
                session=session,
            )
            panes.append((name, file, pane_id))
            print(f"  ✅ Created {name}: {pane_id}")

        # Show current panes
        time.sleep(0.5)
        print("\n" + "-" * 70)
        print("TMUX PANES")
        print("-" * 70)
        pane_list = get_tmux_panes(session)
        for pane in pane_list:
            print(f"  {pane}")

        print("\n" + "-" * 70)
        print("SIMULATING CONFLICT RESOLUTION")
        print("-" * 70)

        # Simulate: A and C can run (no conflict)
        # B blocked by A, D blocked by C

        for name, file, pane_id in panes:
            status = "RUNNING"
            if name == "worker-a":
                status = "🔄 RUNNING (first to claim file-x)"
            elif name == "worker-b":
                status = "⏳ QUEUED (waits for worker-a)"
            elif name == "worker-c":
                status = "🔄 RUNNING (first to claim file-y)"
            elif name == "worker-d":
                status = "⏳ QUEUED (waits for worker-c)"

            send_keys_with_session(
                pane_id=pane_id,
                keys=f"\necho '*** STATUS UPDATE ***'\necho '{status}'",
                session=session,
            )
            print(f"  {name:15} → {status}")

        print("\n" + "-" * 70)
        print("ATTACH TO TMUX TO SEE VISUAL:")
        print(f"  tmux attach -t {session}")
        print()
        print("Or view pane list:")
        print(f"  tmux list-panes -s -t {session}")
        print()
        print("Kill session when done:")
        print(f"  tmux kill-session -t {session}")
        print("-" * 70)

        print("\n" + "=" * 70)
        print("DEMO COMPLETE")
        print("=" * 70)
        print("""
The visual demo shows:
  1. Four panes created (one per worker)
  2. Titles show [worker] filename format
  3. Conflict matrix visualized in status messages

In the real system:
  • Kernel checks file claims before dispatch
  • Conflicting workers stay in PENDING state
  • Non-conflicting workers run immediately
  • When worker A completes, worker B is dispatched automatically
""")


if __name__ == "__main__":
    visual_demo()
