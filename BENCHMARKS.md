# dgov Benchmarks

Model: Kimi K2.5 via Fireworks (`accounts/fireworks/routers/kimi-k2p5-turbo`)
Tasks: trivial single-function file creation
Platform: macOS Darwin 25.4.0, Python 3.14.3

## 2026-04-03: v1.0.0a1

### Before subprocess reduction (commit 27dfd6b)

| Topology | Tasks | Wall Time | Per-task avg |
|----------|-------|-----------|-------------|
| Parallel | 3 | 5.63s | 5.26s dispatch-to-merge |
| Chain (a->b->c) | 3 | 11.11s | 3.61s dispatch-to-merge |

### After subprocess reduction (commit 64eb1f9)

| Topology | Tasks | Wall Time | Per-task avg |
|----------|-------|-----------|-------------|
| Parallel | 3 | 5.13s | 4.84s dispatch-to-merge |
| Chain (a->b->c) | 3 | 13.20s | 4.34s dispatch-to-merge |

### Analysis

- **Parallel speedup**: 2.57x vs chain (5.13s vs 13.20s)
- **Subprocess reduction**: -0.50s wall time on parallel (-9%)
- **Chain variance**: chain times vary with Fireworks API latency (~3-5s per task)
- **Merge tail**: ~0.2s per task in serial merge queue (parallel)
- **Bottleneck**: ~90% of time is model inference, ~10% is git/lint overhead

### Notes

- Parallel dispatch-to-merge includes merge queue wait time
- Chain dispatch-to-merge is purely sequential (no queue contention)
- Settlement: autofix (ruff format + fix) before commit, read-only gate after
- Sentrux gate skipped (no baseline in test repos)
- Worktrees created as siblings of project root (not nested inside)
