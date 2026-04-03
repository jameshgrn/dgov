"""
River Physics Phase Scan: Discovering Emergent Competence Boundaries

This experiment maps the phase space of neural operators for river morphodynamics.
We discover where learning becomes possible (phase transition points) across:
- Resolution: Does competence emerge at certain grid sizes?
- Model capacity: What's the minimum dimension/depth needed?
- Hydrograph complexity: How many peaks before failure?

Protocol: Remote cluster compute on River L40S GPUs via rsync + ssh.
"""

import os
import sys
import json
import argparse
import torch
import torch.optim as optim
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.models.physics_model import MassConservingTransformer
from src.data.river_physics import HydrographDataset, get_hydro_dataloader


def evaluate_model(model, dataloader, device):
    """Evaluate model on test set and return MSE."""
    model.eval()
    total_loss = 0
    with torch.no_grad():
        for x, y, mask in dataloader:
            x, y, mask = x.to(device), y.to(device), mask.to(device)
            _, loss, _ = model(x, y, mask=mask)
            total_loss += loss.item()
    return total_loss / len(dataloader)


def run_phase_point(args):
    """Run a single phase space point."""
    
    # Set up dataset with specific configuration
    n_nodes = args.resolution
    
    # Generate training data at this resolution
    train_ds = HydrographDataset(
        min_nodes=n_nodes, 
        max_nodes=n_nodes, 
        num_samples=1000,
        hydrograph_complexity=args.hydrograph_complexity
    )
    
    # Generate smaller test set
    test_ds = HydrographDataset(
        min_nodes=n_nodes,
        max_nodes=n_nodes,
        num_samples=200
    )
    
    train_dl = torch.utils.data.DataLoader(train_ds, batch_size=args.batch_size, shuffle=True)
    test_dl = torch.utils.data.DataLoader(test_ds, batch_size=32)
    
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    # Create model with specified architecture
    model = MassConservingTransformer(
        n_embd=args.n_embd,
        n_layer=args.n_layer,
    )
    model.to(device)
    
    optimizer = optim.AdamW(model.parameters(), lr=args.lr)
    
    # Training loop
    best_test_loss = float('inf')
    losses_history = []
    
    for epoch in range(args.epochs):
        model.train()
        epoch_losses = []
        
        for x, y, mask in tqdm(train_dl, desc=f"Epoch {epoch}", leave=False, unit="batch"):
            x, y, mask = x.to(device), y.to(device), mask.to(device)
            _, loss, _ = model(x, y, mask=mask)
            
            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            epoch_losses.append(loss.item())
        
        avg_train_loss = sum(epoch_losses) / len(epoch_losses)
        
        # Periodic evaluation (every 5 epochs or final)
        if (epoch + 1) % args.eval_freq == 0 or epoch == args.epochs - 1:
            test_loss = evaluate_model(model, test_dl, device)
            losses_history.append((epoch, avg_train_loss, test_loss))
            
            if test_loss < best_test_loss:
                best_test_loss = test_loss
            
            print(f"Res {n_nodes}: E{epoch:3d} Train: {avg_train_loss:.6f} Test: {test_loss:.6f}")
    
    return {
        "config": args.phase_name,
        "resolution": n_nodes,
        "architecture": {
            "n_embd": args.n_embd,
            "n_layer": args.n_layer,
        },
        "hydrograph_complexity": args.hydrograph_complexity,
        "best_test_loss": best_test_loss,
        "training_history": losses_history,
        "final_mse": best_test_loss,
    }


