#!/usr/bin/env python3
"""Phase boundary mapping for modular addition.

This sweeps parameter space to find:
1. Learning rate stability boundary (stable vs NaN loss)
2. Width boundary (minimum n_embd for stable training)
3. Prime number effects (p=7 vs p=11)
4. Layer count effects
"""
import torch
from src.data.toy_tasks import ModularAddition, get_dataloader
from src.models.transformer import TinyTransformer
import torch.optim as optim
import json
import os
from datetime import datetime


def run_experiment(n_embd, n_layer=1, n_head=None, p=7, lr=0.001, epochs=200):
    """Run a single experiment with given architecture."""
    if n_head is None:
        n_head = n_embd // 32
        n_head = max(1, min(n_head, n_layer))  # sensible cap
    
    train_ds = ModularAddition(p=p, split=0.8, mode="train", seed=42)
    test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=42)
    vocab_size = p + 2

    train_loader = get_dataloader(train_ds, batch_size=32)
    test_loader = get_dataloader(test_ds, batch_size=32, shuffle=False)

    model = TinyTransformer(
        vocab_size=vocab_size,
        n_embd=n_embd,
        n_head=n_head,
        n_layer=n_layer,
        block_size=128,
        dropout=0.0,
        tie_weights=True,
        norm_type="layer",
    )

    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)

    device = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
    model.to(device)

    best_acc = 0
    final_loss = None
    
    try:
        for epoch in range(epochs):
            model.train()
            total_loss = 0
            nan_detected = False
            
            for x, y in train_loader:
                x, y = x.to(device), y.to(device)
                _, loss = model(x, targets=y)
                
                if torch.isnan(loss) or torch.isinf(loss):
                    nan_detected = True
                    break
                    
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                total_loss += loss.item()
            
            if nan_detected:
                return {
                    "n_embd": n_embd, "n_layer": n_layer, "n_head": n_head,
                    "p": p, "lr": lr, "best_acc": None, "final_loss": None,
                    "status": "NaN", "epochs_trained": epoch
                }

            # Evaluate every 10 epochs
            if epoch % 10 == 0 or epoch == epochs - 1:
                model.eval()
                correct = 0
                total = 0
                with torch.no_grad():
                    for x, y in test_loader:
                        x, y = x.to(device), y.to(device)
                        logits, _ = model(x, targets=y)
                        preds = torch.argmax(logits[:, -1, :], dim=-1)
                        correct += (preds == y[:, -1]).sum().item()
                        total += y.size(0)

                acc = correct / total
                if acc > best_acc:
                    best_acc = acc
                
                avg_loss = total_loss / len(train_loader)
                final_loss = avg_loss
                
    except Exception as e:
        return {
            "n_embd": n_embd, "n_layer": n_layer, "n_head": n_head,
            "p": p, "lr": lr, "best_acc": None, "final_loss": None,
            "status": f"Exception: {str(e)[:50]}", "epochs_trained": epochs
        }

    return {
        "n_embd": n_embd, "n_layer": n_layer, "n_head": n_head,
        "p": p, "lr": lr, "best_acc": best_acc, "final_loss": final_loss,
        "status": "ok", "epochs_trained": epochs
    }


def sweep_lr_stability():
    """Find the LR boundary where training becomes unstable (NaN)."""
    print("="*70)
    print("SWEEP 1: Learning Rate Stability Boundary")
    print("(p=7, n_embd=32, n_layer=2)")
    print("="*70)
    
    results = {}
    for lr in [0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001, 0.0005]:
        print(f"\nTesting lr={lr}...")
        r = run_experiment(n_embd=32, n_layer=2, p=7, lr=lr, epochs=200)
        results[f"lr_{lr}"] = r
        acc_str = f"{r['best_acc']:.4f}" if r['best_acc'] else "NaN"
        print(f"Result: status={r['status']}, best_acc={acc_str}")
    
    return results


def sweep_width_boundary():
    """Find the minimum width (n_embd) for stable training."""
    print("\n" + "="*70)
    print("SWEEP 2: Width Boundary")
    print("(p=7, n_layer=2, lr=0.001)")
    print("="*70)
    
    results = {}
    for embd in [32, 64, 96, 128]:
        print(f"\nTesting n_embd={embd}...")
        r = run_experiment(n_embd=embd, n_layer=2, p=7, lr=0.001, epochs=200)
        results[f"embd_{embd}"] = r
        acc_str = f"{r['best_acc']:.4f}" if r['best_acc'] else "NaN"
        print(f"Result: status={r['status']}, best_acc={acc_str}")
    
    return results


def sweep_primes():
    """Test different primes to see complexity effects."""
    print("\n" + "="*70)
    print("SWEEP 3: Prime Number Effects")
    print("(n_embd=64, n_layer=2, lr=0.001)")
    print("="*70)
    
    results = {}
    for p in [5, 7, 11]:
        print(f"\nTesting p={p}...")
        r = run_experiment(n_embd=64, n_layer=2, p=p, lr=0.001, epochs=200)
        results[f"p_{p}"] = r
        acc_str = f"{r['best_acc']:.4f}" if r['best_acc'] else "NaN"
        print(f"Result: status={r['status']}, best_acc={acc_str}")
    
    return results


def sweep_layers():
    """Test layer count with minimal width."""
    print("\n" + "="*70)
    print("SWEEP 4: Layer Count Sweep")
    print("(p=7, n_embd=64, lr=0.001)")
    print("="*70)
    
    results = {}
    for layers in [1, 2, 3]:
        print(f"\nTesting n_layer={layers}...")
        r = run_experiment(n_embd=64, n_layer=layers, p=7, lr=0.001, epochs=200)
        results[f"layers_{layers}"] = r
        acc_str = f"{r['best_acc']:.4f}" if r['best_acc'] else "NaN"
        print(f"Result: status={r['status']}, best_acc={acc_str}")
    
    return results


def save_results(all_results):
    """Save all sweep results to JSON."""
    os.makedirs("reports", exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"reports/mod_add_boundary_sweep_{timestamp}.json"
    
    with open(filename, "w") as f:
        json.dump(all_results, f, indent=2)
    
    print(f"\nResults saved to {filename}")
    return filename


def main():
    """Run all boundary sweeps."""
    print("\n" + "#"*70)
    print("# MOD ADD BOUNDARY SWEEP - REFINER TASK")
    print("# Mapping phase boundaries for modular addition capability")
    print("#"*70 + "\n")
    
    all_results = {}
    
    # Sweep 1: LR stability
    all_results["lr_stability"] = sweep_lr_stability()
    
    # Sweep 2: Width boundary  
    all_results["width_boundary"] = sweep_width_boundary()
    
    # Sweep 3: Prime effects
    all_results["prime_effects"] = sweep_primes()
    
    # Sweep 4: Layer sweep
    all_results["layer_sweep"] = sweep_layers()
    
    # Save results
    filename = save_results(all_results)
    
    # Print summary
    print("\n" + "#"*70)
    print("# SUMMARY")
    print("#"*70)
    
    for sweep_name, sweeps in all_results.items():
        print(f"\n{sweep_name.upper()}")
        for key, r in sweeps.items():
            acc_str = f"{r['best_acc']:.4f}" if r['best_acc'] else "NaN"
            status_char = "✗" if r["status"] != "ok" else ("✓" if r["best_acc"] > 0.9 else "~")
            print(f"  {key}: acc={acc_str:8s} {r['status']:20s} {status_char}")
    
    print("\n# SWEEP COMPLETE")
    print(f"# Results saved to: {filename}\n")


if __name__ == "__main__":
    main()