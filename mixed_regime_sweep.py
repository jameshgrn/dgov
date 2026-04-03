import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import torch.optim as optim
from src.models.physics_model import NMO_V3
from src.data.river_physics import MixedRegimeRiver1D, get_mixed_dataloader
import json
import numpy as np


def run_mixed_regime_sweep():
    n_layer = 4
    n_embd = 128
    results = []

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Using device: {device}")

    print("\n--- Phase B: Mixed-Regime Training (Incision + Transport) ---")
    model = NMO_V3(n_embd=n_embd, n_layer=n_layer, causal=True)
    model.to(device)
    optimizer = optim.AdamW(model.parameters(), lr=1e-4)

    dl = get_mixed_dataloader(batch_size=8)

    best_loss = float("inf")
    os.makedirs("checkpoints", exist_ok=True)
    os.makedirs("reports", exist_ok=True)

    for epoch in range(50):
        model.train()
        losses = []
        for x, y, mask in dl:
            x, y, mask = x.to(device), y.to(device), mask.to(device)
            _, loss, _, _ = model(x, y, mask=mask)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            losses.append(loss.item())

        avg_loss = np.mean(losses)
        print(f"  Epoch {epoch}, Loss: {avg_loss:.6f}")

        if avg_loss < best_loss:
            best_loss = avg_loss
            torch.save(model.state_dict(), "checkpoints/nmo_v3_mixed_best.pt")

    print(f"\nTraining Complete. Best Loss: {best_loss:.6f}")

    # Evaluation on variable resolution
    print("Evaluating on 128 and 512 node grids...")
    model.eval()
    eval_results = {}
    for res in [128, 512]:
        test_ds = MixedRegimeRiver1D(min_nodes=res, max_nodes=res, num_samples=20)
        from src.data.river_physics import collate_mixed
        from torch.utils.data import DataLoader

        test_dl = DataLoader(test_ds, batch_size=4, collate_fn=collate_mixed)

        test_losses = []
        with torch.no_grad():
            for x, y, mask in test_dl:
                x, y, mask = x.to(device), y.to(device), mask.to(device)
                _, loss, _, _ = model(x, y, mask=mask)
                test_losses.append(loss.item())
        eval_results[res] = float(np.mean(test_losses))
        print(f"  Resolution {res} Node MSE: {eval_results[res]:.6f}")

    with open("reports/mixed_regime_results.json", "w") as f:
        json.dump(eval_results, f, indent=2)


if __name__ == "__main__":
    run_mixed_regime_sweep()
