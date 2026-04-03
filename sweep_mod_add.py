import torch
from src.data.toy_tasks import ModularAddition, get_dataloader
from src.models.transformer import TinyTransformer
import torch.optim as optim
import json
import os
from datetime import datetime


def run_experiment(n_embd, n_layer=1, n_head=1, seed=42):
    """Run a single experiment with given architecture."""
    p = 113
    
    train_ds = ModularAddition(p=p, split=0.8, mode="train", seed=seed)
    test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=seed)
    vocab_size = p + 2

    train_loader = get_dataloader(train_ds, batch_size=64)
    test_loader = get_dataloader(test_ds, batch_size=64, shuffle=False)

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

    optimizer = optim.AdamW(model.parameters(), lr=1e-3, weight_decay=0.1)

    device = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
    model.to(device)

    best_acc = 0
    for epoch in range(50):
        model.train()
        total_loss = 0
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            _, loss = model(x, targets=y)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        # Evaluate every 5 epochs
        if epoch % 5 == 0 or epoch == 49:
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
            
            avg_loss = total_loss / len(train_loader)
            print(f"n_embd={n_embd}, epoch={epoch}, loss={avg_loss:.4f}, acc={acc:.4f}")
    
    return best_acc


def main():
    n_embd_values = [32, 64, 128, 256]
    results = {}

    for n_embd in n_embd_values:
        print(f"\n{'='*50}")
        print(f"Testing n_embd={n_embd}, n_layer=1, n_head=1")
        print(f"{'='*50}\n")
        
        acc = run_experiment(n_embd=n_embd)
        results[n_embd] = {"acc": acc, "passed": acc > 0.95}
        print(f"\nFinal result: n_embd={n_embd}, best_acc={acc:.4f}, passed_95={acc > 0.95}\n")

    # Save results
    os.makedirs("reports", exist_ok=True)
    with open("reports/mod_add_sweep_results.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print("\n" + "="*50)
    print("SWEEP COMPLETE")
    print("="*50)
    for n_embd, res in sorted(results.items()):
        status = "✓ PASS" if res["passed"] else "✗ FAIL"
        print(f"n_embd={n_embd:3d}: accuracy={res['acc']:.4f} {status}")

if __name__ == "__main__":
    main()