def main(args):
    """Run phase scan across hyperparameter combinations."""
    
    # Phase space to explore
    phases = []
    
    if args.phase == "all" or args.phase == "resolution_scan":
        for res in args.resolutions:
            phases.append({
                "phase_name": f"res_{res}_embd128_layers4",
                "resolution": res,
                "n_embd": 128,
                "n_layer": 4,
            })
    
    if args.phase == "all" or args.phase == "capacity_scan":
        for embd in args.n_embeds:
            for layer in args.n_layers:
                phases.append({
                    "phase_name": f"embd{embd}_layer{layer}_res256",
                    "resolution": 256,
                    "n_embd": embd,
                    "n_layer": layer,
                })
    
    if args.phase == "all" or args.phase == "complexity_scan":
        for hydro in args.hydrograph_complexities:
            phases.append({
                "phase_name": f"hydro{hydro}_res256_embd128_layers4",
                "resolution": 256,
                "n_embd": 128,
                "n_layer": 4,
                "hydrograph_complexity": hydro,
            })
    
    if args.phase == "single":
        phases.append({
            "phase_name": args.single_phase_name,
            "resolution": args.resolution,
            "n_embd": args.n_embd,
            "n_layer": args.n_layer,
            "hydrograph_complexity": args.hydrograph_complexity,
        })
    
    print(f"\n{'='*60}")
    print(f"PHASE SCAN: {len(phases)} configurations")
    print(f"Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU'}")
    print(f"{'='*60}\n")
    
    results = []
    
    for i, phase in enumerate(phases):
        print(f"\n{'#'*60}")
        print(f"# PHASE {i+1}/{len(phases)}: {phase['phase_name']}")
        print(f"{'#'*60}\n")
        
        # Create args namespace for this configuration
        phase_args = argparse.Namespace(
            n_embd=phase['n_embd'],
            n_layer=phase['n_layer'],
            resolution=phase['resolution'],
            hydrograph_complexity=phase.get('hydrograph_complexity', 2),
            batch_size=args.batch_size,
            epochs=args.epochs,
            lr=args.lr,
            eval_freq=args.eval_freq,
            phase_name=phase['phase_name']
        )
        
        try:
            result = run_phase_point(phase_args)
            results.append(result)
            
            # Save intermediate results
            os.makedirs("reports", exist_ok=True)
            with open(f"reports/phase_scan_results.json", "w") as f:
                json.dump(results, f, indent=2)
            
        except Exception as e:
            print(f"ERROR in phase {phase['phase_name']}: {e}")
            results.append({
                "config": phase['phase_name'],
                "error": str(e),
            })
    
    # Final summary
    os.makedirs("reports", exist_ok=True)
    with open("reports/phase_scan_results.json", "w") as f:
        json.dump(results, f, indent=2)
    
    print(f"\n{'='*60}")
    print(f"PHASE SCAN COMPLETE: {len(results)} configurations evaluated")
    
    # Print summary
    successes = [r for r in results if 'error' not in r]
    if successes:
        best = min(successes, key=lambda x: x['final_mse'])
        print(f"\nBEST CONFIGURATION:")
        print(f"  Config: {best['config']}")
        print(f"  Resolution: {best['resolution']}")
        print(f"  Architecture: n_embd={best['architecture']['n_embd']}, n_layer={best['architecture']['n_layer']}")
        print(f"  Best MSE: {best['final_mse']:.2e}")
    
    print(f"{'='*60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="River Physics Phase Scan")
    
    # Phase selection
    parser.add_argument("--phase", type=str, default="all", choices=["all", "resolution_scan", "capacity_scan", 
                                                                       "complexity_scan", "single"])
    
    # Resolution sweep (for resolution_scan phase)
    parser.add_argument("--resolutions", nargs="+", type=int, default=[64, 128, 256, 512])
    
    # Capacity sweep (for capacity_scan phase)
    parser.add_argument("--n_embeds", nargs="+", type=int, default=[64, 128, 256])
    parser.add_argument("--n_layers", nargs="+", type=int, default=[2, 4, 6])
    
    # Hydrograph complexity sweep (for complexity_scan phase)
    parser.add_argument("--hydrograph_complexities", nargs="+", type=int, default=[1, 2, 3])
    
    # Single point run
    parser.add_argument("--single_phase_name", type=str, default="test_phase")
    parser.add_argument("--resolution", type=int, default=256)
    parser.add_argument("--n_embd", type=int, default=128)
    parser.add_argument("--n_layer", type=int, default=4)
    parser.add_argument("--hydrograph_complexity", type=int, default=2)
    
    # Training hyperparameters
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--eval_freq", type=int, default=5)
    
    args = parser.parse_args()
    main(args)