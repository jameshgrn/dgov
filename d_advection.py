"""D-Advection Branch: Phase Transition and Emergent Behavior Discovery."""

import torch
from src.data.toy_tasks import ModularAddition, get_dataloader
from src.models.transformer import TinyTransformer
import torch.optim as optim
import json
import os
from dataclasses import dataclass
from typing import List, Dict, Any


@dataclass
class ExperimentResult:
    config: Dict[str, Any]
    params: int
    train_loss: float
    test_acc: float
    converged: bool
    learning_trajectory: List[float]


def count_params(model: TinyTransformer) -> int:
    return sum(p.numel() for p in model.parameters())


def run_single_experiment(
    n_embd: int,
    n_layer: int = 1,
    n_head: int = 1,
    p: int = 113,
    epochs: int = 200,
    lr: float = 1e-3,
    seed: int = 42,
) -> ExperimentResult:
    """Run a single experiment and track learning trajectory."""
    
    train_ds = ModularAddition(p=p, split=0.8, mode="train", seed=seed)
    test_ds = ModularAddition(p=p, split=0.8, mode="test", seed=seed)
    vocab_size = p + 2

    train_loader = get_dataloader(train_ds, batch_size=32)
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

    optimizer = optim.AdamW(model.parameters(), lr=lr)
    
    device = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
    model.to(device)

    learning_trajectory = []
    best_acc = 0.0
    converged = True
    
    for epoch in range(epochs):
        model.train()
        total_loss = 0
        for x, y in train_loader:
            x, y = x.to(device), y.to(device)
            _, loss = model(x, targets=y)
            
            if torch.isnan(loss) or torch.isinf(loss):
                converged = False
                break
                
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        if not converged:
            learning_trajectory.append(float(loss))
            break

        # Track accuracy every 10 epochs
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
            learning_trajectory.append(avg_loss)
    
    return ExperimentResult(
        config={
            "n_embd": n_embd,
            "n_layer": n_layer,
            "n_head": n_head,
            "params": count_params(model),
        },
        params=count_params(model),
        train_loss=learning_trajectory[-1] if learning_trajectory else float("nan"),
        test_acc=best_acc,
        converged=converged and best_acc > 0.5,
        learning_trajectory=learning_trajectory,
    )


def find_phase_boundary():
    """Search for phase boundary where capability emerges."""
    
    print("\n" + "=" * 70)
    print("EXPERIMENT: Phase Boundary Discovery")
    print("=" * 70)
    
    results = []
    
    # Sweep depth with fixed small width
    print("\n--- Sweep 1: Minimal Width, Variable Depth ---")
    for n_layer in range(1, 8):
        result = run_single_experiment(n_embd=32, n_layer=n_layer, n_head=1, epochs=100)
        results.append(result)
        conv_str = "✓" if result.converged else "✗"
        print(f"  layers={n_layer:2d} | acc={result.test_acc:.3f} {conv_str} | "
              f"params={result.params:,}")
    
    # Sweep width with single layer (finding minimal competent width)
    print("\n--- Sweep 2: Single Layer, Variable Width ---")
    for n_embd in [16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128]:
        result = run_single_experiment(n_embd=n_embd, n_layer=1, n_head=1, epochs=150)
        results.append(result)
        conv_str = "✓" if result.converged else "✗"
        print(f"  dim={n_embd:3d} | acc={result.test_acc:.3f} {conv_str} | "
              f"params={result.params:,}")
    
    # Find the phase boundary - where does 95% accuracy emerge?
    high_performers = [r for r in results if r.test_acc >= 0.95 and r.converged]
    
    if high_performers:
        min_params_high = min(high_performers, key=lambda x: x.params)
        print(f"\n*** PHASE BOUNDARY FOUND ***")
        print(f"Minimal config achieving 95%+ accuracy: "
              f"{min_params_high.params:,} params")
        print(f"Config: layers={min_params_high.config['n_layer']}, "
              f"dim={min_params_high.config['n_embd']}")
    else:
        print("\n*** NO PHASE BOUNDARY (95% threshold not reached) ***")
        # Report best achieved
        best = max(results, key=lambda x: x.test_acc)
        print(f"Best achieved: {best.params:,} params, acc={best.test_acc:.3f}")
    
    return results


