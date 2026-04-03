"""CLI for running architecture experiments."""

import sys


def main():
    """Run architecture miniaturization experiments."""
    from .experiment import run_architecture_search, ExperimentResult

    print("=" * 60)
    print("ARCHITECTURE MINIATURIZATION EXPLORATION")
    print("=" * 60)

    results = run_architecture_search()

    # Analyze results
    print("\n" + "=" * 60)
    print("SUMMARY OF FINDINGS")
    print("=" * 60)

    # Best by accuracy for each search dimension
    depth_best = min([r for r in results if "Layers=" in str(r.config)], key=lambda x: -x.test_acc)
    width_best = min([r for r in results if "Dim=" in str(r.config)], key=lambda x: -x.test_acc)

    print(f"\nBest depth configuration:")
    print(f"  Parameters: {depth_best.num_params:,}")
    print(f"  Layers: {depth_best.config.num_layers}")
    print(f"  Hidden dim: {depth_best.config.hidden_dim}")
    print(f"  Test accuracy: {depth_best.test_acc:.3f}")

    print(f"\nBest width configuration:")
    print(f"  Parameters: {width_best.num_params:,}")
    print(f"  Layers: {width_best.config.num_layers}")
    print(f"  Hidden dim: {width_best.config.hidden_dim}")
    print(f"  Test accuracy: {width_best.test_acc:.3f}")

    # Find smallest model with >50% accuracy (random baseline)
    viable = [r for r in results if r.test_acc > 0.5]
    if viable:
        smallest = min(viable, key=lambda x: x.num_params)
        print(f"\nSmallest viable model (>50% acc):")
        print(f"  Parameters: {smallest.num_params:,}")
        print(f"  Layers: {smallest.config.num_layers}")
        print(f"  Hidden dim: {smallest.config.hidden_dim}")
        print(f"  Test accuracy: {smallest.test_acc:.3f}")

    # Write report
    report_path = "/Users/jakegearon/projects/tinymodels/.dgov/reports/branch-a-architecture.md"
    import os
    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    with open(report_path, "w") as f:
        f.write("# Branch A: Architecture Miniaturization Report\n\n")
        f.write("## Task\nExplore width, depth, attention heads, MLP ratio, normalization strategy,\nresidual scaling, positional encoding choices, tied vs untied embeddings.\nLook for sudden jumps in in-context behavior and architecture simplifications.\n\n")

        f.write("## Methods\n")
        f.write("- Synthetic dataset with parity, repetition, and majority tasks\n")
        f.write("- Systematic search over architecture hyperparameters\n")
        f.write("- 5 training epochs per configuration (fast convergence)\n\n")

        f.write("## Results\n\n")
        for r in results:
            f.write(f"### Configuration: {str(r.config)[:100]}...\n")
            f.write(f"- Parameters: {r.num_params:,}\n")
            f.write(f"- Test accuracy: {r.test_acc:.3f}\n\n")

    print(f"\nReport written to: {report_path}")


if __name__ == "__main__":
    main()