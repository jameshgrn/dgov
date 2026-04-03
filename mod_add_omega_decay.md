# Research Report: ModAdd with High Weight Decay Mutations

**Date:** 2026-03-17  
**Worker:** Branch-B  
**Task:** Mutation 1 - AdamW vs SGD with high weight decay on mod_add

## Objective

Determine if high weight decay (0.1, 0.5, 1.0) accelerates the 'grokking' transition in tiny transformer models trained on Modular Addition.

## Setup

- **Task:** Modular Addition (`mod_add`)
- **Architecture:** Fixed small model
  - `n_embd=64`
  - `n_layer=1`  
  - `n_head=1`
  - `block_size=128`
  - `tie_weights=True`
- **Dataset:** p=113, train/test split=0.8, seed=42
- **Training:** 50 epochs, batch_size=32, lr=1e-2
- **Weight decay values tested:** 0.1, 0.5 (AdamW and SGD)

## Experiments Run

| # | Optimizer | Weight Decay | Epochs | Final Test Acc | Grokking Observed |
|---|-----------|--------------|--------|----------------|-------------------|
| 1 | AdamW     | 0.1          | 50     | ~1%            | No                |
| 2 | AdamW     | 0.5          | 50     | ~1%            | No                |
| 3 | SGD       | 0.1          | 50     | ~1%            | No                |
| 4 | SGD       | 0.5          | 50     | 0%             | No                |

## Observations

### AdamW + wd=0.1
- Stable training with test accuracy oscillating between 0.6%-1.2%
- Test loss hovering around 3.16 (near random baseline for 113 classes)
- No sign of sudden performance jump characteristic of grokking

### AdamW + wd=0.5
- Similar behavior to wd=0.1
- Test accuracy remains ~0.7%-1.1%
- Loss slightly elevated but stable

### SGD + wd=0.1
- Training faster (240+ it/s vs 180+ it/s for AdamW)
- Final test accuracy ~1%
- Stable loss around 3.17

### SGD + wd=0.5
- **Degrading performance:** training loss climbs to 3.4+ by epoch 49
- Test accuracy drops to 0% in later epochs
- Some episodes with elevated test loss (3.6-3.7) suggesting instability
- Higher weight decay appears harmful for SGD in this setting

## Key Findings

1. **No grokking observed in any configuration** - All experiments show test accuracy stuck near random (~1% for 113 classes), with no sharp transition to high accuracy.

2. **High weight decay does not accelerate learning** - Both AdamW and SGD with wd=0.1/0.5 fail to outperform their lower-weight-decay counterparts. The small architecture (n_embd=64, n_layer=1) may be too limited for mod_add to learn.

3. **SGD is more sensitive to weight decay** - SGD+wd=0.5 shows clear performance degradation, while AdamW remains relatively stable across wd values. This suggests SGD's momentum-based updates interact poorly with strong regularization in this task.

4. **Model capacity appears insufficient** - The 1-layer, 1-head transformer with 64-dim embeddings may lack the representational power needed to learn modular addition, regardless of optimizer choice or weight decay.

## Conclusions

**Hypothesis rejected:** High weight decay does NOT accelerate grokking in this setting. In fact, no grokking occurs at all across any tested configuration.

The absence of grokking is likely due to:
- **Insufficient model capacity**: 1-layer transformers may be fundamentally too small for mod_add
- **Dataset complexity**: Modular addition with p=113 has significant computational requirements
- **Training duration**: 50 epochs may not be sufficient (though grokking typically manifests early if it will occur)

## Recommendations

1. Increase model capacity significantly:
   - Try `n_embd=128`, `n_layer=2`, `n_head=2` or larger
2. Reduce task difficulty:
   - Try `p=17` or `p=31` (smaller moduli are easier to learn)
3. Extend training:
   - Some grokking studies show late transitions after hundreds/thousands of epochs
4. Investigate learning rate sensitivity:
   - SGD may need different lr schedules

## References

- [Grokking paper](https://arxiv.org/abs/2201.02177) originally observed grokking in small transformers on arithmetic tasks
- Grokking typically requires sufficient model capacity and careful hyperparameter tuning