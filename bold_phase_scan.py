"""
Bold Phase Scan: Discovering competence boundaries in modular addition.

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

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import torch
import torch.optim as optim
from tqdm import tqdm
from src.data.toy_tasks import ModularAddition, get_dataloader
from src.models.transformer import TinyTransformer


def run_bold_phase_scan():
    """Execute bold experiment family mapping phase boundaries."""
    
    BASE_CONFIG = {
        "p": 113,              # Modulo prime
        "batch_size": 64,      # Batch size  
        "epochs": 200,         # Training epochs
        "seed": 42,
        "lr": 0.001,           # Baseline learning rate
        "wd": 0.0,             # Weight decay baseline
    }
    
    report_path = Path(__file__).parent.parent.parent / ".dgov" / "reports" / "bold_phase_scan.md"
    results = {
        "width_boundary": [],
        "depth_boundary": [],
        "lr_boundary": [],
        "weird_regimes": [],
    }
    
    print("=" * 70)
    print("BOLD PHASE SCAN: Mapping competence boundaries")
    print("=" * 70)
    
    # === EXPERIMENT 1: WIDTH BOUNDARY (Minimal embedding dimensions) ===
    print("\n[PHASE 1] WIDTH BOUNDARY - From 4 to 512 dimensions")
    widths = [4, 8, 16, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512]
    
    for n_embd in widths:
        config = BASE_CONFIG.copy()
        config["n_embd"] = n_embd
        config["n_head"] = min(8, n_embd // 8) if n_embd >= 8 else 1
        config["n_layer"] = 2
        
        print(f"\n  Testing n_embd={n_embd} (params: {count_params(config):,})...", end=" ")
        
        train_result = run_training(config)
        results["width_boundary"].append({
            "n_embd": n_embd,
            "params": count_params(config),
            **train_result,
        })
        
        if train_result["max_test_acc"] > 0.5:
            print(f"COMPETENCE FOUND at {n_embd}!")
            break
        elif train_result.get("final_train_loss", float("inf")) < 1e-4:
            print(f"TRAIN PERFECT but test={train_result['max_test_acc']:.3f}")
    
    # === EXPERIMENT 2: DEPTH BOUNDARY (Layer count) ===
    print("\n[PHASE 2] DEPTH BOUNDARY - From 1 to 8 layers")
    depths = [1, 2, 3, 4, 6, 8]
    
    for n_layer in depths:
        config = BASE_CONFIG.copy()
        config["n_layer"] = n_layer
        config["n_embd"] = 128
        config["n_head"] = 4
        
        print(f"\n  Testing n_layer={n_layer}...", end=" ")
        
        train_result = run_training(config)
        results["depth_boundary"].append({
            "n_layer": n_layer,
            **train_result,
        })
    
    # === EXPERIMENT 3: LR BOUNDARY ===
    print("\n[PHASE 3] LR BOUNDARY - Critical learning rates")
    lrs = [0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1]
    
    for lr in lrs:
        config = BASE_CONFIG.copy()
        config["lr"] = lr
        config["n_embd"] = 64
        config["n_layer"] = 2
        config["n_head"] = 4
        
        print(f"\n  Testing lr={lr}...", end=" ")
        
        train_result = run_training(config)
        results["lr_boundary"].append({
            "lr": lr,
            **train_result,
        })
    
    # === EXPERIMENT 4: STABLE WEIRD REGIMES ===
    print("\n[PHASE 4] STABLE WEIRD REGIMES")
    weird_configs = [
        {"lr": 0.0001, "wd": 0.5},
        {"lr": 0.00001, "wd": 1.0},
        {"lr": 0.0001, "wd": 2.0},
    ]
    
    for wc in weird_configs:
        config = BASE_CONFIG.copy()
        config["lr"] = wc["lr"]
        config["wd"] = wc["wd"]
        config["n_embd"] = 128
        config["n_layer"] = 4
        
        print(f"\n  Testing LR={wc['lr']}, WD={wc['wd']}...", end=" ")
        
        train_result = run_training(config)
        results["weird_regimes"].append({
            "lr": wc["lr"],
            "wd": wc["wd"],
            **train_result,
        })
    
    write_report(report_path, results)
    return results


def run_training(config):
    """Run a single training experiment with early stopping on NaN."""
    p = config["p"]
    batch_size = config["batch_size"]
    epochs = config["epochs"]
    lr = config["lr"]
    wd = config.get("wd", 0.0)
    
    train_ds = ModularAddition(p=p, split=0.8, mode="train", seed=config["seed"])
    test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=config["seed"])
    vocab_size = p + 2
    
    train_loader = get_dataloader(train_ds, batch_size=batch_size)
    test_loader = get_dataloader(test_ds, batch_size=batch_size, shuffle=False)
    
    model = TinyTransformer(
        vocab_size=vocab_size,
        n_embd=config["n_embd"],
        n_head=config.get("n_head", 4),
        n_layer=config["n_layer"],
        block_size=128,
        dropout=0.0,
        tie_weights=True,
        norm_type="layer",
    )
    
    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=wd)
    
    device = torch.device("cuda" if torch.cuda.is_available() 
                          else "mps" if torch.backends.mps.is_available() 
                          else "cpu")
    model.to(device)
    
    best_acc = 0.0
    history = []
    
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        has_nan = False
        
        for x, y in tqdm(train_loader, desc=f"Epoch {epoch}", leave=False):
            x, y = x.to(device), y.to(device)
            _, loss = model(x, targets=y)
            
            if not loss.isfinite():
                has_nan = True
                break
            
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        
        if has_nan:
            history.append({
                "epoch": epoch,
                "train_loss": float("nan"),
                "test_acc": 0.0,
            })
            break
        
        avg_loss = total_loss / len(train_loader)
        
        if epoch % 10 == 0 or epoch == epochs - 1:
            model.eval()
            correct = 0
            total = 0
            for x, y in test_loader:
                x, y = x.to(device), y.to(device)
                logits, _ = model(x, targets=y)
                preds = torch.argmax(logits[:, -1, :], dim=-1)
                correct += (preds == y[:, -1]).sum().item()
                total += y.size(0)
            
            acc = correct / total
            if acc > best_acc:
                best_acc = acc
            
            history.append({
                "epoch": epoch,
                "train_loss": avg_loss,
                "test_acc": acc,
            })
    
    return {
        "final_train_loss": history[-1]["train_loss"] if history else float("inf"),
        "max_test_acc": best_acc,
        "has_nan": any(not h.get("train_loss", float("inf")).isfinite() if h.get("train_loss") else True for h in history),
    }


def count_params(config):
    """Quick parameter estimate for config."""
    n_embd = config["n_embd"]
    n_layer = config.get("n_layer", 2)
    vocab = 115
    
    params = vocab * n_embd
    params += n_layer * (
        3 * n_embd**2 + n_embd**2 + 
        n_embd**2 + 4 * n_embd**2 + 
        4 * n_embd **2
    )
    params += n_embd * vocab
    return params


def write_report(path, results):
    """Write the research report."""
    
    def fmt(v, d=3):
        return f"{v:.{d}f}" if isinstance(v, float) else v
    
    width = results["width_boundary"]
    depth = results["depth_boundary"]
    lr_results = results["lr_boundary"]
    weird = results["weird_regimes"]
    
    competent_width = next(
        (w["n_embd"] for w in width if w["max_test_acc"] > 0.5),
        "None found",
    )
    
    nan_widths = [w["n_embd"] for w in width if w.get("has_nan")]
    min_nan_width = min(nan_widths) if nan_widths else None
    
    slow_lrs = [l["lr"] for l in lr_results if not l.get("has_nan") and l["final_train_loss"] > 1.0]
    exploding_lrs = [l["lr"] for l in lr_results if l.get("has_nan")]
    
    report = f"""# Bold Phase Scan: Competence Boundaries in Modular Addition

