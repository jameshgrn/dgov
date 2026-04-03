import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import torch.optim as optim
from src.models.physics_model import MassConservingTransformer
from src.data.river_physics import HydrographDataset, get_hydro_dataloader
import json
import numpy as np


def run_hydrograph_sweep():
    n_layer = 4
    n_embd = 128
    results = []

    # We'll focus on 256 nodes for this non-stationary test
    test_resolutions = [256]

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    print("\nTraining Hydrograph-Aware Coupled Model (Q variable)")
    model = MassConservingTransformer(n_embd=n_embd, n_layer=n_layer)
    model.to(device)
    optimizer = optim.AdamW(model.parameters(), lr=1e-4)

    dl = get_hydro_dataloader(batch_size=8)

    for epoch in range(30):
        model.train()
        losses = []
        mass_errors = []
        for x, y, mask in dl:
            x, y, mask = x.to(device), y.to(device), mask.to(device)
            logits, loss, qs_pred = model(x, y, mask=mask)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            losses.append(loss.item())

            with torch.no_grad():
                eta_now = x[:, -1, :, 0]
                eta_next = logits[:, -1, :, 0]
                m = mask[:, -1]
                m_error = torch.abs((eta_next * m).sum() - (eta_now * m).sum())
                mass_errors.append(m_error.item())

        print(
            f"  Epoch {epoch}, Loss: {np.mean(losses):.6f}, Avg Mass Imbalance: {np.mean(mass_errors):.6f}"
        )

    # Evaluation on unseen hydrographs
    res_perf = {}
    for res in test_resolutions:
        # Generate a smaller test set with specific res
        test_ds = HydrographDataset(min_nodes=res, max_nodes=res, num_samples=50)
        from src.data.river_physics import collate_hydrograph
        from torch.utils.data import DataLoader

        test_dl = DataLoader(test_ds, batch_size=8, shuffle=False, collate_fn=collate_hydrograph)

        model.eval()
        test_losses = []
        with torch.no_grad():
            for x, y, mask in test_dl:
                x, y, mask = x.to(device), y.to(device), mask.to(device)
                _, loss, _ = model(x, y, mask=mask)
                test_losses.append(loss.item())
        res_perf[res] = np.mean(test_losses)
        print(f"  Resolution {res} (Non-stationary) MSE: {res_perf[res]:.6f}")

    results.append(
        {
            "config": "hydrograph_v1",
            "res_perf": res_perf,
            "final_mse": np.mean(list(res_perf.values())),
        }
    )

    os.makedirs("reports", exist_ok=True)
    with open("reports/hydrograph_exner_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nSweep complete. Report saved to reports/hydrograph_exner_results.json")


if __name__ == "__main__":
    run_hydrograph_sweep()
