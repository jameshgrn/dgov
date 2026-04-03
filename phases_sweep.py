"""
River Morphodynamics Phase Transition Sweeps
Exploring emergent behavior in physics-informed neural operators.

Phase boundaries being tested:
1. Width boundary - minimum embedding for competence (32, 64, 128, 256)
2. Depth boundary - layers needed for temporal coherence (1-6)
3. Resolution independence - grid convergence (64-1024 nodes)
4. Mass conservation error scaling - physical consistency at scale
5. Critical discharge threshold - high-Q robustness

Research questions:
- What is the minimal model capacity to capture Exner morphodynamics?
- How does mass conservation error scale with resolution?
- Are there phase transitions in loss landscapes across hyperparameter regimes?
"""

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from src.data.river_physics import HydrographDataset, collate_hydrograph
import numpy as np
import json
import os
from datetime import datetime
from tqdm import tqdm


class PhaseTransitionTransformer(nn.Module):
    """
    Physics-conserving transformer for river morphodynamics.
    Same architecture but used to benchmark phase transitions.
    """

    def __init__(self, n_embd=128, n_head=4, n_layer=4, dropout=0.0, dt=0.01):
        super().__init__()
        self.dt = dt
        self.node_proj = nn.Linear(5, n_embd)
        self.time_emb = nn.Parameter(torch.zeros(1, 100, 1, n_embd))

        self.blocks = nn.ModuleList(
            [nn.TransformerEncoderLayer(d_model=n_embd, nhead=n_head, dim_feedforward=4*n_embd, dropout=dropout, batch_first=False) for _ in range(n_layer)]
        )

        self.ln_f = nn.LayerNorm(n_embd)
        self.output_proj = nn.Linear(n_embd, 3)

    def forward(self, x, targets=None, mask=None):
        B, T, N, _ = x.size()
        eta_now = x[:, :, :, 0:1]
        x_norm = x[:, :, :, 3:4]
        Q_now = x[:, :, :, 4:5]

        feat = self.node_proj(x)
        feat = feat + self.time_emb[:, :T, :, :]
        feat = feat.view(B * T, N, -1)

        for block in self.blocks:
            feat = block(feat)

        feat = self.ln_f(feat)
        out = self.output_proj(feat).view(B, T, N, 3)

        qs_pred = out[:, :, :, 0:1]
        h_next_pred = out[:, :, :, 1:2]
        u_next_pred = out[:, :, :, 2:3]

        dx = (x_norm[:, :, 1:2, :] - x_norm[:, :, 0:1, :]) * 100.0
        dqs = torch.zeros_like(qs_pred)
        dqs[:, :, 1:, :] = (qs_pred[:, :, 1:, :] - qs_pred[:, :, :-1, :]) / dx
        dqs[:, :, 0, :] = dqs[:, :, 1, :]

        eta_next_pred = eta_now - (self.dt * 0.2) * dqs

        logits = torch.cat([eta_next_pred, h_next_pred, u_next_pred, x_norm, Q_now], dim=-1)

        loss = None
        if targets is not None:
            eta_target = targets[:, :, :, 0:1]
            h_target = targets[:, :, :, 1:2]
            u_target = targets[:, :, :, 2:3]
            qs_target = targets[:, :, :, 5:6]

            loss_eta = nn.functional.mse_loss(eta_next_pred, eta_target)
            loss_qs = nn.functional.mse_loss(qs_pred, qs_target)
            loss_hydro = nn.functional.mse_loss(h_next_pred, h_target) + nn.functional.mse_loss(u_next_pred, u_target)

            if mask is not None:
                m = mask.unsqueeze(-1)
                loss_eta = ((eta_next_pred - eta_target) ** 2 * m).sum() / m.sum()
                loss_qs = ((qs_pred - qs_target) ** 2 * m).sum() / m.sum()

            loss = loss_eta + 0.1 * loss_qs + 0.1 * loss_hydro

        return logits, loss, qs_pred


def compute_mass_violation(qs_history):
    """
    Compute mass conservation violation across time steps.
    Expected: total discharge should be conserved (integral of qs over space).
    """
    # Total flux at each time step
    total_flux = qs_history.sum(dim=1)  # [T,]
    # Relative variance indicates mass loss/gain
    mean_flux = total_flux.mean()
    if mean_flux > 0:
        rel_var = (total_flux - mean_flux).std() / mean_flux
    else:
        rel_var = 0.0
    return rel_var


