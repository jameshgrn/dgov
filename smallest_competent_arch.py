"""
SMALLEST COMPETENT ARCHITECTURE EXPERIMENT

Goal: Find the absolute minimum parameters for a transformer to solve modular arithmetic.

We test the parameter space aggressively by sweeping:
- Hidden dimension: 8, 12, 16, 24, 32 (down to extreme limits)
- Layers: 1, 2 (minimum depth)
- Heads: 1, 2 (single-head attention is interesting)

The question: At what point does competence completely collapse?
"""

import torch
from src.models.transformer import TinyTransformer
from src.data.toy_tasks import ModularAddition


def run_architecture_sweep():
    """
    Sweep down to minimal architecture. Test models from normal down to absurdly small.
    """
    
    # Task setup: use a prime modulus where simple patterns don't work
    p = 107  # Prime, larger than typical toy tasks
    
    model_configs = [
        (64, 2, 4),      # Baseline: reasonable but small
        (32, 2, 2),      # Medium-small
        (24, 2, 2),      # Getting tight
        (16, 2, 2),      # Very small
        (16, 1, 1),      # Extreme: single head, single layer
        (12, 1, 1),      # Absurdly small
        (8, 1, 1),       # Minimum viable transformer?
    ]
    
    results = []
    
    for n_embd, n_layer, n_head in model_configs:
        print(f"\n{'='*60}")
        print(f"Testing: n_embd={n_embd}, n_layer={n_layer}, n_head={n_head}")
        print('='*60)
        
        # Create model with very small embedding space (p+3 tokens needed for bits + SEP + PAD)
        vocab_size = p + 5
        
        # Check if config is even physically possible
        if n_embd < n_head * 2:
            print(f"WARNING: Embd dim {n_embd} too small for {n_head} heads!")
            continue
            
        model = TinyTransformer(
            vocab_size=vocab_size,
            n_embd=n_embd,
            n_head=n_head,
            n_layer=n_layer,
        )
        
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)

        total_params = sum(p.numel() for p in model.parameters())
        print(f"Total parameters: {total_params:,}")
        
        # Create datasets with sufficient samples
        train_ds = ModularAddition(p=p, split=0.8, mode="train", seed=42)
        test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=42)
        
        train_loader = torch.utils.data.DataLoader(train_ds, batch_size=128, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_ds, batch_size=64, shuffle=False)
        
        # Train for fixed epochs regardless of model size
        max_epochs = 30

        try:
            # Use simple trainer loop instead of Trainer class
            for epoch in range(max_epochs):
                model.train()
                total_loss = 0
                for x, y in train_loader:
                    x, y = x.to(model.device), y.to(model.device)
                    logits, loss = model(x, y)
                    
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    
                    total_loss += loss.item()

            # Evaluate final performance
            model.eval()
            correct = 0
            total = 0
            
            for x, y in test_loader:
                x, y = x.to(model.device), y.to(model.device)
                with torch.no_grad():
                    logits, _ = model(x, y)
                
                # For modular addition, we need to check if all bits are predicted correctly
                pred = torch.argmax(logits, dim=-1)
                correct += (pred == y).sum().item()
                total += y.numel()
            
            accuracy = correct / total
            print(f"Test accuracy: {accuracy*100:.1f}%")
            
            results.append({
                "config": {"n_embd": n_embd, "n_layer": n_layer, "n_head": n_head},
                "params": total_params,
                "accuracy": accuracy,
                "competent": accuracy > 0.95  # Define "competent" as 95% bit-level accuracy
            })
            
        except Exception as e:
            print(f"ERROR training: {e}")
            results.append({
                "config": {"n_embd": n_embd, "n_layer": n_layer, "n_head": n_head},
                "error": str(e),
                "competent": False
            })
    
    # Summary report
    print("\n" + "="*60)
    print("SUMMARY: SMALLEST COMPETENT ARCHITECTURE")
    print("="*60)
    print(f"{'Config':<25} {'Params':>10} {'Accuracy':>10} {'Competent?'}")
    print("-"*60)
    
    for r in results:
        if "error" not in r:
            config_str = f"n_embd={r['config']['n_embd']}, n_layer={r['config']['n_layer']}, n_head={r['config']['n_head']}"
            status = "YES" if r["competent"] else "NO"
            print(f"{config_str:<25} {r['params']:>10,} {r['accuracy']*100:>9.1f}% {status}")
    
    # Find the smallest competent model
    competent_models = [r for r in results if r.get("competent", False)]
    if competent_models:
        smallest = min(competent_models, key=lambda x: x["params"])
        print(f"\nSMALLEST COMPETENT MODEL:")
        print(f"  Config: n_embd={smallest['config']['n_embd']}, n_layer={smallest['config']['n_layer']}, n_head={smallest['config']['n_head']}")
        print(f"  Parameters: {smallest['params']:,}")
        print(f"  Accuracy: {smallest['accuracy']*100:.1f}%")
    else:
        print("\nNO COMPETENT MODELS FOUND!")
    
    return results


if __name__ == "__main__":
    import json
    
    import os
    os.makedirs("reports", exist_ok=True)
    results = run_architecture_sweep()
    
    # Save results
    with open("reports/smallest_competent_arch_results.jsonl", "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    
    print("\nResults saved to reports/smallest_competent_arch_results.jsonl")