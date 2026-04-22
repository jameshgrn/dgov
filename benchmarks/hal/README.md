# dgov × HAL Harness

Benchmarking dgov's governor pipeline against a raw model baseline using
Princeton's [HAL Harness](https://github.com/princeton-pli/hal-harness).

## Agents

| Agent | What it tests |
|-------|--------------|
| `dgov_pipeline` | Full dgov: plan → worktree → Kimi K2.5 worker → settlement → merge |
| `kimi_baseline` | Raw Kimi K2.5 single call (no tools, no settlement) |

The meaningful comparison: same model, same problem — does the dgov harness
improve pass rate? And at what cost/latency overhead?

## Setup

```bash
# 1. Clone HAL
git clone --recursive https://github.com/princeton-pli/hal-harness.git
cd hal-harness

# 2. Create env (HAL wants Python 3.12)
conda create -n hal python=3.12
conda activate hal
pip install -e .

# 3. Install dgov (from local checkout)
pip install -e /path/to/dgov

# 4. Set API keys
export FIREWORKS_API_KEY="..."
# Optional: set WEAVE keys for cost tracking (see HAL docs)
```

## Running

### Smoke test — baseline on mini split (50 tasks)

```bash
hal-eval \
  --benchmark swebench_verified_mini \
  --agent_dir /path/to/dgov/benchmarks/hal/agents/kimi_baseline \
  --agent_function main.run \
  --agent_name "kimi-k2.5-baseline" \
  --max_concurrent 5
```

### dgov pipeline on mini split

```bash
hal-eval \
  --benchmark swebench_verified_mini \
  --agent_dir /path/to/dgov/benchmarks/hal/agents/dgov_pipeline \
  --agent_function main.run \
  --agent_name "dgov-kimi-k2.5" \
  --max_concurrent 3
```

Lower concurrency for dgov — each task clones a repo + spawns a worker subprocess.

## Known limitations (v0)

- **Cost tracking**: stub (returns 0.0). dgov doesn't track token costs yet.
- **Repo cloning**: full clone per task — slow for large repos (Django, etc.).
  Future: shared cache directory with `git worktree add` from a single clone.
- **Sentrux skipped**: the Python API bypasses the CLI's sentrux gate.
  Add it back once baseline.json generation is automated per-repo.
- **Baseline is single-call**: no tool loop. A fairer baseline would give
  Kimi the same tools (read/edit/bash) without dgov's settlement layer.
  That's the v1 baseline.

## What to measure

Beyond raw pass@1:

| Metric | Why it matters |
|--------|---------------|
| Pass rate | Does dgov solve more tasks? |
| Cost per task | What's the overhead of worktrees + settlement? |
| Patch size | Smaller patches = more surgical fixes |
| Settlement catches | How often does autofix/lint save a bad patch? |
| Wall time | Latency cost of the governor loop |
