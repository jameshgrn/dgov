import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from src.models.physics_model import MassConservingTransformer
from src.data.river_physics import HydrographDataset
import json
import numpy as np


def evaluate_multiscale(model_path: str, causal: bool, name: str, resolutions: list):
    n_layer = 4
    n_embd = 128
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    print(f"\n--- Multiscale Evaluation for {name} Model ---")
    model = MassConservingTransformer(n_embd=n_embd, n_layer=n_layer, causal=causal)
    model.load_state_dict(torch.load(model_path, map_location=device))
    model.to(device)
    model.eval()

    results = {}

    for res in resolutions:
        print(f"  Testing Resolution: {res} nodes")
        # Generate 10 samples for long rollout (100 steps)
        test_ds = HydrographDataset(min_nodes=res, max_nodes=res, num_samples=10, n_steps=100)

        rollout_mses = []
        mass_errors = []
        crest_errors = []  # Phase lag proxy
        tv_ratios = []  # Sharpness retention

        with torch.no_grad():
            for i in range(10):
                data, _ = test_ds.generate_sample()
                # Start with context window
                current_state = torch.tensor(data[:10], dtype=torch.float32).unsqueeze(0).to(device)

                preds = []
                for t in range(10, 100):
                    Q_next = torch.tensor(data[t : t + 1, :, 4:5], dtype=torch.float32).to(device)
                    logits, _, _ = model(current_state)
                    next_pred = logits[:, -1:, :, :4]
                    next_full = torch.cat([next_pred, Q_next.unsqueeze(0)], dim=-1)
                    preds.append(next_full.squeeze(0).squeeze(0).cpu().numpy())
                    current_state = torch.cat([current_state[:, 1:, :, :], next_full], dim=1)

                preds = np.array(preds)
                true = data[10:100]

                # Metrics
                # 1. MSE
                rollout_mses.append(np.mean((preds[:, :, 0] - true[:, :, 0]) ** 2))

                # 2. Mass Imbalance (Integrated over 90 steps)
                # eta is index 0
                mass_now = np.sum(preds[-1, :, 0])
                mass_start = np.sum(data[9, :, 0])
                mass_errors.append(np.abs(mass_now - mass_start))

                # 3. Crest Migration Error
                crest_pred = np.argmax(preds[-1, :, 0])
                crest_true = np.argmax(true[-1, :, 0])
                crest_errors.append(np.abs(crest_pred - crest_true))

                # 4. Total Variation Retention (Sharpness retention)
                tv_pred = np.sum(np.abs(preds[-1, 1:, 0] - preds[-1, :-1, 0]))
                tv_true = np.sum(np.abs(true[-1, 1:, 0] - true[-1, :-1, 0]))
                tv_ratios.append(tv_pred / max(tv_true, 1e-6))

        results[res] = {
            "mse": float(np.mean(rollout_mses)),
            "mass_error": float(np.mean(mass_errors)),
            "crest_shift_nodes": float(np.mean(crest_errors)),
            "tv_retention": float(np.mean(tv_ratios)),
        }
        print(
            f"    MSE: {results[res]['mse']:.6f}, Mass Error: {results[res]['mass_error']:.6f}, Crest Shift: {results[res]['crest_shift_nodes']:.1f} nodes, TV Retention: {results[res]['tv_retention']:.3f}"
        )

    return results


def run_phase_a():
    resolutions = [128, 256, 512, 1024]

    upwind_results = evaluate_multiscale(
        "checkpoints/upwind_best.pt", causal=True, name="Upwind", resolutions=resolutions
    )
    symmetric_results = evaluate_multiscale(
        "checkpoints/symmetric_best.pt", causal=False, name="Symmetric", resolutions=resolutions
    )

    final_report = {"upwind": upwind_results, "symmetric": symmetric_results}

    os.makedirs("reports", exist_ok=True)
    with open("reports/multiscale_generalization_report.json", "w") as f:
        json.dump(final_report, f, indent=2)

    print("\nPhase A: Multiscale Generalization Test Complete.")
    print("Report saved to reports/multiscale_generalization_report.json")


if __name__ == "__main__":
    run_phase_a()
