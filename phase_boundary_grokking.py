"""
PHASE BOUNDARY MAPPING: GROKKING THRESHOLD

Goal: Identify exactly when (and if) grokking emerges for modular arithmetic.

Grokking is a phase transition where:
- Train loss plateaus or stays flat
- Test loss suddenly drops after many epochs
- Sudden jump from random to perfect performance

We will sweep:
1. Epochs: Fine-grained sweeps from 0 to 200+ (long training)
2. Dataset size: Small vs large (grokking often needs enough data but not too much)
3. Learning rate: The critical knob for grokking

Hypothesis: There's a precise boundary in epoch count / dataset ratio where sudden competence appears.
"""

import torch
from src.models.transformer import TinyTransformer
from src.data.toy_tasks import ModularAddition


def analyze_for_grokking(training_log):
    """
    Detect grokking signature:
    1. Train loss plateaus at some value > 0 for many epochs
    2. Test accuracy stays near random (~50%) or low
    3. Suddenly, test accuracy jumps to near-perfect
    4. This happens AFTER the plateau phase
    
    Returns True if grokking signature is detected.
    """
    
    if len(training_log) < 20:
        return False
    
    # Look for sudden jump in test accuracy in later epochs
    mid_point = len(training_log) // 2
    
    early_test_acc = sum(
        e["test_accuracy"] 
        for e in training_log[:mid_point] 
        if e["test_accuracy"] > 0
    ) / max(1, sum(1 for e in training_log[:mid_point] if e["test_accuracy"] > 0))
    
    late_test_acc = sum(
        e["test_accuracy"] 
        for e in training_log[mid_point:] 
        if e["test_accuracy"] > 0
    ) / max(1, sum(1 for e in training_log[mid_point:] if e["test_accuracy"] > 0))
    
    # Grokking signature: late accuracy significantly higher than early accuracy
    # AND both are non-zero (model learned something eventually)
    if late_test_acc > 0.8 and late_test_acc > early_test_acc * 2:
        return True
    
    # Also check for sudden step change in the last quarter of training
    last_quarter = training_log[-len(training_log)//4:]
    if len(last_quarter) >= 4:
        early_last = sum(e["test_accuracy"] for e in last_quarter[:2]) / 2
        late_last = sum(e["test_accuracy"] for e in last_quarter[2:]) / max(1, len(last_quarter)-2)
        
        if late_last > 0.9 and early_last < 0.5:
            return True
    
    return False


def measure_grokking_phase_transition():
    """
    Sweep learning rate and epochs to find grokking boundary.
    Look for sudden drops in test loss after stable train loss.
    """
    
    # Task setup - prime modulus is crucial for genuine computation (not memorization)
    p = 107
    
    # Configuration grid for phase boundary search
    configs = [
        # (lr, epochs, dataset_size_multiplier)
        (5e-4, 50,   0.5),  # Low LR, short, small data - likely no grokking
        (1e-3, 50,   2.0),  # Medium LR, standard
        (5e-4, 200,  0.5),  # Low LR, long training - GROKKING CANDIDATE
        (2e-4, 300,  0.3),  # Very low LR, very long - ANOTHER CANDIDATE
        (1e-3, 150,  0.3),  # Medium LR but compressed data - interesting regime
    ]
    
    results = []
    
    for lr, max_epochs, data_mult in configs:
        print(f"\n{'='*60}")
        print(f"Testing: lr={lr}, epochs={max_epochs}, data_mult={data_mult}")
        print('='*60)
        
        vocab_size = p + 5
        
        model = TinyTransformer(
            vocab_size=vocab_size,
            n_embd=48,
            n_head=6,
            n_layer=2,
        )
        
        optimizer = torch.optim.AdamW(model.parameters(), lr=lr)
        
        # Scale dataset size - this is key for grokking
        train_ds = ModularAddition(p=p, split=data_mult, mode="train", seed=42)
        test_ds = ModularAddition(p=p, split=data_mult, mode="test", seed=42)
        
        print(f"Dataset sizes: train={len(train_ds)}, test={len(test_ds)}")
        
        train_loader = torch.utils.data.DataLoader(train_ds, batch_size=64, shuffle=True)
        test_loader = torch.utils.data.DataLoader(test_ds, batch_size=64, shuffle=False)
        
        # Track metrics at each epoch for phase transition detection
        training_log = []
        
        model.train()
        for epoch in range(max_epochs):
            total_train_loss = 0
            for x, y in train_loader:
                x, y = x.to(model.device), y.to(model.device)
                logits, loss = model(x, y)
                
                optimizer.zero_grad()
                loss.backward()
                optimizer.step()
                
                total_train_loss += loss.item()
            
            avg_train_loss = total_train_loss / len(train_loader)
            
            # Evaluate test performance periodically (not every epoch for speed)
            if epoch % 5 == 0 or epoch == max_epochs - 1:
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
                
                test_accuracy = correct / total
                
            else:
                test_accuracy = 0.0
            
            training_log.append({
                "epoch": epoch,
                "train_loss": avg_train_loss,
                "test_accuracy": test_accuracy
            })
            
            # Early termination if model completely converges
            if test_accuracy > 0.99 and epoch > 50:
                print(f"Full convergence at epoch {epoch}, continuing to check for phase transition...")
        
        # Analyze for grokking signature
        is_grokking = analyze_for_grokking(training_log)
        
        results.append({
            "config": {"lr": lr, "epochs": max_epochs, "data_mult": data_mult},
            "training_log": training_log[-20:],  # Save last 20 epochs for detailed analysis
            "is_grokking": is_grokking,
            "final_train_loss": training_log[-1]["train_loss"],
            "final_test_accuracy": training_log[-1]["test_accuracy"]
        })
        
        print(f"Final train loss: {training_log[-1]['train_loss']:.4f}")
        print(f"Final test accuracy: {training_log[-1]['test_accuracy']*100:.1f}%")
        print(f"Grokking detected: {is_grokking}")
    
    # Generate phase boundary analysis
    print("\n" + "="*60)
    print("PHASE BOUNDARY ANALYSIS")
    print("="*60)
    
    for r in results:
        config = r["config"]
        status = "GROKKING!" if r["is_grokking"] else "no grokking"
        print(f"lr={config['lr']:.4f}, epochs={config['epochs']:3d}, data_mult={config['data_mult']:.1f} => {status}")
    
    return results


if __name__ == "__main__":
    import json
    import os
    
    os.makedirs("reports", exist_ok=True)
    
    print("="*60)
    print("PHASE BOUNDARY MAPPING: GROKKING EXPERIMENT")
    print("="*60)
    
    results = measure_grokking_phase_transition()
    
    # Save results
    with open("reports/phase_boundary_grokking_results.jsonl", "w") as f:
        for r in results:
            # Remove large training logs from JSON save to avoid huge files
            r_small = {k: v for k, v in r.items() if k != "training_log"}
            f.write(json.dumps(r_small) + "\n")
    
    print("\nResults saved to reports/phase_boundary_grokking_results.jsonl")