import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import torch.optim as optim
from src.models.physics_model import MassConservingTransformer
from src.data.river_physics import get_hydro_dataloader
import json
import numpy as np


def run_phase_b():
    n_layer = 4
    n_embd = 128

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print("\n--- Phase B: Stochastic Event Trains ---")
    print(f"Using device: {device}")

    # Use the winner from Phase A: Upwind-Biased
    model = MassConservingTransformer(n_embd=n_embd, n_layer=n_layer, causal=True)
    model.to(device)
    optimizer = optim.AdamW(model.parameters(), lr=1e-4)

    # We will use the 'stochastic' mode implicitly through the updated generator
    dl = get_hydro_dataloader(batch_size=16)

    results = []

    for epoch in range(50):  # 50 epochs for Phase B
        model.train()
        losses = []
        for x, y, mask in dl:
            x, y, mask = x.to(device), y.to(device), mask.to(device)
            logits, loss, _ = model(x, y, mask=mask)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            losses.append(loss.item())

        avg_loss = np.mean(losses)
        print(f"  Epoch {epoch}, Loss: {avg_loss:.6f}")

        results.append({"epoch": epoch, "loss": float(avg_loss)})

    os.makedirs("reports", exist_ok=True)
    with open("reports/phase_b_stochastic_results.json", "w") as f:
        json.dump(results, f, indent=2)

    os.makedirs("checkpoints", exist_ok=True)
    torch.save(model.state_dict(), "checkpoints/stochastic_best.pt")

    print("\nPhase B: Stochastic Event Training Complete.")
    print("Checkpoint saved to checkpoints/stochastic_best.pt")


if __name__ == "__main__":
    run_phase_b()
