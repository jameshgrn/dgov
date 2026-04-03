#!/usr/bin/env python3
"""Phase boundary sweep for modular addition."""
import torch
from src.models.transformer import TinyTransformer
from src.data.toy_tasks import ModularAddition, get_dataloader
from src.utils.trainer import Trainer

def run_experiment(n_embd, n_layer, p, lr, epochs=100):
    """Run a single experiment and return final metrics."""
    try:
        model = TinyTransformer(vocab_size=p+2, n_embd=n_embd, n_layer=n_layer, n_head=1)
        train_ds = ModularAddition(p=p, split=0.8, seed=42)
        test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=42)
        train_loader = get_dataloader(train_ds, batch_size=32)
        test_loader = get_dataloader(test_ds, batch_size=32)
        
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=0.01)
        trainer = Trainer(model, train_loader, test_loader, optimizer, exp_name=f"p{p}_embd{n_embd}")
        trainer.train(epochs)
        
        final_loss, final_acc = trainer.evaluate()
        return {"n_embd": n_embd, "n_layer": n_layer, "p": p, "lr": lr, 
                "final_acc": final_acc, "final_loss": final_loss, "status": "ok"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"n_embd": n_embd, "n_layer": n_layer, "p": p, "lr": lr, 
                "final_acc": None, "final_loss": None, "status": f"error: {str(e)}"}

# Sweep 1: Learning rate stability for minimal config
print("=== LR STABILITY SWEEP (p=7, n_embd=32, layers=2) ===")
for lr in [0.05, 0.02, 0.01, 0.005, 0.001, 0.0005]:
    r = run_experiment(32, 2, p=7, lr=lr)
    acc_str = f"{r['final_acc']:.4f}" if r['final_acc'] else "NaN"
    print(f"lr={lr:6f}: acc={acc_str}")

# Sweep 2: Test smaller primes
print("\n=== SMALLER PRIME SWEEP (n_embd=32, layers=2, lr=0.001) ===")
for p in [3, 5, 7]:
    r = run_experiment(32, 2, p=p, lr=0.001)
    acc_str = f"{r['final_acc']:.4f}" if r['final_acc'] else "NaN"
    print(f"p={p}: acc={acc_str}")

# Sweep 3: Larger width for p=5
print("\n=== LARGER WIDTH SWEEP (p=5, layers=2, lr=0.001) ===")
for embd in [64, 128]:
    r = run_experiment(embd, 2, p=5, lr=0.001)
    acc_str = f"{r['final_acc']:.4f}" if r['final_acc'] else "NaN"
    print(f"embd={embd}: acc={acc_str}")