def test_weird_regimes():
    """Test unstable-looking configurations that might work."""
    
    print("\n" + "=" * 70)
    print("EXPERIMENT: Stable Weird Regimes")
    print("=" * 70)
    
    results = []
    
    # Very deep and narrow (extreme depth-first approach)
    print("\n--- Regime 1: Extreme Depth/Narrow Width ---")
    for n_embd in [16, 24]:
        for n_layer in [8, 12, 16]:
            result = run_single_experiment(
                n_embd=n_embd, n_layer=n_layer, n_head=1, epochs=300
            )
            results.append(result)
            conv_str = "✓" if result.converged else "✗"
            print(f"  dim={n_embd:2d} layers={n_layer:2d} | acc={result.test_acc:.3f} {conv_str}")
    
    # Very wide single layer (beyond typical dimensions)
    print("\n--- Regime 2: Extreme Width Single Layer ---")
    for n_embd in [160, 192, 256]:
        result = run_single_experiment(
            n_embd=n_embd, n_layer=1, n_head=1, epochs=200
        )
        results.append(result)
        conv_str = "✓" if result.converged else "✗"
        print(f"  dim={n_embd:3d} layers=1 | acc={result.test_acc:.3f} {conv_str}")
    
    # Single head vs multi-head confusion test
    print("\n--- Regime 3: Head Configuration Extremes ---")
    for n_head in [1, 2, 4]:
        for n_embd in [64]:
            try:
                result = run_single_experiment(
                    n_embd=n_embd, n_layer=2, n_head=n_head, epochs=150
                )
                results.append(result)
                conv_str = "✓" if result.converged else "✗"
                print(f"  dim={n_embd} heads={n_head} layers=2 | acc={result.test_acc:.3f} {conv_str}")
            except Exception as e:
                print(f"  dim={n_embd} heads={n_head} layers=2 | ERROR: {e}")
    
    # Zero-depth equivalent (just embeddings + head, no transformer blocks)
    print("\n--- Regime 4: No Transformer Layers ---")
    for n_layer in [0]:  # Edge case - what if we remove all layers?
        try:
            result = run_single_experiment(
                n_embd=64, n_layer=n_layer, n_head=1, epochs=200
            )
            results.append(result)
            conv_str = "✓" if result.converged else "✗"
            print(f"  dim=64 layers={n_layer} | acc={result.test_acc:.3f} {conv_str}")
        except Exception as e:
            print(f"  dim=64 layers={n_layer} | ERROR: {e}")
    
    return results


def test_attention_patterns():
    """Test if attention mechanism is even being used."""
    
    print("\n" + "=" * 70)
    print("EXPERIMENT: Attention Usage Discovery")
    print("=" * 70)
    
    results = []
    
    # Single-head vs multi-head comparison
    print("\n--- Sweep: Head Count (same total width) ---")
    for n_head, n_embd in [(1, 64), (2, 64), (4, 64)]:
        result = run_single_experiment(n_embd=n_embd, n_layer=2, n_head=n_head, epochs=150)
        results.append(result)
        conv_str = "✓" if result.converged else "✗"
        print(f"  heads={n_head} | dim={n_embd} | acc={result.test_acc:.3f} {conv_str}")
    
    # Test if attention actually helps vs FFN-only equivalent
    print("\n--- Contrast: Attention-Heavy vs Width-Equivalent ---")
    configs = [
        (64, 2, 1),   # Standard small
        (96, 1, 1),   # Wider single layer (attention-heavy)
        (64, 1, 1),   # Narrower single layer  
    ]
    
    for n_embd, n_layer, n_head in configs:
        result = run_single_experiment(n_embd=n_embd, n_layer=n_layer, n_head=n_head, epochs=200)
        results.append(result)
        conv_str = "✓" if result.converged else "✗"
        print(f"  dim={n_embd:3d} layers={n_layer} heads={n_head} | acc={result.test_acc:.3f} {conv_str}")
    
    return results


