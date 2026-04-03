#!/usr/bin/env python3
"""Phase boundary sweep for modular addition - dimensionless scaling analysis."""

import torch
from src.models.transformer import TinyTransformer
from src.data.toy_tasks import ModularAddition, get_dataloader
from src.utils.trainer import Trainer


def run_experiment(n_embd, n_layer, p, lr, epochs=200):
    """Run a single experiment and return final metrics."""
    try:
        model = TinyTransformer(vocab_size=p + 2, n_embd=n_embd, n_layer=n_layer, n_head=max(1, n_embd // 32))
        train_ds = ModularAddition(p=p, split=0.8, seed=42)
        test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=123)
        train_loader = get_dataloader(train_ds, batch_size=32)
        test_loader = get_dataloader(test_ds, batch_size=32)

        optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
        trainer = Trainer(
            model, train_loader, test_loader, optimizer, 
            exp_name=f"sweep_p{p}_embd{n_embd}_l{n_layer}"
        )
        trainer.train(epochs)

        final_loss, final_acc = trainer.evaluate()
        return {
            "n_embd": n_embd,
            "n_layer": n_layer,
            "p": p,
            "lr": lr,
            "final_acc": final_acc,
            "final_loss": final_loss,
            "status": "ok",
        }
    except Exception as e:
        import traceback
        print(f"Error in experiment: {e}")
        traceback.print_exc()
        return {
            "n_embd": n_embd,
            "n_layer": n_layer,
            "p": p,
            "lr": lr,
            "final_acc": None,
            "final_loss": None,
            "status": f"error: {str(e)}",
        }


def main():
    results = []
    
    # Phase 1: LR sweep for baseline config (p=7)
    print("\n=== SWEEP 1: LR STABILITY (p=7, n_embd=32, layers=2) ===")
    for lr in [0.1, 0.05, 0.02, 0.01, 0.005]:
        r = run_experiment(32, 2, p=7, lr=lr)
        acc_str = f"{r['final_acc']:.4f}" if r['final_acc'] else "NaN"
        print(f"lr={lr:6f}: acc={acc_str}")
        results.append(r)
    
    # Phase 2: Modulo base sweep (minimal model)
    print("\n=== SWEEP 2: MODULO BASE (n_embd=32, layers=2, lr=0.01) ===")
    for p in [3, 5, 7, 11]:
        r = run_experiment(32, 2, p=p, lr=0.01)
        acc_str = f"{r['final_acc']:.4f}" if r['final_acc'] else "NaN"
        print(f"p={p}: acc={acc_str}")
        results.append(r)
    
    # Phase 3: Width sweep for p=5
    print("\n=== SWEEP 3: WIDTH SWEEP (p=5, layers=2, lr=0.01) ===")
    for embd in [32, 64, 128]:
        r = run_experiment(embd, 2, p=5, lr=0.01)
        acc_str = f"{r['final_acc']:.4f}" if r['final_acc'] else "NaN"
        print(f"embd={embd}: acc={acc_str}")
        results.append(r)
    
    # Phase 4: Depth sweep for p=3
    print("\n=== SWEEP 4: DEPTH SWEEP (p=3, n_embd=32, lr=0.01) ===")
    for layers in [1, 2, 4]:
        r = run_experiment(32, layers, p=3, lr=0.01)
        acc_str = f"{r['final_acc']:.4f}" if r['final_acc'] else "NaN"
        print(f"layers={layers}: acc={acc_str}")
        results.append(r)
    
    # Phase 5: Critical region (p=5, varied depth)
    print("\n=== SWEEP 5: CRITICAL REGION (p=5, n_embd=64, lr=0.01) ===")
    for layers in [2, 3, 4]:
        r = run_experiment(64, layers, p=5, lr=0.01)
        acc_str = f"{r['final_acc']:.4f}" if r['final_acc'] else "NaN"
        print(f"layers={layers}: acc={acc_str}")
        results.append(r)
    
    # Write summary to file
    import json
    os.makedirs("reports", exist_ok=True)
    with open("reports/phase_sweep_summary.jsonl", "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"\n=== Results written to reports/phase_sweep_summary.jsonl ===")


if __name__ == "__main__":
    import os
    main()