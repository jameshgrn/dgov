#!/usr/bin/env bash
# sentrux-track.sh — Continuous structural quality logging
# Usage: ./sentrux-track.sh [message]

set -euo pipefail

LOGFILE=".sentrux/history.log"
mkdir -p .sentrux

# Capture metrics
RESULT=$(sentrux gate . 2>&1)
EXIT_CODE=$?

# Extract metrics from output
QUALITY=$(echo "$RESULT" | grep -oE "Quality:[^0-9]*[0-9]+" | grep -oE "[0-9]+" || echo "0")
COUPLING=$(echo "$RESULT" | grep -oE "Coupling:[^0-9.]*[0-9.]+" | grep -oE "[0-9.]+" || echo "0")
CYCLES=$(echo "$RESULT" | grep -oE "Cycles:[^0-9]*[0-9]+" | grep -oE "[0-9]+" || echo "0")
GODFILES=$(echo "$RESULT" | grep -oE "God files:[^0-9]*[0-9]+" | grep -oE "[0-9]+" || echo "0")
STATUS=$([ $EXIT_CODE -eq 0 ] && echo "PASS" || echo "FAIL")

# Build log entry
TS=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
MSG="${1:-auto}"
ENTRY="$TS | $STATUS | Q:$QUALITY C:$COUPLING CY:$CYCLES G:$GODFILES | $MSG"

# Append to log
echo "$ENTRY" >>"$LOGFILE"

# Show current state
echo "=== SENTRUX TRACK ==="
echo "$ENTRY"
echo ""
echo "$RESULT"

# Show history trend (last 5)
echo ""
echo "=== LAST 5 RUNS ==="
tail -5 "$LOGFILE" 2>/dev/null || echo "(no history yet)"