def run_width_sweep(sweep_id, embd_dims=[32, 64, 128, 256], n_layers=4, epochs=15):
    """Width boundary sweep - find minimum embedding for competent learning."""
    results = {"sweep": "width_boundary", "id": sweep_id}
    
    for embd_dim in embd_dims:
        print(f"\n=== Width sweep: n_embd={embd_dim} ===")
        
        model = PhaseTransitionTransformer(n_embd=embd_dim, n_head=embd_dim//32, n_layer=n_layers)
        device = torch.device("cuda" if torch.cuda.is_available() else "mps")
        model.to(device)
        
        optimizer = optim.AdamW(model.parameters(), lr=1e-4)
        
        dataset = HydrographDataset(min_nodes=64, max_nodes=256)
        dataloader = DataLoader(dataset, batch_size=16, shuffle=True, collate_fn=collate_hydrograph)
        
        best_loss = float('inf')
        loss_curve = []
        
        for epoch in tqdm(range(epochs), desc=f"embd={embd_dim}"):
            model.train()
            total_loss = 0
            for inp, tgt, mask in dataloader:
                inp, tgt, mask = inp.to(device), tgt.to(device), mask.to(device)
                _, loss, _ = model(inp, tgt, mask)
                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                total_loss += loss.item()
            
            avg_loss = total_loss / len(dataloader)
            loss_curve.append(avg_loss)
            if avg_loss < best_loss:
                best_loss = avg_loss
            
            if epoch % 5 == 0 or epoch == epochs - 1:
                print(f"Epoch {epoch}: Loss={avg_loss:.6f}")
        
        results[embd_dim] = {"best_loss": best_loss, "final_loss": loss_curve[-1], "curve": loss_curve}
    
    return results


def run_depth_sweep(sweep_id, layers=[1, 2, 4, 6], n_embd=128, epochs=15):
    """Depth boundary sweep - layer count vs learning dynamics."""
    results = {"sweep": "depth_boundary", "id": sweep_id}
    
    for n_layer in layers:
        print(f"\n=== Depth sweep: n_layer={n_layer} ===")
        
        model = PhaseTransitionTransformer(n_embd=n_embd, n_head=4, n_layer=n_layer)
        device = torch.device("cuda" if torch.cuda.is_available() else "mps")
        model.to(device)
        
        optimizer = optim.AdamW(model.parameters(), lr=1e-4)
        
        dataset = HydrographDataset(min_nodes=64, max_nodes=256)
        dataloader = DataLoader(dataset, batch_size=16, shuffle=True, collate_fn=collate_hydrograph)
        
        best_loss = float('inf')
        loss_curve = []
        
        for epoch in tqdm(range(epochs), desc=f"layers={n_layer}"):
            model.train()
            total_loss = 0
            for inp, tgt, mask in dataloader:
                inp, tgt, mask = inp.to(device), tgt.to(device), mask.to(device)
                _, loss, _ = model(inp, tgt, mask)
                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                total_loss += loss.item()
            
            avg_loss = total_loss / len(dataloader)
            loss_curve.append(avg_loss)
            if avg_loss < best_loss:
                best_loss = avg_loss
            
            if epoch % 5 == 0 or epoch == epochs - 1:
                print(f"Epoch {epoch}: Loss={avg_loss:.6f}")
        
        results[n_layer] = {"best_loss": best_loss, "final_loss": loss_curve[-1], "curve": loss_curve}
    
    return results


def run_resolution_sweep(sweep_id, resolutions=[64, 128, 256, 512], n_embd=128, epochs=10):
    """Resolution independence test - grid convergence study."""
    results = {"sweep": "resolution_independence", "id": sweep_id}
    
    for n_nodes in resolutions:
        print(f"\n=== Resolution sweep: n_nodes={n_nodes} ===")
        
        # Custom dataset with fixed resolution
        class FixedResDataset(HydrographDataset):
            def __init__(self, res, **kwargs):
                super().__init__(**kwargs)
                self.fixed_res = res
            
            def generate_sample(self):
                return super().generate_sample()  # Override to use fixed resolution
        
        dataset = FixedResDataset(res=n_nodes, min_nodes=n_nodes, max_nodes=n_nodes)
        
        model = PhaseTransitionTransformer(n_embd=n_embd, n_head=4, n_layer=2)
        device = torch.device("cuda" if torch.cuda.is_available() else "mps")
        model.to(device)
        
        optimizer = optim.AdamW(model.parameters(), lr=1e-4)
        
        # Smaller batch size for higher resolutions to fit memory
        batch_size = 8 if n_nodes > 256 else 16
        
        dataloader = DataLoader(dataset, batch_size=batch_size, shuffle=True, collate_fn=collate_hydrograph)
        
        best_loss = float('inf')
        loss_curve = []
        
        for epoch in tqdm(range(epochs), desc=f"res={n_nodes}"):
            model.train()
            total_loss = 0
            for inp, tgt, mask in dataloader:
                inp, tgt, mask = inp.to(device), tgt.to(device), mask.to(device)
                _, loss, _ = model(inp, tgt, mask)
                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                total_loss += loss.item()
            
            avg_loss = total_loss / len(dataloader)
            loss_curve.append(avg_loss)
            if avg_loss < best_loss:
                best_loss = avg_loss
            
            if epoch % 3 == 0 or epoch == epochs - 1:
                print(f"Epoch {epoch}: Loss={avg_loss:.6f}")
        
        results[n_nodes] = {"best_loss": best_loss, "final_loss": loss_curve[-1], "curve": loss_curve}
    
    return results


def run_mass_conservation_test(sweep_id, n_runs=20, epochs=5):
    """Test mass conservation error scaling across models."""
    results = {"sweep": "mass_conservation", "id": sweep_id}
    
    model = PhaseTransitionTransformer(n_embd=128, n_head=4, n_layer=2)
    device = torch.device("cuda" if torch.cuda.is_available() else "mps")
    model.to(device)
    
    optimizer = optim.AdamW(model.parameters(), lr=1e-4)
    
    # Generate training data
    dataset = HydrographDataset(min_nodes=64, max_nodes=128)
    dataloader = DataLoader(dataset, batch_size=16, shuffle=True, collate_fn=collate_hydrograph)
    
    for epoch in tqdm(range(epochs), desc="Training"):
        model.train()
        for inp, tgt, mask in dataloader:
            inp, tgt, mask = inp.to(device), tgt.to(device), mask.to(device)
            _, loss, _ = model(inp, tgt, mask)
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
    
    # Evaluation phase - measure mass conservation
    print("\n=== Mass Conservation Evaluation ===")
    violations = []
    for run in range(n_runs):
        model.eval()
        with torch.no_grad():
            inp, tgt, mask = next(iter(dataloader))
            inp, tgt, mask = inp.to(device), tgt.to(device), mask.to(device)
            
            _, _, qs_pred = model(inp, None, None)
            # Convert back to physical units
            qs_physical = (qs_pred.cpu().numpy() * 10.0).squeeze()
            
            violation = compute_mass_violation(qs_physical)
            violations.append(violation)
    
    results["mean_violation"] = np.mean(violations)
    results["std_violation"] = np.std(violations)
    results["violations"] = violations
    
    return results


def save_results(results, base_path="reports"):
    """Save sweep results to JSON."""
    os.makedirs(base_path, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{base_path}/phase_sweep_{timestamp}.json"
    
    with open(filename, "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\nResults saved to {filename}")
    return filename


def main():
    """Run all phase transition sweeps."""
    all_results = {}
    
    # Width boundary
    print("\n" + "="*60)
    print("RUNNING WIDTH BOUNDARY SWEEP")
    print("="*60)
    width_results = run_width_sweep("width_v1", embd_dims=[32, 64, 128, 256], epochs=10)
    all_results.update(width_results)
    
    # Depth boundary  
    print("\n" + "="*60)
    print("RUNNING DEPTH BOUNDARY SWEEP")
    print("="*60)
    depth_results = run_depth_sweep("depth_v1", layers=[1, 2, 4, 6], epochs=10)
    all_results.update(depth_results)
    
    # Resolution independence (subset for speed)
    print("\n" + "="*60)
    print("RUNNING RESOLUTION INDEPENDENCE SWEEP")
    print("="*60)
    res_results = run_resolution_sweep("res_v1", resolutions=[64, 128, 256], epochs=5)
    all_results.update(res_results)
    
    # Mass conservation
    print("\n" + "="*60)
    print("RUNNING MASS CONSERVATION TEST")
    print("="*60)
    mass_results = run_mass_conservation_test("mass_v1", n_runs=20, epochs=5)
    all_results.update(mass_results)
    
    # Save all results
    save_results(all_results)
    
    # Print summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    print(f"Width sweep best: {min(width_results[32:].values(), key=lambda x: x['best_loss'])}")
    print(f"Depth sweep best: {min(depth_results.values(), key=lambda x: x['best_loss'])}")
    print(f"Resolution convergence: {[(k, v['final_loss']) for k, v in res_results.items()]}")
    print(f"Mass conservation error: {mass_results['mean_violation']:.4f} (+/-{mass_results['std_violation']:.4f})")


if __name__ == "__main__":
    main()