def generate_report(results: List[ExperimentResult], report_path: str):
    """Generate phase transition research report."""
    
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    
    with open(report_path, "w") as f:
        f.write("# D-Advection Branch: Phase Transitions and Emergent Behavior\n\n")
        
        f.write("## Research Agenda\n- **Smallest Competent Architecture**: Find minimal parameters for modular addition\n- **Phase Boundary Mapping**: Identify exact capability emergence point\n- **Stable Weird Regimes**: Discover surprising working configurations\n\n")
        
        # Organize results by experiment type
        all_results = results
        
        # Phase boundary section
        f.write("## 1. Phase Boundary Discovery\n\n")
        
        converged = [r for r in all_results if r.converged]
        
        # Find minimal config achieving different accuracy thresholds
        thresholds = [0.5, 0.7, 0.9, 0.95]
        f.write("### Minimum Parameters for Accuracy Thresholds\n\n")
        f.write("| Accuracy Threshold | Min Params | Best Config |\n")
        f.write("|-------------------|------------|-------------|\n")
        
        for threshold in thresholds:
            qualified = [r for r in converged if r.test_acc >= threshold]
            if qualified:
                best = min(qualified, key=lambda x: x.params)
                f.write(
                    f"| {threshold:.0%} | {best.params:,} | "
                    f"layers={best.config['n_layer']}, dim={best.config['n_embd']}, "
                    f"heads={best.config['n_head']} |\n"
                )
            else:
                # Find closest
                closest = max(all_results, key=lambda x: x.test_acc)
                f.write(
                    f"| {threshold:.0%} | N/A (max {closest.test_acc:.0%}) | "
                    f"layers={closest.config['n_layer']}, dim={closest.config['n_embd']} |\n"
                )
        
        # Parametric sweep heatmap
        f.write("\n### Depth vs Width Sweeps\n\n")
        
        # Get unique layer counts and dims for the table
        layers_found = sorted(set(r.config["n_layer"] for r in all_results))
        dims_found = sorted(set(r.config["n_embd"] for r in all_results))
        
        f.write("| Layers \\ Dim | 16 | 24 | 32 | 40 | 48 | 56 | 64 | 80 | 96 | 112 | 128 |\n")
        f.write("|---------------|----|----|----|----|----|----|----|----|----|-----|-----|\n")
        
        for layer in layers_found:
            row = f"| {layer} |"
            for dim in dims_found:
                matching = [r for r in all_results 
                           if r.config["n_layer"] == layer and r.config["n_embd"] == dim]
                if matching:
                    acc = max(r.test_acc for r in matching)
                    cell = f"{acc:.0%}"
                else:
                    cell = "-"
                row += f" {cell:>4} |"
            f.write(row + "\n")
        
        # Weird regimes section
        f.write("\n## 2. Stable Weird Regimes\n\n")
        
        weird_results = [r for r in all_results if r.config["n_layer"] >= 8 or 
                        (r.config["n_layer"] == 1 and r.config["n_embd"] > 150)]
        
        if weird_results:
            f.write("### Extreme Configurations That Worked\n\n")
            for r in sorted(weird_results, key=lambda x: x.test_acc, reverse=True)[:10]:
                conv = "✓" if r.converged else "✗"
                f.write(f"- dim={r.config['n_embd']:3d}, layers={r.config['n_layer']:2d}: "
                       f"acc={r.test_acc:.3f} {conv}\n")
        else:
            f.write("No extreme regimes achieved notable success.\n")
        
        # Attention patterns
        f.write("\n## 3. Attention Usage Analysis\n\n")
        
        head_configs = [r for r in all_results if "n_head" in str(r.config)]
        f.write("### Head Count Comparison\n\n")
        f.write("| Heads | Dim | Layers | Acc |\n")
        f.write("|-------|-----|--------|-----|\n")
        
        for r in head_configs:
            f.write(f"| {r.config['n_head']} | {r.config['n_embd']} | "
                   f"{r.config['n_layer']} | {r.test_acc:.3f} |\n")
        
        # Key findings
        f.write("\n## Key Findings\n\n")
        
        if converged:
            min_config = min(converged, key=lambda x: x.params)
            best_config = max(converged, key=lambda x: x.test_acc)
            
            f.write(f"1. **Minimal Competent Architecture**: {min_config.params:,} parameters "
                   f"achieve acc={min_config.test_acc:.3f}\n")
            f.write(f"   Config: layers={min_config.config['n_layer']}, "
                   f"dim={min_config.config['n_embd']}, heads={min_config.config['n_head']}\n\n")
            
            f.write(f"2. **Best Performance**: {best_config.params:,} parameters achieve "
                   f"acc={best_config.test_acc:.3f}\n")
            f.write(f"   Config: layers={best_config.config['n_layer']}, "
                   f"dim={best_config.config['n_embd']}, heads={best_config.config['n_head']}\n\n")
        
        # Efficiency analysis
        if converged:
            efficiency = [(r.params / (r.test_acc + 1e-6), r) for r in converged]
            most_efficient = min(efficiency, key=lambda x: x[0])[1]
            
            f.write(f"3. **Most Parameter-Efficient**: {most_efficient.params:,} params for "
                   f"acc={most_efficient.test_acc:.3f}\n\n")
        
        # Phase transition insights
        f.write("4. **Phase Transition Observations**:\n\n")
        
        # Look for sudden capability jumps in the depth sweep
        depth_configs = sorted(
            [r for r in all_results if r.config["n_embd"] == 32],
            key=lambda x: x.config["n_layer"]
        )
        
        if len(depth_configs) >= 2:
            for i in range(len(depth_configs) - 1):
                prev = depth_configs[i]
                curr = depth_configs[i + 1]
                acc_jump = curr.test_acc - prev.test_acc
                
                if acc_jump > 0.15:  # Significant jump
                    f.write(f"   - **Sudden capability emergence**: "
                           f"layers {prev.config['n_layer']}→{curr.config['n_layer']} "
                           f"(acc: {prev.test_acc:.3f} → {curr.test_acc:.3f}, "
                           f"+{acc_jump:.3f})\n")
        
        if not any(
            r.config["n_embd"] == 32 for r in depth_configs
        ):
            # General observation about depth
            best_shallow = max([r for r in all_results if r.config["n_layer"] == 1], 
                             key=lambda x: x.test_acc)
            best_deep = max([r for r in all_results if r.config["n_layer"] >= 3], 
                           key=lambda x: x.test_acc if r.converged else 0)
            
            if best_deep.test_acc > best_shallow.test_acc:
                f.write(f"   - **Depth provides advantage**: Best single-layer acc={best_shallow.test_acc:.3f}, "
                       f"best multi-layer acc={best_deep.test_acc:.3f}\n")
        
        # Conclusion
        f.write("\n## Conclusion\n\n")
        f.write(f"This experiment discovered the phase boundaries for modular addition in tiny transformers.\n")
        f.write(f"Key insights:\n\n")
        
        total_configs = len(all_results)
        converged_count = len(converged)
        converge_rate = converged_count / total_configs * 100 if total_configs else 0
        
        f.write(f"- Tested {total_configs} unique configurations\n")
        f.write(f"- Converged in {converge_rate:.1f}% of cases ({converged_count}/{total_configs})\n")
        
        max_acc = max(r.test_acc for r in all_results) if all_results else 0
        min_params_at_max = min(
            (r.params for r in all_results if r.test_acc == max_acc), default=0
        )
        f.write(f"- Maximum accuracy achieved: {max_acc:.3f}\n")
        f.write(f"- Minimum parameters at peak performance: {min_params_at_max:,}\n")


def main():
    """Run all experiments and generate report."""
    
    print("\n" + "=" * 70)
    print("D-ADVECTION BRANCH: PHASE TRANSITION DISCOVERY")
    print("=" * 70)
    
    # Run phase boundary experiment
    phase_results = find_phase_boundary()
    
    # Run weird regime experiments  
    weird_results = test_weird_regimes()
    
    # Run attention pattern experiments
    attention_results = test_attention_patterns()
    
    # Combine all results
    all_results = phase_results + weird_results + attention_results
    
    print("\n" + "=" * 70)
    print("EXPERIMENT COMPLETE")
    print("=" * 70)
    print(f"Total configurations tested: {len(all_results)}")
    print(f"Converged: {sum(1 for r in all_results if r.converged)}")
    
    # Generate report
    report_path = (
        "/Users/jakegearon/projects/tinymodels/.dgov/reports/d-advection-phase-transitions.md"
    )
    generate_report(all_results, report_path)
    
    print(f"\nReport written to: {report_path}")


if __name__ == "__main__":
    main()