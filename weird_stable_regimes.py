"""
STABLE WEIRD REGIMES EXPERIMENT

Goal: Find settings that should FAIL but surprisingly work well.

These are the "phase transitions into competence from absurd regimes":

1. EXTREME UNDERPARAMETERIZATION: Model has fewer parameters than input dimensions
2. MASSIVE WEIGHT SPARSITY: Only 10% of weights active at a time
3. CORRUPTED LABELS: Random noise in targets but model still learns
4. EXTREME LEARNING RATE SWEEP: Testing LR values that should explode or vanish

The insight: Understanding where competence is robust vs fragile reveals the true nature of learning.
"""

import torch
from src.models.transformer import TinyTransformer
from src.data.toy_tasks import ModularAddition


def experiment_extreme_underparameterization():
    """
    Test when model params < input dimensions.
    
    For modular addition with p=107, we have ~8 bits per number + separators,
    so input dimension is roughly 16 tokens. 
    
    A 4-layer model with tiny hidden dim might have total params < 256.
    """
    
    print("\n" + "="*60)
    print("EXP: EXTREME UNDERPARAMETERIZATION")
    print("="*60)
    
    p = 107
    vocab_size = p + 5
    
    # Configs where model is absurdly small
    configs = [
        (8, 1, 1),   # 8-dim embeddings, single layer/head - should fail spectacularly
        (16, 1, 1),  # Bare minimum for transformer math
        (32, 2, 4),  # Getting more reasonable but still small
    ]
    
    results = []
    
    for n_embd, n_layer, n_head in configs:
        print(f"\nTesting: n_embd={n_embd}, n_layer={n_layer}, n_head={n_head}")
        
        model = TinyTransformer(
            vocab_size=vocab_size,
            n_embd=n_embd,
            n_head=n_head,
            n_layer=n_layer,
        )
        
        optimizer = torch.optim.AdamW(model.parameters(), lr=5e-3)  # Higher LR to compensate for tiny model
        
        total_params = sum(p.numel() for p in model.parameters())
        print(f"Total params: {total_params:,}")
        
        # Small dataset since model is tiny (risk of memorization)
        train_ds = ModularAddition(p=p, split=0.5, mode="train", seed=42)
        test_ds = ModularAddition(p=p, split=0.5, mode="test", seed=42)
        
        train_loader = torch.utils.data.DataLoader(train_ds, batch_size=32, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_ds, batch_size=32, shuffle=False)
        
        # Train for moderate epochs
        for epoch in range(50):
            total_loss = 0
            for x, y in train_loader:
                x, y = x.to(model.device), y.to(model.device)
                logits, loss = model(x, y)
                
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                
                total_loss += loss.item()
        
        # Evaluate
        model.eval()
        correct = 0
        total = 0
        
        with torch.no_grad():
            for x, y in test_loader:
                x, y = x.to(model.device), y.to(model.device)
                logits, _ = model(x, y)
                
                pred = torch.argmax(logits, dim=-1)
                correct += (pred == y).sum().item()
                total += y.numel()
        
        accuracy = correct / total
        
        print(f"Test accuracy: {accuracy*100:.1f}%")
        
        results.append({
            "config": {"n_embd": n_embd, "n_layer": n_layer, "n_head": n_head},
            "params": total_params,
            "accuracy": accuracy,
            "surprising": accuracy > 0.5  # Better than random is surprising for tiny models
        })
    
    print("\nUnderparameterization results:")
    for r in results:
        surprise = "SURPRISING!" if r["surprising"] else "expected failure"
        print(f"  Params={r['params']:>6,} acc={r['accuracy']*100:.1f}% [{surprise}]")
    
    return results


def experiment_weight_sparsity():
    """
    Test massive weight sparsity.
    
    Set 90% of weights to zero BEFORE training and see if the model can learn anyway.
    This is essentially testing: does the remaining 10% have enough capacity?
    """
    
    print("\n" + "="*60)
    print("EXP: WEIGHT SPARSITY (90% sparsity)")
    print("="*60)
    
    p = 107
    vocab_size = p + 5
    
    for sparsity_pct in [0.9, 0.95, 0.99]:
        print(f"\nTesting {sparsity_pct*100:.0f}% sparsity")
        
        model = TinyTransformer(
            vocab_size=vocab_size,
            n_embd=32,
            n_head=4,
            n_layer=2,
        )
        
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
        
        # Apply sparsity mask before training - zero out weights after each step for true sparsity
        def apply_sparsity(mask):
            for name, param in model.named_parameters():
                if "bias" not in name:  # Keep biases full
                    with torch.no_grad():
                        param.mul_(mask.to(param.device))
        
        mask_90 = torch.rand_like(model.weight) > 0.9 if hasattr(model, 'weight') else None
        
        train_ds = ModularAddition(p=p, split=0.8, mode="train", seed=42)
        test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=42)
        
        train_loader = torch.utils.data.DataLoader(train_ds, batch_size=32, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_ds, batch_size=32, shuffle=False)
        
        for epoch in range(100):
            total_loss = 0
            for x, y in train_loader:
                x, y = x.to(model.device), y.to(model.device)
                logits, loss = model(x, y)
                
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                
                # Apply sparsity after update to maintain it
                if mask_90 is not None:
                    with torch.no_grad():
                        for name, param in model.named_parameters():
                            if "bias" not in name:
                                param.mul_(mask_90.to(param.device))
                
                total_loss += loss.item()
        
        # Evaluate
        model.eval()
        correct = 0
        total = 0
        
        with torch.no_grad():
            for x, y in test_loader:
                x, y = x.to(model.device), y.to(model.device)
                logits, _ = model(x, y)
                
                pred = torch.argmax(logits, dim=-1)
                correct += (pred == y).sum().item()
                total += y.numel()
        
        accuracy = correct / total
        
        print(f"Test accuracy: {accuracy*100:.1f}%")
        
        if accuracy > 0.5:
            print("SURPRISE: Learned despite extreme sparsity!")


