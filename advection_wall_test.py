import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
import torch.optim as optim
from src.models.physics_model import MassConservingTransformer
from src.data.river_physics import HydrographDataset, get_hydro_dataloader
import json
import numpy as np


def train_and_eval(causal: bool, name: str):
    n_layer = 4
    n_embd = 128

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"\n--- Training {name} Model (Upwind/Causal={causal}) ---")
    print(f"Using device: {device}")

    model = MassConservingTransformer(n_embd=n_embd, n_layer=n_layer, causal=causal)
    model.to(device)
    optimizer = optim.AdamW(model.parameters(), lr=1e-4)

    # Train for 20 epochs on hydrograph data
    dl = get_hydro_dataloader(batch_size=8)

    for epoch in range(20):
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
            f"  Epoch {epoch}, Loss: {np.mean(losses):.6f}, Avg Mass Imbal: {np.mean(mass_errors):.6f}"
        )

    print("Evaluating Long Rollout Stability (100 steps autoregressive)...")

    # We will take 1 sample from a test set and roll it out
    test_ds = HydrographDataset(min_nodes=128, max_nodes=128, num_samples=10, n_steps=100)

    model.eval()
    rollout_errors = []

    with torch.no_grad():
        for i in range(10):
            # data shape [T, N, 5], qs shape [T, N, 1]
            data, _ = test_ds.generate_sample()

            # Start with the first 10 steps as context
            current_state = torch.tensor(data[:10], dtype=torch.float32).unsqueeze(0).to(device)

            # We will autoregressively predict the next 90 steps
            predicted_trajectory = []

            for t in range(10, 100):
                # We need Q for the next step. It's stored in data[t, :, 4]
                Q_next = torch.tensor(data[t : t + 1, :, 4:5], dtype=torch.float32).to(device)

                # Forward pass
                logits, _, _ = model(current_state)

                # The prediction for t is the last element of logits
                next_pred = logits[:, -1:, :, :4]  # [eta, h, u, x]

                # Concatenate with the true Q for the new step
                next_full = torch.cat([next_pred, Q_next.unsqueeze(0)], dim=-1)
                predicted_trajectory.append(next_full.squeeze(0).squeeze(0).cpu().numpy())

                # Slide window
                current_state = torch.cat([current_state[:, 1:, :, :], next_full], dim=1)

            # Compare predicted trajectory to true data
            true_trajectory = data[10:100]
            pred_trajectory = np.array(predicted_trajectory)

            mse = np.mean((pred_trajectory[:, :, 0] - true_trajectory[:, :, 0]) ** 2)
            rollout_errors.append(mse)

    avg_rollout_mse = np.mean(rollout_errors)
    print(f"  Long Rollout MSE ({name}): {avg_rollout_mse:.6f}")
    return avg_rollout_mse


def run_advection_wall_test():
    print("Testing the 'Advection Wall': Symmetric vs Upwind-Biased Attention")

    causal_mse = train_and_eval(causal=True, name="Upwind-Biased")
    symmetric_mse = train_and_eval(causal=False, name="Symmetric")

    results = {
        "Upwind_Biased_MSE": causal_mse,
        "Symmetric_MSE": symmetric_mse,
        "Upwind_Advantage": symmetric_mse / causal_mse,
    }

    os.makedirs("reports", exist_ok=True)
    with open("reports/advection_wall_results.json", "w") as f:
        json.dump(results, f, indent=2)

    print("\n==================================================")
    print(f"RESULTS: Upwind MSE = {causal_mse:.6f} | Symmetric MSE = {symmetric_mse:.6f}")
    print(
        f"Upwind-Biased attention is {results['Upwind_Advantage']:.2f}x better at preserving advection."
    )
    print("==================================================")


if __name__ == "__main__":
    run_advection_wall_test()
