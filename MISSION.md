# Mission: Neural Morphodynamics (Remote Cluster Ops)

## Objective
Discover emergent behavior and efficient neural operators for 1D morphodynamics (Exner Equation) across multiple scales and non-stationary hydrographs.

## Research Strategy (The Ladder)
- **Phase A: Multiscale Generalization**: Prove the model works on high-resolution grids (1024+ nodes) using weights trained on low resolutions. Use transport-native metrics (crest migration, mass balance).
- **Phase B: Stochastic Event Trains**: Test intermittency and memory using Poisson pulse arrivals and variable hydrograph sequences. (Capability implemented in data generator).
- **Phase C: Hydrodynamic Feedback**: Implement two-way coupling where bed changes dynamically update the water surface.

## Architecture
- **Inductive Bias**: Conservative (flux-first) and Upwind-Biased spatial attention.
- **Generalization**: Grid-agnostic point-wise tokenization.