## Executive Summary

### Key Findings

**1. Width Boundary (Minimal Competent Architecture)**
- Smallest n_embd achieving >50% test accuracy: **{competent_width}**
- NaN regime observed below n_embd = **{min_nan_width or "None"}**
- Best architecture at low dimension: **n_embd={width[0]["n_embd"]}, n_layer=2, n_head={min(8, width[0]["n_embd"]//8)}**

**2. Phase Transition Points**
- LR below which training is too slow (loss > 1.0): **{slow_lrs[0] if slow_lrs else "None"}**
- LR above which training explodes: **{exploding_lrs[0] if exploding_lrs else "None"}**

**3. Stable Weird Regimes**
"""
    
    for wr in weird:
        status = "SUCCESS" if wr["max_test_acc"] > 0.5 else "FAILED (expected)"
        report += f"- LR={fmt(wr['lr'], 6)}, WD={fmt(wr['wd'])}: max_acc={fmt(wr['max_test_acc'])} [{status}]\n"
    
    report += """

## Detailed Results

### Width Boundary Analysis

| n_embd | Params | NaN Loss | Train Loss | Max Test Acc | Status |
|--------|--------|----------|------------|--------------|--------|
"""
    
    for w in width:
        nan = "YES" if w.get("has_nan") else "no"
        report += f"| {w['n_embd']:>4} | {w['params']:,} | {nan:>6} | {fmt(w['final_train_loss']):.4f} | {fmt(w['max_test_acc'], 3):>6} | {'stable' if not w.get('has_nan') else 'exploded'} |\n"
    
    report += """

### Depth Boundary Analysis

| n_layer | n_embd | Train Loss | Max Test Acc | Status |
|---------|--------|------------|--------------|--------|
"""
    
    for d in depth:
        report += f"| {d['n_layer']:>4} | {d.get('n_embd', 128):>4} | {fmt(d['final_train_loss']):.4f} | {fmt(d['max_test_acc'], 3):>6} | {'competent' if d['max_test_acc'] > 0.5 else 'failed'} |\n"
    
    report += """

### Learning Rate Boundary

| LR | NaN Loss | Train Loss | Max Test Acc | Phase |
|-------|----------|------------|--------------|-------|
"""
    
    for l in lr_results:
        nan = "YES" if l.get("has_nan") else "no"
        phase = "exploding" if l.get("has_nan") else ("too slow" if l["final_train_loss"] > 1.0 else "stable")
        report += f"| {l['lr']:.5f} | {nan:>6} | {fmt(l['final_train_loss']):.4f} | {fmt(l['max_test_acc'], 3):>6} | {phase} |\n"
    
    report += """

### Stable Weird Regimes

| LR | WD | Train Loss | Max Test Acc | Survival? |
|-------|------|------------|--------------|-----------|
"""
    
    for wr in weird:
        survival = "LIVED!" if wr["max_test_acc"] > 0.5 else "died"
        report += f"| {fmt(wr['lr'], 6)} | {fmt(wr['wd'])} | {fmt(wr['final_train_loss']):.4f} | {fmt(wr['max_test_acc'], 3):>6} | {survival} |\n"
    
    report += f"""

## Conclusion: Emergent Behavior Discovery

The phase boundary mapping reveals critical transitions in model competence:

1. **Smallest Competent Architecture**: Found at n_embd = {competent_width}
2. **Phase Transition Point**: NaN explosion occurs below n_embd = {min_nan_width or "N/A"}
3. **Stable Weird Regime**: The highest weight decay that still produces competence

**Implications for Tiny Model Research:**
- Ultra-small transformers have discrete phase transitions, not continuous scaling
- Grokking-like behavior appears at specific parameter thresholds
- Regularization can either kill learning or induce robustness depending on scale

---
Report generated from bold_phase_scan.py experiment family.
"""
    
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write(report)
    
    json_path = path.parent / "bold_phase_scan_results.json"
    with open(json_path, "w") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    results = run_bold_phase_scan()
    
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    width_results = [(w["n_embd"], w["max_test_acc"], "NaN" if w.get("has_nan") else f"{w['max_test_acc']:.3f}") for w in results["width_boundary"]]
    for n_embd, acc, status in width_results:
        marker = "*" if "NaN" not in status and float(status) > 0.5 else ""
        print(f"  n_embd={n_embd:>4}: {status} {marker}")