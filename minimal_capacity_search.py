"""
Minimal Capacity Search - Finding Phase Boundaries in Tiny Transformers
=========================================================================

Research agenda:
1. Smallest Competent Architecture - minimum params for parity task
2. Phase Boundary Mapping - where does capability suddenly appear?
3. Stable Weird Regimes - settings that should fail but work
"""

import torch
from torch.utils.data import DataLoader
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.transformer import TinyTransformer
from src.data.toy_tasks import ParityTask, ModularAddition


def get_model_params(model):
    """Count trainable parameters."""
    return sum(p.numel() for p in model.parameters())


def evaluate_model(model, loader, device="cpu", num_batches=20):
    """Evaluate test accuracy."""
    model.eval()
    correct = 0
    total = 0
    losses = []
    
    with torch.no_grad():
        for i, (x, y) in enumerate(loader):
            if i >= num_batches:
                break
            x, y = x.to(device), y.to(device)
            
            logits, loss = model(x, y)
            if loss is not None:
                losses.append(loss.item())
            
            # Predict next token (last position)
            preds = torch.argmax(logits[:, -1, :], dim=-1)
            correct += (preds == y[:, -1]).sum().item()
            total += y.size(0)
    
    return {
        "accuracy": correct / total if total > 0 else 0.0,
        "loss": sum(losses) / len(losses) if losses else float("inf"),
        "samples": total
    }


def train_model(model, loader, epochs=50, lr=1e-3, device="cpu"):
    """Train model and return final metrics."""
    from torch.optim import AdamW
    
    optimizer = AdamW(model.parameters(), lr=lr)
    
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        
        for x, y in loader:
            x, y = x.to(device), y.to(device)
            
            optimizer.zero_grad()
            logits, loss = model(x, y)
            
            if loss is not None:
                loss.backward()
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
                optimizer.step()
                total_loss += loss.item()
    
    return total_loss / epochs


