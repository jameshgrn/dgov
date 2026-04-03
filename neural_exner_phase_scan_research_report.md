# Neural Exner Phase Scan: River Morphodynamics Competence Boundaries

## Executive Summary

**Research Question**: What are the minimal architecture requirements for learning river morphodynamics (Exner equation)? Where do phase transitions occur in competence, stability, and mass conservation?

**Methodology**: Systematic phase boundary mapping across embedding dimensions (16-256), layer depths (1-8), attention causalities (upwind vs symmetric), and hyperparameter regimes.

## 10-Point Research Report

### 1. Research Objective
Identify phase transition points in river physics modeling that separate:
- **NaN explosion** from **stable learning**
- **Unlearnable** from **competent** architectures  
- **Symmetric artifacts** from **physics-aligned** solutions

### 2. Experimental Design

| Phase | Parameter Swept | Values Tested | Hypothesis |
|-------|----------------|---------------|------------|
| Width Boundary | `n_embd` (embedding dim) | 16, 32, 48, 64, 96, 128, 192, 256 | There's a minimum dimension below which gradients cannot flow |
| Depth Boundary | `n_layer` (transformer depth) | 1, 2, 3, 4, 6, 8 | Below some depth, compositional computation is impossible |
| Causality Phase | Spatial attention mask | Causal (upwind-biased), Symmetric | Upwind should outperform as scale decreases |
| Weird Regimes | Learning rate / weight decay | (1e-5, 0.5), (1e-6, 1.0), (1e-3, 0.1) | Extreme regularization might induce robustness |

### 3. Architecture Specifications

**Base Model**: `MassConservingTransformer`
- Flux-first divergence operator: $q_{t+dt} = q_t - \Delta t \cdot \nabla \cdot Q(\eta)$
- Erosion/deposition closure: $\frac{d\eta}{dt} = \tau \cdot |Q|^\alpha$
- Node projection: 5-d input (x, y, η, time, mask) → n_embd
- Time embedding: learnable sinusoidal encoding for t ∈ [0,1]

**Key Feature**: Mass-conserving architecture ensures discrete continuity equation is enforced by construction, not learned.

### 4. Baseline: Previous Advection Wall Breakthrough

**Result**: Upwind-biased (causal) attention achieves **2.07x lower MSE** than symmetric attention over 100-step rollouts.
- Symmetric: MSE = 2.944
- Upwind: MSE = 1.420

**Implication for Phase Scan**: Causality is not just an inductive bias—it may be the **minimal requirement** for competence at small scales.

### 5. Hypothesized Phase Transitions

#### Transition Point 1: NaN Explosion Threshold
**Prediction**: Below n_embd ≈ 32, gradients explode due to insufficient representational capacity for flow divergence terms.

**Mechanism**: 
- Too few dimensions → attention head collapse
- Singular Jacobians in flux computation
- Mass conservation constraint becomes over-constrained

#### Transition Point 2: Competence Onset
**Prediction**: Test loss < 0.05 emerges at n_embd ≈ 64-96 with n_layer ≥ 2.

**Mechanism**: 
- Sufficient width for multi-head attention to encode different flow regimes (rapid, mild, supercritical)
- Depth enables composition of erosion/deposition operators

#### Transition Point 3: Mass Conservation Emergence
**Prediction**: Below n_embd ≈ 128, best mass error > 5% despite low test loss.

**Mechanism**: 
- Architecture enforces continuity by design but numerical precision requires sufficient width
- Small models "cheat" by memorizing patterns rather than learning physical closure

### 6. Cluster Execution Plan

**Remote Path**: `/not_backed_up/jgearon/tinymodels/phase-a-neural-exner`

**Command Protocol**:
```bash
# Sync code
rsync -avz experiments/neural_exner_phase_scan.py \
    jgearon@river.emes.unc.edu:/not_backed_up/jgearon/tinymodels/phase-a-neural-exner/

# Execute on L40S GPUs (tcsh env syntax)
ssh jgearon@river.emes.unc.edu "env CUDA_VISIBLE_DEVICES=0,1 \
    /not_backed_up/jgearon/tinymodels/phase-a-neural-exner/.venv/bin/python \
    experiments/neural_exner_phase_scan.py"

# Sync results back
rsync -avz jgearon@river.emes.unc.edu:/not_backed_up/jgearon/tinymodels/phase-a-neural-exner/reports/ ./reports/
```

**Estimated Runtime**: 2-4 hours on dual L40S (8 GPUs available, we use 2 for this sweep).

### 7. Evaluation Metrics

| Metric | Threshold for Competence | Physical Meaning |
|--------|--------------------------|------------------|
| Test Loss (MSE) | < 0.05 | Shape preservation over rollouts |
| Mass Error | < 1% | Continuity equation satisfaction |
| NaN Events | 0 | Training stability |
| Params | N/A → Track for scaling laws |

### 8. Emergent Behavior Discovery Targets

#### Phase A: Smallest Competent Architecture
- Find minimal n_embd where test_loss < 0.05
- Measure parameter count vs accuracy curve
- Hypothesis: **n_embd=64** achieves competence (10x smaller than typical transformer)

#### Phase B: Stable Weird Regimes  
- Extreme learning rates that "should" explode but don't
- Ultra-high weight decay inducing regularization via mass conservation
- **Hypothesis**: LR=1e-5, WD=0.5 creates stable learning in narrow band

#### Phase C: Depth vs Width Tradeoffs
- Is shallow-and-wide better than deep-and-narrow for river physics?
- **Hypothesis**: Width dominates (n_layer=2 sufficient if n_embd≥96)

### 9. Expected Scientific Output

**Discovery 1: Discrete Competence Thresholds**
River morphodynamics models exhibit phase transitions, not smooth scaling curves. This contrasts with natural language where BERT-style scaling laws hold continuously.

**Discovery 2: Upwind Advantage at Scale Boundaries**
Causal attention should outperform symmetric by larger margins in the "just-competent" regime (n_embd=48-64) vs large-capacity regime (n_embd≥192).

**Discovery 3: Minimal Model Size for Physical Learning**
If we find competence at n_embd=64, this implies river physics is simpler to learn than previously assumed—potentially enabling real-time flood forecasting with edge devices.

### 10. Next Steps After Cluster Execution

1. **Analyze NaN vs competent boundary**: Plot loss stability vs embedding dimension
2. **Fit scaling law**: Power-law fit of test_loss vs params for n_embd≥64 points
3. **Visualize attention patterns**: Do small models develop upwind bias naturally?
4. **Mass conservation analysis**: Is mass error correlated with test loss or orthogonal?
5. **Expand sweep**: If n_embd=96 is minimal, scan finer-grained (72, 80, 88)

---

**Status**: Awaiting cluster execution via `run_phase_scan_cluster.sh`  
**Files Ready**: 
- Experiment: `experiments/neural_exner_phase_scan.py`
- Cluster runner: `run_phase_scan_cluster.sh`
- Report template: this file

**Contact**: jgearon@unc.edu | River Emes Cluster Access: Required