def experiment_corrupted_labels():
    """
    Test with corrupted labels.
    
    Replace some fraction of labels with random noise during training.
    Can the model learn the underlying pattern despite corruption?
    """
    
    print("\n" + "="*60)
    print("EXP: CORRUPTED LABELS")
    print("="*60)
    
    p = 107
    vocab_size = p + 5
    
    for corruption_pct in [0.0, 0.1, 0.25, 0.5]:
        print(f"\nTesting {corruption_pct*100:.0f}% label corruption")
        
        model = TinyTransformer(
            vocab_size=vocab_size,
            n_embd=48,
            n_head=6,
            n_layer=2,
        )
        
        optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
        
        train_ds = ModularAddition(p=p, split=0.8, mode="train", seed=42)
        test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=42)  # Clean test set
        
        train_loader = torch.utils.data.DataLoader(train_ds, batch_size=32, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_ds, batch_size=32, shuffle=False)
        
        # Wrap training loop to corrupt labels
        for epoch in range(100):
            total_loss = 0
            
            for x, y in train_loader:
                x, y = x.to(model.device), y.to(model.device)
                
                # Corrupt labels during training only
                if corruption_pct > 0:
                    corrupt_mask = torch.rand_like(y, dtype=torch.bool)
                    n_corrupt = int(corruption_pct * y.numel())
                    
                    # Replace some targets with random values
                    rand_idx = torch.randint(0, vocab_size - 2, (n_corrupt,), device=y.device)
                    positions = torch.multinomial(torch.ones(y.numel()), n_corrupt, replacement=False)
                    y[positions] = rand_idx[:len(positions)]
                
                logits, loss = model(x, y)
                
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                
                total_loss += loss.item()
        
        # Evaluate on CLEAN test set
        model.eval()
        correct = 0
        total = 0
        
        with torch.no_grad():
            for x, y in test_loader:
                x, y = x.to(model.device), y.to(model.device)
                logits, _ = model(x, y)
                
                pred = torch.argmax(logits, dim=-1)
                correct += (pred == y).sum().item()
                total += y.numel()
        
        accuracy = correct / total
        
        print(f"Test accuracy: {accuracy*100:.1f}%")
        
        if corruption_pct > 0 and accuracy > 0.8:
            print("SURPRISE: Learned despite corruption!")


def experiment_learning_rate_extremes():
    """
    Test LR values that should be catastrophic.
    
    - Extremely high LR: Should cause divergence/exploding gradients
    - Extremely low LR: Should never learn within reasonable epochs
    """
    
    print("\n" + "="*60)
    print("EXP: EXTREME LEARNING RATES")
    print("="*60)
    
    p = 107
    vocab_size = p + 5
    
    for lr in [1e-6, 1e-4, 5e-3, 0.05]:  # Wide spectrum
        print(f"\nTesting LR={lr}")
        
        model = TinyTransformer(
            vocab_size=vocab_size,
            n_embd=48,
            n_head=6,
            n_layer=2,
        )
        
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
        
        train_ds = ModularAddition(p=p, split=0.8, mode="train", seed=42)
        test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=42)
        
        train_loader = torch.utils.data.DataLoader(train_ds, batch_size=32, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_ds, batch_size=32, shuffle=False)
        
        model.train()
        training_stable = True
        
        for epoch in range(100):
            total_loss = 0
            
            try:
                for x, y in train_loader:
                    x, y = x.to(model.device), y.to(model.device)
                    logits, loss = model(x, y)
                    
                    if not torch.isfinite(loss):
                        print(f"  Epoch {epoch}: LOSS NON-FINITE, stopping")
                        training_stable = False
                        break
                    
                    optimizer.zero_grad()
                    loss.backward()
                    optimizer.step()
                    
                    total_loss += loss.item()
                
                if not training_stable:
                    break
                    
            except RuntimeError as e:
                print(f"  Epoch {epoch}: ERROR - {e}")
                training_stable = False
                break
        
        # Only evaluate if training was stable
        if training_stable:
            model.eval()
            correct = 0
            total = 0
            
            with torch.no_grad():
                for x, y in test_loader:
                    x, y = x.to(model.device), y.to(model.device)
                    logits, _ = model(x, y)
                    
                    pred = torch.argmax(logits, dim=-1)
                    correct += (pred == y).sum().item()
                    total += y.numel()
            
            accuracy = correct / total
            
            # Check for interesting behavior
            if lr == 1e-6 and accuracy > 0.1:
                print(f"Test accuracy: {accuracy*100:.1f}% - SURPRISE at low LR!")
            elif lr >= 0.05 and accuracy > 0.3:
                print(f"Test accuracy: {accuracy*100:.1f}% - SURPRISE at high LR!")
            else:
                print(f"Test accuracy: {accuracy*100:.1f}%")
        else:
            print("Training unstable, no evaluation")


if __name__ == "__main__":
    import json
    import os
    
    os.makedirs("reports", exist_ok=True)
    
    all_results = []
    
    # Run all experiments
    results1 = experiment_extreme_underparameterization()
    results2 = experiment_weight_sparsity()
    results3 = experiment_corrupted_labels()
    results4 = experiment_learning_rate_extremes()
    
    all_results.extend(results1)
    
    # Save results
    with open("reports/weird_stable_regimes_results.jsonl", "w") as f:
        for r in all_results:
            f.write(json.dumps(r) + "\n")
    
    print("\nResults saved to reports/weird_stable_regimes_results.jsonl")