def run_contrastive_family(name, configs, task_type="parity"):
    """Run a family of contrasting configurations."""
    print(f"\n{'='*70}")
    print(f"EXPERIMENT: {name}")
    print(f"{'='*70}")
    
    results = []
    device = "cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu"
    
    for i, cfg in enumerate(configs):
        print(f"\nConfig {i+1}/{len(configs)}: ", end="")
        
        # Create task with specified parameters
        seq_len = cfg.get("seq_len", 16)
        
        if task_type == "parity":
            train_ds = ParityTask(seq_len=seq_len, num_samples=2000)
            test_ds = ParityTask(seq_len=seq_len, num_samples=500)
            vocab_size = 3  # 0, 1, separator
        elif task_type == "modular":
            p = cfg.get("p", 113)
            train_ds = ModularAddition(p=p, split=0.8, mode="train")
            test_ds = ModularAddition(p=p, split=0.8, mode="test")
            vocab_size = p + 2  # numbers 0..p-1, '=', padding
        else:
            raise ValueError(f"Unknown task type: {task_type}")
        
        train_loader = DataLoader(train_ds, batch_size=cfg.get("batch_size", 64), shuffle=True)
        test_loader = DataLoader(test_ds, batch_size=32, shuffle=False)
        
        # Build model with config
        n_embd = cfg.get("n_embd", 64)
        n_head = cfg.get("n_head", 2)
        n_layer = cfg.get("n_layer", 1)
        
        # Ensure heads divide embedding
        if n_embd % n_head != 0 and n_head > 1:
            n_embd = (n_embd // n_head + 1) * n_head
        
        try:
            model = TinyTransformer(
                vocab_size=vocab_size,
                n_embd=n_embd,
                n_head=min(n_head, n_embd),  # Ensure valid head count
                n_layer=n_layer,
                block_size=seq_len + 4,
                dropout=0.0,
                tie_weights=cfg.get("tie_weights", True),
                norm_type=cfg.get("norm_type", "layer"),
            )
            
            params = get_model_params(model)
            print(f"params={params:,}, embd={n_embd}, heads={min(n_head, n_embd)}, layers={n_layer}")
            
            # Train
            final_loss = train_model(
                model, train_loader, 
                epochs=cfg.get("epochs", 50),
                lr=cfg.get("lr", 1e-3),
                device=device
            )
            
            # Evaluate
            eval_result = evaluate_model(model, test_loader, device=device)
            
            result = {
                "name": name,
                "task_type": task_type,
                "params": params,
                "n_embd": n_embd,
                "n_head": min(n_head, n_embd),
                "n_layer": n_layer,
                "tie_weights": cfg.get("tie_weights", True),
                "norm_type": cfg.get("norm_type", "layer"),
                "train_loss": final_loss,
                "test_acc": eval_result["accuracy"],
                "test_loss": eval_result["loss"],
            }
            results.append(result)
            
            # Print key metric
            print(f"  ACC={eval_result['accuracy']:.3f}, Loss={final_loss:.4f}")
            
        except Exception as e:
            import traceback
            print(f" FAILED: {e}")
            traceback.print_exc()
            results.append({
                "name": name,
                "task_type": task_type,
                "params": params if 'params' in locals() else 0,
                "n_embd": n_embd,
                "n_head": min(n_head, n_embd),
                "n_layer": n_layer,
                "tie_weights": cfg.get("tie_weights", True),
                "norm_type": cfg.get("norm_type", "layer"),
                "train_loss": float("inf"),
                "test_acc": 0.0,
                "test_loss": float("inf"),
                "error": str(e),
            })
    
    return results


def main():
    """Run all experiments."""
    import os
    from datetime import datetime
    
    os.makedirs("reports", exist_ok=True)
    report_path = f"reports/minimal_capacity_{''.join([datetime.now().strftime('%Y%m%d_%H%M%S')])}.md"
    
    # ==========================================================================
    # EXPERIMENT 1: PARITY - Width Sweep (Find minimum width for parity)
    # ==========================================================================
    print("\n\n" + "#"*70)
    print("# RESEARCH: MINIMAL CAPACITY SEARCH")
    print("#"*70)
    
    width_configs = [
        {"seq_len": 16, "n_embd": 32, "n_head": 2, "n_layer": 1, "epochs": 100},
        {"seq_len": 16, "n_embd": 48, "n_head": 2, "n_layer": 1, "epochs": 100},
        {"seq_len": 16, "n_embd": 64, "n_head": 2, "n_layer": 1, "epochs": 100},
        {"seq_len": 16, "n_embd": 80, "n_head": 2, "n_layer": 1, "epochs": 100},
        {"seq_len": 16, "n_embd": 96, "n_head": 2, "n_layer": 1, "epochs": 100},
        {"seq_len": 16, "n_embd": 128, "n_head": 4, "n_layer": 1, "epochs": 100},
    ]
    
    width_results = run_contrastive_family("WIDTH_SWEEP_PARITY_1L", width_configs, "parity")
    
    # ==========================================================================
    # EXPERIMENT 2: PARITY - Depth Sweep (Find minimum depth for parity)
    # ==========================================================================
    
    depth_configs = [
        {"seq_len": 16, "n_embd": 64, "n_head": 2, "n_layer": 1, "epochs": 100},
        {"seq_len": 16, "n_embd": 64, "n_head": 2, "n_layer": 2, "epochs": 100},
        {"seq_len": 16, "n_embd": 64, "n_head": 2, "n_layer": 3, "epochs": 100},
        {"seq_len": 16, "n_embd": 64, "n_head": 4, "n_layer": 4, "epochs": 100},
    ]
    
    depth_results = run_contrastive_family("DEPTH_SWEEP_PARITY_64D", depth_configs, "parity")
    
    # ==========================================================================
    # EXPERIMENT 3: PARITY - Sequence Length Scaling (Find phase boundary)
    # ==========================================================================
    
    seq_len_configs = [
        {"seq_len": 8, "n_embd": 32, "n_head": 2, "n_layer": 2, "epochs": 150},
        {"seq_len": 10, "n_embd": 32, "n_head": 2, "n_layer": 2, "epochs": 150},
        {"seq_len": 12, "n_embd": 32, "n_head": 2, "n_layer": 2, "epochs": 150},
        {"seq_len": 14, "n_embd": 32, "n_head": 2, "n_layer": 2, "epochs": 150},
        {"seq_len": 16, "n_embd": 32, "n_head": 2, "n_layer": 2, "epochs": 150},
        {"seq_len": 18, "n_embd": 48, "n_head": 2, "n_layer": 2, "epochs": 150},
        {"seq_len": 20, "n_embd": 48, "n_head": 2, "n_layer": 2, "epochs": 150},
    ]
    
    seq_results = run_contrastive_family("SEQUENCE_LENGTH_SCALING", seq_len_configs, "parity")
    
    # ==========================================================================
    # EXPERIMENT 4: PARITY - Weird Regimes (Should fail but might work?)
    # ==========================================================================
    
    weird_configs = [
        # Standard configs for baseline
        {"seq_len": 16, "n_embd": 32, "n_head": 2, "n_layer": 2, 
         "norm_type": "layer", "tie_weights": True, "epochs": 100},
        
        # Low lr regime (should be slow)
        {"seq_len": 16, "n_embd": 32, "n_head": 2, "n_layer": 2, 
         "lr": 1e-4, "epochs": 300},
        
        # High lr regime (might diverge)  
        {"seq_len": 16, "n_embd": 32, "n_head": 2, "n_layer": 2, 
         "lr": 5e-3, "epochs": 100},
         
        # Single head vs multi-head
        {"seq_len": 16, "n_embd": 64, "n_head": 1, "n_layer": 2, "epochs": 100},
        {"seq_len": 16, "n_embd": 64, "n_head": 4, "n_layer": 2, "epochs": 100},
        
        # No tie weights (should have more params)
        {"seq_len": 16, "n_embd": 32, "n_head": 2, "n_layer": 2, 
         "tie_weights": False, "epochs": 100},
    ]
    
    weird_results = run_contrastive_family("WEIRD_REGIMES", weird_configs, "parity")
    
    # ==========================================================================
    # GENERATE REPORT
    # ==========================================================================
    
    print("\n\n" + "="*70)
    print("GENERATING REPORT")
    print("="*70)
    
    with open(report_path, "w") as f:
        f.write("# Minimal Capacity Search - Research Report\n\n")
        f.write(f"Generated: {datetime.now().isoformat()}\n\n")
        
        # Section 1: Minimum Architecture for Parity
        f.write("## 1. Smallest Competent Architecture (Parity Task)\n\n")
        
        f.write("### Width Sweep (1 layer, varying width)\n\n")
        f.write("| n_embd | Heads | Params | Train Loss | Test Acc | Status |\n")
        f.write("|--------|-------|--------|------------|----------|--------|\n")
        
        for r in sorted(width_results, key=lambda x: x["params"]):
            status = "✓" if r["test_acc"] > 0.9 else "✗" if r["test_acc"] < 0.5 else "~"
            f.write(f"| {r['n_embd']:6d} | {r['n_head']:3d} | {r['params']:8,} | {r['train_loss']:.4f} | {r['test_acc']:.3f} | {status} |\n")
        
        # Find minimum competent model
        competent_width = [r for r in width_results if r["test_acc"] > 0.95]
        if competent_width:
            min_comp = min(competent_width, key=lambda x: x["params"])
            f.write(f"\n**MINIMUM COMPETENT WIDTH**: {min_comp['n_embd']}d ({min_comp['params']:,} params) achieves {min_comp['test_acc']:.1%} accuracy\n")
        
        f.write("\n### Depth Sweep (64-dim, varying depth)\n\n")
        f.write("| Layers | Params | Train Loss | Test Acc | Status |\n")
        f.write("|--------|--------|------------|----------|--------|\n")
        
        for r in sorted(depth_results, key=lambda x: x["n_layer"]):
            status = "✓" if r["test_acc"] > 0.9 else "✗" if r["test_acc"] < 0.5 else "~"
            f.write(f"| {r['n_layer']:6d} | {r['params']:8,} | {r['train_loss']:.4f} | {r['test_acc']:.3f} | {status} |\n")
        
        competent_depth = [r for r in depth_results if r["test_acc"] > 0.95]
        if competent_depth:
            min_comp = min(competent_depth, key=lambda x: x["params"])
            f.write(f"\n**MINIMUM COMPETENT DEPTH**: {min_comp['n_layer']} layers ({min_comp['params']:,} params) achieves {min_comp['test_acc']:.1%} accuracy\n")
        
        # Section 2: Phase Boundary - Sequence Length Scaling
        f.write("\n## 2. Phase Boundary Mapping (Sequence Length)\n\n")
        f.write("Observing when parity becomes unsolvable as sequence length increases.\n\n")
        f.write("| Seq Len | Params | Test Acc | Difficulty |\n")
        f.write("|---------|--------|----------|------------|\n")
        
        for r in sorted(seq_results, key=lambda x: x["seq_len"]):
            diff = "easy" if r["test_acc"] > 0.9 else "medium" if r["test_acc"] > 0.5 else "hard"
            f.write(f"| {r['seq_len']:7d} | {r['params']:8,} | {r['test_acc']:.3f} | {diff} |\n")
        
        # Check for phase transition
        seq_results_sorted = sorted(seq_results, key=lambda x: x["seq_len"])
        acc_diffs = []
        for i in range(1, len(seq_results_sorted)):
            diff = seq_results_sorted[i]["test_acc"] - seq_results_sorted[i-1]["test_acc"]
            if abs(diff) > 0.3:
                acc_diffs.append((seq_results_sorted[i]["seq_len"], diff))
        
        if acc_diffs:
            f.write("\n**NOTABLE PHASE TRANSITIONS**:\n")
            for seq_len, diff in acc_diffs:
                direction = "dropped" if diff < 0 else "improved"
                f.write(f"- At seq_len={seq_len}: accuracy {direction} by {abs(diff):.2f}\n")
        
        # Section 3: Stable Weird Regimes
        f.write("\n## 3. Stable Weird Regimes\n\n")
        f.write("Configs that should break according to standard recipes:\n\n")
        f.write("| Config | Params | LR | Test Acc | Verdict |\n")
        f.write("|--------|--------|----|----------|--------|\n")
        
        for r in weird_results:
            norm_type = r.get("norm_type", "layer")
            tie = r.get("tie_weights", True)
            lr = r.get("lr", 1e-3)
            
            config_str = f"norm={norm_type}, tie={tie}, lr={lr}"
            verdict = ""
            
            if r["test_acc"] > 0.9:
                verdict = "ROBUST ✓"
            elif r["test_acc"] < 0.5 and r.get("error"):
                verdict = "DIVERGED ✗"
            else:
                verdict = "??"
            
            f.write(f"| {config_str} | {r['params']:8,} | {lr:.2e} | {r['test_acc']:.3f} | {verdict} |\n")
        
        # Find surprising survivors
        weird_survivors = [r for r in weird_results if r["test_acc"] > 0.85]
        if weird_survivors:
            f.write("\n**SURPRISINGLY ROBUST CONFIGS**:\n")
            for r in weird_survivors:
                lr = r.get("lr", 1e-3)
                tie = r.get("tie_weights", True)
                verdict = ""
                
                if lr > 1e-2:
                    verdict = "(high LR)"
                elif not tie:
                    verdict = "(untied weights)"
                    
                f.write(f"- {r['params']:,} params, acc={r['test_acc']:.1%} {verdict}\n")
        
        # Summary section
        f.write("\n## Summary of Discoveries\n\n")
        
        # Minimum competent model
        all_parity = width_results + depth_results
        if all_parity:
            min_params = min(all_parity, key=lambda x: x["params"])
            best_acc = max(all_parity, key=lambda x: x["test_acc"])
            
            f.write(f"- **Smallest model**: {min_params['params']:,} params\n")
            f.write(f"  - Config: embd={min_params['n_embd']}, heads={min_params['n_head']}, layers={min_params['n_layer']}\n")
            f.write(f"- **Best accuracy**: {best_acc['test_acc']:.1%} ({best_acc['params']:,} params)\n")
        
        # Phase transition insight
        if seq_results:
            good_seqs = [r for r in seq_results if r["test_acc"] > 0.9]
            hard_seqs = [r for r in seq_results if r["test_acc"] < 0.6]
            
            if good_seqs and hard_seqs:
                max_easy = max(r["seq_len"] for r in good_seqs)
                min_hard = min(r["seq_len"] for r in hard_seqs)
                f.write(f"- **Phase boundary**: Parity is easy up to seq_len={max_easy}, becomes hard at {min_hard}\n")
        
        # Weird regime findings
        if weird_survivors:
            f.write(f"- **Stable weird regimes found**: {len(weird_survivors)} configs that should fail but work\n")
        
    print(f"\nReport written to: {report_path}")
    
    # Print terminal summary
    print("\n" + "="*70)
    print("EXPERIMENT SUMMARY")
    print("="*70)
    
    print("\n[1] WIDTH SWEEP (1-layer parity):")
    competent = [r for r in width_results if r["test_acc"] > 0.95]
    if competent:
        min_c = min(competent, key=lambda x: x["params"])
        print(f"   ✓ Minimum competent: {min_c['n_embd']}d, {min_c['params']:,} params")
    else:
        best = max(width_results, key=lambda x: x["test_acc"])
        print(f"   ✗ No config >95% acc; best={best['test_acc']:.1%} ({best['params']:,} params)")
    
    print("\n[2] DEPTH SWEEP (64-dim):")
    competent = [r for r in depth_results if r["test_acc"] > 0.95]
    if competent:
        min_c = min(competent, key=lambda x: x["params"])
        print(f"   ✓ Minimum competent: {min_c['n_layer']} layers, {min_c['params']:,} params")
    else:
        best = max(depth_results, key=lambda x: x["test_acc"])
        print(f"   ✗ No config >95% acc; best={best['test_acc']:.1%}")
    
    print("\n[3] SEQUENCE LENGTH SCALING:")
    if seq_results:
        for r in sorted(seq_results, key=lambda x: x["seq_len"]):
            stars = "█" * int(r["test_acc"] * 5) + "░" * (5 - int(r["test_acc"] * 5))
            print(f"   {r['seq_len']:2d}: {stars} {r['test_acc']:.1%}")
    
    print("\n[4] WEIRD REGIMES:")
    robust = [r for r in weird_results if r["test_acc"] > 0.85]
    print(f"   Surviving: {len(robust)}/{len(weird_results)} configs")


if __name__ == "__main__":
    main()