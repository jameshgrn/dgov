# Architecture Spec: Neural Morphodynamic Operator (NMO)

This document defines the implementation details for the graph-native, mass-conserving neural operator for morphodynamics.

## 1. Data Structures (Graph-Native)
- **Node ($i$):** A spatial point (cross-section node or reach segment).
- **Features ($F_i$):** `[eta, h, u, x_norm, Q, slope, lambda]`
- **Edges ($E_{ij}$):**
  - `Type 1 (Downstream):` $i \to i+1$ (Advection path)
  - `Type 2 (Upstream):` $i \to i-1$ (Backwater/Information path)
  - `Type 3 (Lateral):` Optional neighbor connectivity.

## 0. Core Inductive Bias Mandate

**ARCHITECTURAL PRINCIPLE: Upwind-Biased Message Passing is REQUIRED.**

Empirical validation from multiscale generalization tests:
- **Symmetric attention:** TV Retention > 7 → unphysical wiggles, mass loss
- **Upwind-biased:** TV Retention ≈ 1.0 → stable, physically consistent

The NMO architecture must enforce causal asymmetry: nodes attend only to themselves and upstream neighbors. Symmetric or undirected attention mechanisms are prohibited.

## 2. Model Components

### A. FineGraphEncoder (Inductive Upwind Bias)
- **Layer:** Directed Message Passing Neural Network (MPNN).
- **Logic:** Strictly asymmetric message flow with distinct kernels:

**Kernel 1: Transport Flow ($i-1 \to i$)** - HIGH CAPACITY
- Purpose: Learn sediment flux closure relationships
- Capacity: Full hidden dimension $D$
- Form: $m_{\text{upwind}} = \text{MLP}_{\text{transport}}(h_{i-1}, h_i, v_{i-1\to i})$
- Activation: GELU, two-layer architecture
- Learning rate: Standard (unconstrained)

**Kernel 2: Information Flow ($i+1 \to i$)** - SUPPRESSED LOW CAPACITY
- Purpose: Encode water pressure/backwater effects only (no sediment transport)
- Capacity: $D/4$ hidden units + learnable suppression $\alpha \approx 0.1$
- Form: $m_{\text{info}} = \alpha \cdot \text{MLP}_{\text{info}}(h_{i+1}, h_i, p_{i+1})$
- Activation: GELU, single-layer architecture
- Constraint: $\alpha$ initialized at 0.1, bounded $[0.05, 0.3]$ during training

**Causal Mask:** Lower-triangular with diagonal (j ≤ i) enforced on all attention.

### B. Hierarchical Temporal Pyramid
- **Fast State ($z_{fast}$):** Updated every $\Delta t$. Encodes instantaneous flow and flux.
- **Event State ($z_{event}$):** Updated per hydrograph pulse. Encodes hydrograph "memory" (e.g., peak shear history).
- **Slow State ($z_{slow}$):** Updated over long rollouts. Encodes bed armoring, cover state, and substrate state.

### C. Multi-Head Output (Constrained Physics)
1. **Flux Head ($\Phi_{flux}$):** Predicts normalized sediment flux $q_s$.
   - *Constraint:* Positivity (Softplus/Exp).
2. **Incision Head ($\Phi_{incision}$):** Predicts effective erodibility modifier $K_{eff}$.
   - *Constraint:* Bounded $[0, 1]$ (Sigmoid) to modify a base incision law.
3. **Storage Head ($\Phi_{cover}$):** Predicts mobile sediment cover $C_{cover}$.
4. **Regime Head ($\Phi_{regime}$):** Diagnostic. Softmax over `[Transport, Incision, Storage]`.

## 3. Explicit Update Layer (Mass Conservation)
The network output is used inside a structural Exner update:
$$\eta_{t+1} = \eta_t - \Delta t \left( \nabla \cdot q_s \right) + (D - E_{constrained})$$
- **$\nabla \cdot q_s$:** Computed via finite difference on the graph edges.
- **This ensures machine-zero mass imbalance regardless of network weights.**

## 4. Tensor Shapes (Batch size $B$, Nodes $N$, Reaches $R$, Hidden $D$)
- **Input Nodes:** `[B, N, 7]`
- **Node Latent:** `[B, N, D]`
- **Reach Tokens:** `[B, R, D]` (via scatter-pooling)
- **Flux Output:** `[B, N, 1]`
- **Incision Output:** `[B, N, 1]`

## 5. Implementation Reference (Phase B/C)

### A. Attention Mask (Causal Enforcement)
```python
def upwind_mask(n_nodes: int, device=None) -> torch.Tensor:
    """Lower-triangular mask with diagonal: j <= i."""
    row = torch.arange(n_nodes, device=device).unsqueeze(1)
    col = torch.arange(n_nodes, device=device).unsqueeze(0)
    return (col <= row).float()  # Shape: [N, N], causal only
```

### B. FlowAwareExchangeOperator
Enforces the Upwind-Biased mandate with explicit Transport vs Information separation:
```python
class FlowAwareExchangeOperator(nn.Module):
    def __init__(self, hidden_dim, causal_mask: torch.Tensor):
        super().__init__()
        self.hidden_dim = hidden_dim

        # Transport kernel (upwind, high capacity)
        self.transport_kernel = nn.Sequential(
            nn.Linear(hidden_dim * 2 + 1, hidden_dim),
            nn.GELU(),
            nn.Linear(hidden_dim, hidden_dim)
        )

        # Information kernel (downstream, suppressed low capacity)
        self.info_kernel = nn.Sequential(
            nn.Linear(hidden_dim * 2 + 1, hidden_dim // 4),
            nn.GELU(),
            nn.Linear(hidden_dim // 4, hidden_dim)
        )

        # Learnable suppression on information flow (bounded [0.05, 0.3])
        self.alpha = nn.Parameter(torch.tensor(0.1))
        self.causal_mask = causal_mask

    def forward(self, h: torch.Tensor, velocity: torch.Tensor) -> torch.Tensor:
        """h: [B, N, D], velocity: [B, N]"""
        B, N, D = h.shape

        # Upwind messages (i-1 → i): Transport flow - learns sediment flux closure
        m_upwind = self.transport_kernel(
            torch.cat([h[:, :-1], h[:, 1:], velocity.unsqueeze(1).repeat(1, N-1, 1)], dim=-1)
        )

        # Downstream messages (i+1 → i): Information flow - pressure effects only
        m_info = self.info_kernel(
            torch.cat([h[:, 1:], h[:, :-1], velocity.unsqueeze(1).repeat(1, N-1, 1)], dim=-1)
        )
        m_info = torch.clamp(self.alpha, 0.05, 0.3) * m_info

        # Aggregate with causal mask: zero out any downstream→current leakage
        M = (h @ m_upwind.transpose(-2, -1)) + (h @ m_info.transpose(-2, -1))
        return h + M * self.causal_mask  # [B, N, D]
```

### C. TVD Enforcement Protocol
When $TV(\eta_t) > 10 \times$ baseline:
1. Clamp $\alpha \in [0.15, 0.3]$ for 10 update steps
2. Enforce flux positivity via `softplus` on all transport kernel outputs
3. Log violation for downstream analysis

---

## 6. Staged Training Plan
- **Stage 1 (Pure Advection):** 1D Exner worlds. Supervise against $q_s$ and $\eta$.
- **Stage 2 (Coupled Transitions):** 1D mixed-regime worlds. Supervise against $K_{eff}$ and cover.
- **Stage 3 (Branching Networks):** Add graph-hierarchy and upstream supply memory.
- **Stage 4 (Stress Tests):** Out-of-distribution hydrographs and resolution transfers.
