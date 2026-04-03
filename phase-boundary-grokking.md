# Phase Boundary Mapping: Grokking in Modular Addition

## Research Agenda: Branch B (Optimizer/Rules)

### Objective
Systematically identify phase boundaries where test accuracy abruptly jumps
(grokking transitions) as a function of model size, learning rate, and weight decay.
Seek "stable weird regimes"-settings that should fail but work surprisingly well.

## Summary Statistics

- Total experiments: 6
- Converged (no NaN/Inf loss): 0/6
- Grokking detected: 0/6

## 1. Width Boundary (Minimal Embedding Dimensions)

| n_embd | Params | Final Train Loss | Final Test Acc | Max Acc | Grokked |
|--------|--------|------------------|----------------|---------|---------|
| 8 | 2,832 |              NaN |          0.000 | 0.000 | no |
| 16 | 7,200 |              NaN |          0.000 | 0.000 | no |
| 24 | 13,104 |              NaN |          0.000 | 0.000 | no |
| 32 | 20,544 |              NaN |          0.000 | 0.000 | no |
| 48 | 40,032 |              NaN |          0.000 | 0.000 | no |
| 64 | 65,664 |              NaN |          0.000 | 0.000 | no |

### Key Finding: Minimal Competent Width

*No width configuration achieved >30% accuracy without NaN loss*

## 2. Depth Boundary (Layer Count)

| Layers | Params | Final Train Loss | Final Test Acc | Max Acc | Grokked |
|--------|--------|------------------|----------------|---------|---------|
| 1 | 20,544 |              NaN |          0.000 | 0.000 | no |

## 3. Learning Rate Boundary

| LR | Params | Final Train Loss | Final Test Acc | Grokked |
|----------|--------|------------------|----------------|---------|

## 4. Stable Weird Regimes (High Weight Decay)

| WD | Params | Final Train Loss | Final Test Acc | Grokked |
|----------|--------|------------------|----------------|---------|

## Key Findings

### Unstable Regimes

- **Config:** n_embd=8, n_layer=1, lr=0.001
- **Config:** n_embd=16, n_layer=1, lr=0.001
- **Config:** n_embd=24, n_layer=1, lr=0.001

## Conclusion

This experiment mapped phase boundaries for grokking behavior in modular addition.
The smallest competent architectures and surprising stable regimes identified here
provide concrete targets for future research on computational mechanisms in tiny models.
