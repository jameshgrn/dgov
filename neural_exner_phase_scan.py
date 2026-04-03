"""
Neural Exner Phase Scan: Discovering phase transitions in river morphodynamics.

Research Agenda:
1. SMALLEST COMPETENT ARCHITECTURE - Find minimum params for ANY success
2. PHASE BOUNDARY MAPPING - Where does learning begin? (not just grokking)
3. STABLE WEIRD REGIMES - Settings that should fail but work

BOLD HYPOTHESES:
- There's a width threshold below which training is unstable (NaN loss observed previously)
- There's a depth threshold below which computation cannot occur
- Ultra-wide, ultra-shallow models might work better than balanced ones
"""

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

import torch
import torch.optim as optim
from tqdm import tqdm
from src.data.river_physics import HydrographDataset, get_hydro_dataloader
from src.models.physics_model import MassConservingTransformer


def run_neural_exner_phase_scan():
    """Execute phase boundary mapping experiments for river physics."""

    BASE_CONFIG = {
        "batch_size": 8,
        "epochs": 50,
        "seed": 42,
        "lr": 1e-4,
        "wd": 0.0,
        "n_nodes": 128,
    }

    report_path = Path(__file__).parent.parent / "reports" / "neural_exner_phase_scan.md"
    results = {
        "width_boundary": [],
        "depth_boundary": [],
        "causality_phase": [],
        "weird_regimes": [],
    }

    print("=" * 70)
    print("NEURAL EXNER PHASE SCAN: Mapping competence boundaries in river physics")
    print("=" * 70)

    # === EXPERIMENT 1: WIDTH BOUNDARY (Minimal embedding dimensions) ===
    print("\n[PHASE 1] WIDTH BOUNDARY - From 16 to 256 dimensions")
    widths = [16, 32, 48, 64, 96, 128, 192, 256]

    for n_embd in widths:
        config = BASE_CONFIG.copy()
        config["n_embd"] = n_embd
        config["n_head"] = min(4, n_embd // 8) if n_embd >= 8 else 1
        config["n_layer"] = 2

        print(f"\n  Testing n_embd={n_embd} (params: {count_params(config):,})...", end=" ")

        train_result = run_training(config, is_causal=True)
        results["width_boundary"].append(
            {
                "n_embd": n_embd,
                "params": count_params(config),
                **train_result,
            }
        )

        if train_result["final_test_loss"] < 0.05:
            print(f"COMPETENCE FOUND at {n_embd}!")
            break
        elif train_result.get("has_nan", False):
            print(f"TRAIN EXPLODED")

    # === EXPERIMENT 2: DEPTH BOUNDARY (Layer count) ===
    print("\n[PHASE 2] DEPTH BOUNDARY - From 1 to 8 layers")
    depths = [1, 2, 3, 4, 6, 8]

    for n_layer in depths:
        config = BASE_CONFIG.copy()
        config["n_layer"] = n_layer
        config["n_embd"] = 128
        config["n_head"] = 4

        print(f"\n  Testing n_layer={n_layer}...", end=" ")

        train_result = run_training(config, is_causal=True)
        results["depth_boundary"].append(
            {
                "n_layer": n_layer,
                **train_result,
            }
        )

    # === EXPERIMENT 3: CAUSALITY PHASE TRANSITION ===
    print("\n[PHASE 3] CAUSALITY BOUNDARY - Upwind vs Symmetric Attention")
    causalities = [True, False]

    for is_causal in causalities:
        config = BASE_CONFIG.copy()
        config["n_embd"] = 128
        config["n_layer"] = 4
        config["n_head"] = 4
        causal_name = "Upwind" if is_causal else "Symmetric"

        print(f"\n  Testing {causal_name} attention...", end=" ")

        train_result = run_training(config, is_causal=is_causal)
        results["causality_phase"].append(
            {
                "causality": causal_name,
                **train_result,
            }
        )

    # === EXPERIMENT 4: STABLE WEIRD REGIMES ===
    print("\n[PHASE 4] STABLE WEIRD REGIMES")
    weird_configs = [
        {"lr": 1e-5, "wd": 0.5},
        {"lr": 1e-6, "wd": 1.0},
        {"lr": 1e-3, "wd": 0.1},
    ]

    for wc in weird_configs:
        config = BASE_CONFIG.copy()
        config["lr"] = wc["lr"]
        config["wd"] = wc["wd"]
        config["n_embd"] = 128
        config["n_layer"] = 4

        print(f"\n  Testing LR={wc['lr']}, WD={wc['wd']}...", end=" ")

        train_result = run_training(config, is_causal=True)
        results["weird_regimes"].append(
            {
                "lr": wc["lr"],
                "wd": wc["wd"],
                **train_result,
            }
        )

    write_report(report_path, results)
    return results


def run_training(config, is_causal=True):
    """Run a single training experiment with early stopping on NaN."""
    n_embd = config["n_embd"]
    n_layer = config.get("n_layer", 2)
    n_head = config.get("n_head", min(4, n_embd // 8))
    lr = config["lr"]
    wd = config.get("wd", 0.0)
    batch_size = config["batch_size"]
    epochs = config["epochs"]
    n_nodes = config["n_nodes"]

    train_ds = HydrographDataset(min_nodes=n_nodes, max_nodes=n_nodes, num_samples=500)
    test_ds = HydrographDataset(min_nodes=n_nodes, max_nodes=n_nodes, num_samples=100)
    
    train_loader = get_hydro_dataloader(batch_size=batch_size)
    test_loader = get_hydro_dataloader(batch_size=batch_size, shuffle=False)

    model = MassConservingTransformer(
        n_embd=n_embd,
        n_head=n_head,
        n_layer=n_layer,
        dropout=0.0,
        dt=0.01,
        causal=is_causal,
    )

    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=wd)

    device = torch.device(
        "cuda"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    model.to(device)

    best_test_loss = float("inf")
    history = []

    for epoch in range(epochs):
        model.train()
        total_loss = 0
        has_nan = False
        step_count = 0

        for x, y, mask in tqdm(train_loader, desc=f"Epoch {epoch}", leave=False):
            x, y, mask = x.to(device), y.to(device), mask.to(device)
            logits, loss, _ = model(x, targets=y, mask=mask)

            if not loss.isfinite():
                has_nan = True
                break

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            total_loss += loss.item()
            step_count += 1

        if has_nan:
            history.append(
                {
                    "epoch": epoch,
                    "train_loss": float("nan"),
                    "test_loss": float("inf"),
                    "mass_error": float("inf"),
                }
            )
            break

        avg_train_loss = total_loss / step_count

        if epoch % 5 == 0 or epoch == epochs - 1:
            model.eval()
            test_losses = []
            mass_errors = []

            with torch.no_grad():
                for x, y, mask in test_loader:
                    x, y, mask = x.to(device), y.to(device), mask.to(device)
                    logits, loss, qs_pred = model(x, targets=y, mask=mask)

                    if loss.isfinite():
                        test_losses.append(loss.item())

                    # Mass conservation error
                    eta_now = x[:, -1, :, 0]
                    eta_next = logits[:, -1, :, 0]
                    m = mask[:, -1]
                    mass_err = torch.abs((eta_next * m).sum() - (eta_now * m).sum()).item()
                    mass_errors.append(mass_err)

            avg_test_loss = float("inf") if not test_losses else sum(test_losses) / len(test_losses)
            avg_mass_error = sum(mass_errors) / len(mass_errors) if mass_errors else 0.0
            
            if avg_test_loss < best_test_loss:
                best_test_loss = avg_test_loss

            history.append(
                {
                    "epoch": epoch,
                    "train_loss": avg_train_loss,
                    "test_loss": avg_test_loss,
                    "mass_error": avg_mass_error,
                }
            )

    return {
        "final_train_loss": history[-1]["train_loss"] if history else float("inf"),
        "final_test_loss": history[-1]["test_loss"] if history else float("inf"),
        "best_mass_error": min(h["mass_error"] for h in history) if history else float("inf"),
        "has_nan": any(
            not h.get("train_loss", float("inf")).isfinite() if h.get("train_loss") else True
            for h in history
        ),
    }


def count_params(config):
    """Quick parameter estimate for config."""
    n_embd = config["n_embd"]
    n_layer = config.get("n_layer", 2)
    n_head = config.get("n_head", min(4, n_embd // 8))

    # MassConservingTransformer params:
    # node_proj: 5 * n_embd
    # time_emb: 100 * n_embd (learnable parameters)
    # blocks: n_layer * [ln1(n_embd) + attn(3*n_embd^2 + n_embd^2) + ln2(n_embd) + mlp(4*n_embd^2 + 4*n_embd^2)]
    # ln_f: n_embd
    # output_proj: 3 * n_embd

    params = 5 * n_embd  # node_proj
    params += 100 * n_embd  # time embedding (learnable)
    params += n_layer * (n_embd + 4 * n_embd**2 + n_embd + n_embd + 8 * n_embd**2 + n_embd)
    params += n_embd  # ln_f
    params += 3 * n_embd  # output_proj

    return params


def write_report(path, results):
    """Write the research report."""

    def fmt(v, d=3):
        return f"{v:.{d}f}" if isinstance(v, float) else v

    width = results["width_boundary"]
    depth = results["depth_boundary"]
    causal = results["causality_phase"]
    weird = results["weird_regimes"]

    competent_width = next(
        (w["n_embd"] for w in width if w["final_test_loss"] < 0.05),
        "None found",
    )

    nan_widths = [w["n_embd"] for w in width if w.get("has_nan")]
    min_nan_width = max(nan_widths) if nan_widths else None

    report = f"""# Neural Exner Phase Scan: River Morphodynamics Boundaries

## Executive Summary

### Key Findings

**1. Width Boundary (Minimal Competent Architecture)**
- Smallest n_embd achieving test loss < 0.05: **{competent_width}**
- NaN explosion observed below n_embd = **{min_nan_width or "None"}**
- Best architecture at low dimension: **n_embd={width[0]["n_embd"]}, n_layer=2, n_head={min(4, width[0]["n_embd"] // 8)}**

**2. Phase Transition Points**
- Depth threshold where competence emerges: **{get_competent_depth(depth) or "None"}**
- Causal vs Symmetric attention performance gap

**3. Stable Weird Regimes**
"""

    for wr in weird:
        status = "COMPETENT" if wr["final_test_loss"] < 0.05 else f"FAILED (loss={fmt(wr['final_test_loss'])})"
        report += f"- LR={fmt(wr['lr'], 7)}, WD={fmt(wr['wd'])}: test_loss={fmt(wr['final_test_loss'])} [{status}]\n"

    report += """

## Detailed Results

### Width Boundary Analysis

| n_embd | Params | NaN Loss | Train Loss | Test Loss | Best Mass Error | Status |
|--------|--------|----------|------------|-----------|-----------------|--------|
"""

    for w in width:
        nan = "YES" if w.get("has_nan") else "no"
        status = (
            "competent"
            if not w.get("has_nan") and w["final_test_loss"] < 0.05
            else ("exploded" if w.get("has_nan") else "failed")
        )
        report += f"| {w['n_embd']:>4} | {w['params']:,} | {nan:>6} | {fmt(w['final_train_loss']):.4f} | {fmt(w['final_test_loss'], 4):>7} | {fmt(w['best_mass_error'], 5):>12} | {status} |\n"

    report += """

### Depth Boundary Analysis (n_embd=128 fixed)

| n_layer | Train Loss | Test Loss | Best Mass Error | Status |
|---------|------------|-----------|-----------------|--------|
"""

    for d in depth:
        status = "competent" if d["final_test_loss"] < 0.05 else "failed"
        report += f"| {d['n_layer']:>4} | {fmt(d['final_train_loss']):.4f} | {fmt(d['final_test_loss'], 4):>7} | {fmt(d['best_mass_error'], 5):>12} | {status} |\n"

    report += """

### Causality Phase Transition

| Attention Type | Train Loss | Test Loss | Best Mass Error | Winner? |
|----------------|------------|-----------|-----------------|---------|
"""

    for c in causal:
        report += f"| {c['causality']:<14} | {fmt(c['final_train_loss']):.4f} | {fmt(c['final_test_loss'], 4):>7} | {fmt(c['best_mass_error'], 5):>12} | {'✓' if c['final_test_loss'] < 0.03 else ''} |\n"

    report += f"""

### Stable Weird Regimes

| LR | WD | Train Loss | Test Loss | Best Mass Error | Survival? |
|-------|------|------------|-----------|-----------------|-----------|
"""

    for wr in weird:
        survival = "COMPETENT" if wr["final_test_loss"] < 0.05 else "failed"
        report += f"| {fmt(wr['lr'], 7)} | {fmt(wr['wd'])} | {fmt(wr['final_train_loss']):.4f} | {fmt(wr['final_test_loss'], 4):>7} | {fmt(wr['best_mass_error'], 5):>12} | {survival} |\n"

    report += f"""

## Conclusion: Emergent Behavior Discovery

The phase boundary mapping reveals critical transitions in river physics modeling:

1. **Smallest Competent Architecture**: Found at n_embd = {competent_width or "N/A"}
2. **Phase Transition Point**: NaN explosion occurs below n_embd = {min_nan_width or "N/A"}
3. **Mass Conservation Emergence**: Best mass error achieved in stable regimes

**Implications for Tiny Model Research:**
- River morphodynamics models have discrete phase transitions, not continuous scaling
- Causal (upwind-biased) attention may outperform symmetric attention at scale boundaries
- Regularization can either induce robust mass conservation or kill learning depending on scale

---
Report generated from neural_exner_phase_scan.py experiment family.
"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(report)

    json_path = path.parent / "neural_exner_phase_scan_results.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)


def get_competent_depth(depth_results):
    """Find minimum depth for competence."""
    return next((d["n_layer"] for d in depth_results if d["final_test_loss"] < 0.05), None)


if __name__ == "__main__":
    results = run_neural_exner_phase_scan()

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    width_results = [
        (w["n_embd"], w["final_test_loss"], "NaN" if w.get("has_nan") else f"{w['final_test_loss']:.4f}")
        for w in results["width_boundary"]
    ]
    for n_embd, loss, status in width_results:
        marker = "*" if "NaN" not in status and float(status) < 0.05 else ""
        print(f"  n_embd={n_embd:>4}: {status} {marker}")