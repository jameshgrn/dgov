# Pi-Sentrux Integration — Complete

## What was done

Created two integration paths for Sentrux + Pi:

### 1. MCP Config (Primary)
- **File**: `~/.pi/agent/mcp.json`
- **Content**: Points Pi's MCP adapter at `sentrux --mcp`
- **Status**: ✅ Installed and valid JSON

### 2. Pi Extension Package (Optional)
- **Dir**: `pi-sentrux/`
- **Provides**: `sentrux_scan`, `sentrux_gate_start`, `sentrux_gate_end` tools + `/sentrux` command
- **Status**: ✅ Package structure complete, dependencies installed
- **Install**: `cd pi-sentrux && pi install .`

## Verification
- `sentrux --version` → 0.5.7 ✓
- `~/.pi/agent/mcp.json` → valid JSON ✓
- `pi-sentrux/` → has package.json, src/index.ts, README.md ✓

## Next steps (if needed)
1. Install the Pi MCP adapter package if not already present
2. Test: `pi -e ./pi-sentrux/src/index.ts` for one-off test
3. Or: `cd pi-sentrux && pi install .` for permanent install

---

# dgov + Sentrux Integration — Complete

## Architecture

**Deep integration in plan execution lifecycle** — no shims, direct subprocess calls.

### Changes Made

1. **`src/dgov/cli/__init__.py`**:
   - Converted CLI from `click.Command` to `click.Group` to support subcommands
   - Added `dgov sentrux` command group with: `check`, `gate-save`, `gate`, `status`
   - Modified `_cmd_run_plan()` to automatically:
     - Save sentrux baseline before plan execution
     - Compare against baseline after plan execution
     - Include sentrux results in final JSON output

2. **Helper functions**:
   - `_sentrux_available()` — check if binary exists
   - `_run_sentrux(args, cwd, timeout)` — direct subprocess execution

### Usage

```bash
# Manual commands
dgov sentrux status           # Check availability
dgov sentrux check .          # Run architectural check
dgov sentrux gate-save .      # Save baseline
dgov sentrux gate .           # Compare to baseline
dgov sentrux gate --fail-on-degradation  # Exit 1 if degraded

# Automatic in plan execution
dgov run plan.toml            # Saves baseline → runs plan → compares baseline
```

### Plan Execution Flow

1. **Pre-flight**: If sentrux available, save baseline → parse quality score
2. **Execute**: Run plan tasks via EventDagRunner
3. **Post-flight**: Compare against baseline → detect degradation
4. **Output**: JSON includes `sentrux: {degradation, quality_before, quality_after}`

### Verification Results

- ✅ `dgov sentrux status` → "installed and available"
- ✅ `dgov sentrux check --json-output` → `{"quality": 7104, "path": "."}`
- ✅ `dgov sentrux gate-save` → "Baseline saved at .sentrux/baseline.json"
- ✅ `dgov sentrux gate` → "✓ No degradation detected"
- ✅ `dgov run test.toml` → "[sentrux] Saving baseline... [sentrux] Gate result: ✓ clean"

## Ruff Check
- All checks passed on `src/dgov/cli/__init__.py`
