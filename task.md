# Task: Map Phase Transition Boundaries in Modular Addition

## Background
Previous experiments with modular addition (p=7) on a transformer model showed failure to converge - the model maintained ~0% accuracy throughout training. This suggests either:
1. The task encoding is incorrect
2. The model architecture lacks capacity
3. Hyperparameters are poorly tuned

## Goal
Perform systematic parameter sweeps to find the boundary where modular addition becomes learnable vs. unlearnable.

## Parameters to Sweep
- Embedding dimension (embd_size): 32, 64, 128
- Number of layers: 1, 2, 4
- Modulo base (p): 3, 5, 7, 11
- Sequence length effects

## Expected Outcome
- Identify the phase transition boundary for learnability
- Produce a research report with findings

## Files to Use
- `src/data/toy_tasks.py` - ModularAddition dataset
- `src/models/transformer.py` - Model architecture  
- `run_exp.py` - Main experiment script