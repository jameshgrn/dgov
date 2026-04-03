"""Branch B: Optimizer and training-rule mutation experiments.

Search over:
- Optimizer family (Adam, AdamW, SGD, RMSprop, Adagrad)
- Learning rate schedules (constant, step decay, exponential, cosine, warmup-cosine)
- Weight decay rules (none, fixed, adaptive, layerwise)
- Gradient clipping (none, value clip, norm clip)
- Noise injection (none, gradient noise, parameter noise)

Identify instability boundaries and qualitative changes in error types.
"""

import os
from dataclasses import dataclass
from typing import Any
from datetime import datetime

import torch
from torch.utils.data import DataLoader

# Import from tinymodels package
sys_path = "/Users/jakegearon/projects/tinymodels"
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from tinymodels.model import TransformerConfig, TinyTransformer
from tinymodels.experiment import SyntheticDataset, evaluate_model as base_evaluate
from tinymodels.training_rules import (
    OptimizerFamily,
    LRSchedule,
    WeightDecayRule,
    GradientClipping,
    NoiseInjection,
    TrainingRules,
    create_training_loop,
)


@dataclass
class MutationResult:
    """Results from a training rule mutation configuration."""

    rules: TrainingRules
    num_params: int
    final_train_loss: float | None
    test_acc: float
    stability: bool
    max_grad_norm: float
    clipping_ratio: float
    steps_to_instability: int | None = None
    error_type: str | None = None


class InstabilityDetector:
    """Track training stability and error types."""

    def __init__(self, patience: int = 10):
        self.losses: list[float] = []
        self.patience = patience
        self.convergence_count = 0

    def check(self, loss: float) -> tuple[bool, str | None]:
        """Check for instability. Returns (is_stable, error_type)."""
        if torch.isnan(torch.tensor(loss)) or torch.isinf(torch.tensor(loss)):
            return False, "nan_or_inf"

        self.losses.append(loss)

        # Check for divergence: loss increasing rapidly
        if len(self.losses) >= 20:
            recent = self.losses[-10:]
            if all(recent[i] > recent[i - 1] * 1.1 for i in range(1, len(recent))):
                return False, "divergence"

        # Check for loss explosion
        if loss > 1e6:
            return False, "loss_explosion"

        # Track convergence
        if len(self.losses) >= 30 and len(set([round(l, 4) for l in self.losses[-15:]])) == 1:
            self.convergence_count += 1
            if self.convergence_count >= self.patience:
                return True, "premature_convergence"

        self.convergence_count = max(0, self.convergence_count - 1)
        return True, None


def evaluate_mutation(
    model: TinyTransformer,
    rules: TrainingRules,
    dataset: SyntheticDataset,
    total_steps: int = 500,
    instability_detector: InstabilityDetector | None = None,
) -> MutationResult:
    """Evaluate a single mutation configuration."""

    if instability_detector is None:
        instability_detector = InstabilityDetector()

    loader = DataLoader(dataset, batch_size=32, shuffle=True)
    criterion = torch.nn.CrossEntropyLoss()

    train_step, get_state, config_str = create_training_loop(
        model, rules, loader, criterion, total_steps
    )

    # Training with instability detection
    final_loss = None
    steps_to_instability = None
    error_type = None

    for step in range(total_steps):
        batch = next(iter(loader))
        loss = train_step(batch, step)

        is_stable, err_type = instability_detector.check(loss)

        if not is_stable:
            steps_to_instability = step
            error_type = err_type
            break

        final_loss = loss

    # Get state metrics
    state = get_state()

    test_acc = base_evaluate(model, dataset, num_batches=20) if final_loss is not None else 0.0

    return MutationResult(
        rules=rules,
        num_params=model.num_parameters(),
        final_train_loss=final_loss,
        test_acc=test_acc,
        stability=is_stable if final_loss is not None else False,
        max_grad_norm=state["mean_grad_norm"],
        clipping_ratio=state.get("clip_ratio", 0.0),
        steps_to_instability=steps_to_instability,
        error_type=error_type,
    )


def sweep_learning_rates(
    opt_family: OptimizerFamily,
    lr_schedule: LRSchedule,
    weight_decay_rule: WeightDecayRule = WeightDecayRule.FIXED,
) -> list[MutationResult]:
    """Search over learning rates for a given optimizer and schedule."""

    print(f"\n{'='*60}")
    print(f"LR SWEEP: {opt_family.value} | {lr_schedule.value}")
    print(f"{'='*60}")

    model_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        tie_embeddings=True,
        max_seq_len=32,
    )
    dataset = SyntheticDataset(vocab_size=256, seq_len=32)

    results = []

    # Fine-grained LR sweep
    lr_range = [1e-5, 3e-5, 1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1]

    for lr in lr_range:
        config = TrainingRules(
            optimizer=opt_family,
            lr=lr,
            lr_schedule=lr_schedule,
            weight_decay_rule=weight_decay_rule,
            weight_decay=0.01,
            gradient_clipping=GradientClipping.NORM_CLIP,
            clip_value=1.0,
            noise_injection=NoiseInjection.NONE,
        )

        model = TinyTransformer(model_config)
        result = evaluate_mutation(model, config, dataset, total_steps=500)

        status = "✓" if result.stability else f"✗ ({result.error_type})"
        print(f"  lr={lr:.4e} | loss={result.final_train_loss:.4f if result.final_train_loss is not None else 'N/A':6s} | acc={result.test_acc:.3f} | {status}")

        results.append(result)

    return results


def sweep_optimizer_family() -> list[MutationResult]:
    """Search over optimizer families."""

    print(f"\n{'='*60}")
    print(f"OPTIMIZER FAMILY SWEEP")
    print(f"{'='*60}")

    model_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        tie_embeddings=True,
        max_seq_len=32,
    )
    dataset = SyntheticDataset(vocab_size=256, seq_len=32)

    results = []
    lr_schedule = LRSchedule.COSINE_ANNEALING

    for opt in OptimizerFamily:
        config = TrainingRules(
            optimizer=opt,
            lr=0.001,
            lr_schedule=lr_schedule,
            weight_decay_rule=WeightDecayRule.FIXED,
            weight_decay=0.01,
            gradient_clipping=GradientClipping.NORM_CLIP,
            clip_value=1.0,
            noise_injection=NoiseInjection.NONE,
        )

        model = TinyTransformer(model_config)
        result = evaluate_mutation(model, config, dataset, total_steps=500)

        status = "✓" if result.stability else f"✗ ({result.error_type})"
        print(f"  {opt.value:16s} | loss={result.final_train_loss:.4f if result.final_train_loss is not None else 'N/A':6s} | acc={result.test_acc:.3f} | {status}")

        results.append(result)

    return results


def sweep_lr_schedules() -> list[MutationResult]:
    """Search over learning rate schedules."""

    print(f"\n{'='*60}")
    print(f"LR SCHEDULE SWEEP")
    print(f"{'='*60}")

    model_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        tie_embeddings=True,
        max_seq_len=32,
    )
    dataset = SyntheticDataset(vocab_size=256, seq_len=32)

    results = []
    optimizer = OptimizerFamily.ADAMW

    for sched in LRSchedule:
        config = TrainingRules(
            optimizer=optimizer,
            lr=0.001,
            lr_schedule=sched,
            weight_decay_rule=WeightDecayRule.FIXED,
            weight_decay=0.01,
            gradient_clipping=GradientClipping.NORM_CLIP,
            clip_value=1.0,
            noise_injection=NoiseInjection.NONE,
        )

        model = TinyTransformer(model_config)
        result = evaluate_mutation(model, config, dataset, total_steps=500)

        status = "✓" if result.stability else f"✗ ({result.error_type})"
        print(f"  {sched.value:20s} | loss={result.final_train_loss:.4f if result.final_train_loss is not None else 'N/A':6s} | acc={result.test_acc:.3f} | {status}")

        results.append(result)

    return results


def sweep_weight_decay_rules() -> list[MutationResult]:
    """Search over weight decay rules."""

    print(f"\n{'='*60}")
    print(f"WEIGHT DECAY RULE SWEEP")
    print(f"{'='*60}")

    model_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        tie_embeddings=True,
        max_seq_len=32,
    )
    dataset = SyntheticDataset(vocab_size=256, seq_len=32)

    results = []

    for wd_rule in WeightDecayRule:
        config = TrainingRules(
            optimizer=OptimizerFamily.ADAMW,
            lr=0.001,
            lr_schedule=LRSchedule.COSINE_ANNEALING,
            weight_decay_rule=wd_rule,
            weight_decay=0.01 if wd_rule != WeightDecayRule.NONE else 0.0,
            gradient_clipping=GradientClipping.NORM_CLIP,
            clip_value=1.0,
            noise_injection=NoiseInjection.NONE,
        )

        model = TinyTransformer(model_config)
        result = evaluate_mutation(model, config, dataset, total_steps=500)

        status = "✓" if result.stability else f"✗ ({result.error_type})"
        print(f"  {wd_rule.value:12s} | loss={result.final_train_loss:.4f if result.final_train_loss is not None else 'N/A':6s} | acc={result.test_acc:.3f} | {status}")

        results.append(result)

    return results


def sweep_gradient_clipping() -> list[MutationResult]:
    """Search over gradient clipping strategies."""

    print(f"\n{'='*60}")
    print(f"GRADIENT CLIPPING SWEEP")
    print(f"{'='*60}")

    model_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        tie_embeddings=True,
        max_seq_len=32,
    )
    dataset = SyntheticDataset(vocab_size=256, seq_len=32)

    results = []

    for clip_strategy in [
        GradientClipping.NONE,
        GradientClipping.VALUE_CLIP,
        GradientClipping.NORM_CLIP,
    ]:
        config = TrainingRules(
            optimizer=OptimizerFamily.ADAMW,
            lr=0.001,
            lr_schedule=LRSchedule.COSINE_ANNEALING,
            weight_decay_rule=WeightDecayRule.FIXED,
            weight_decay=0.01,
            gradient_clipping=clip_strategy,
            clip_value=1.0 if clip_strategy != GradientClipping.NONE else 1.0,
            noise_injection=NoiseInjection.NONE,
        )

        model = TinyTransformer(model_config)
        result = evaluate_mutation(model, config, dataset, total_steps=500)

        status = "✓" if result.stability else f"✗ ({result.error_type})"
        print(f"  {clip_strategy.value:12s} | loss={result.final_train_loss:.4f if result.final_train_loss is not None else 'N/A':6s} | acc={result.test_acc:.3f} | {status}")

        results.append(result)

    return results


def sweep_noise_injection() -> list[MutationResult]:
    """Search over noise injection strategies."""

    print(f"\n{'='*60}")
    print(f"NOISE INJECTION SWEEP")
    print(f"{'='*60}")

    model_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        tie_embeddings=True,
        max_seq_len=32,
    )
    dataset = SyntheticDataset(vocab_size=256, seq_len=32)

    results = []

    for noise_type in [NoiseInjection.NONE, NoiseInjection.GRADIENT_NOISE]:
        for noise_std in [0.001, 0.01, 0.1]:
            config = TrainingRules(
                optimizer=OptimizerFamily.ADAMW,
                lr=0.001,
                lr_schedule=LRSchedule.COSINE_ANNEALING,
                weight_decay_rule=WeightDecayRule.FIXED,
                weight_decay=0.01,
                gradient_clipping=GradientClipping.NORM_CLIP,
                clip_value=1.0,
                noise_injection=noise_type,
                noise_std=noise_std,
            )

            model = TinyTransformer(model_config)
            result = evaluate_mutation(model, config, dataset, total_steps=500)

            noise_name = f"{noise_type.value}_std={noise_std}"
            status = "✓" if result.stability else f"✗ ({result.error_type})"
            print(f"  {noise_name:20s} | loss={result.final_train_loss:.4f if result.final_train_loss is not None else 'N/A':6s} | acc={result.test_acc:.3f} | {status}")

            results.append(result)

    return results


def find_instability_boundaries() -> dict[str, Any]:
    """Systematically search for instability boundaries."""

    print(f"\n{'='*60}")
    print(f"INSTABILITY BOUNDARY SEARCH")
    print(f"{'='*60}")

    model_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        tie_embeddings=True,
        max_seq_len=32,
    )
    dataset = SyntheticDataset(vocab_size=256, seq_len=32)

    boundaries = {}

    # Test each optimizer with each schedule at multiple LR values
    for opt in OptimizerFamily:
        for sched in LRSchedule:
            key = f"{opt.value}_{sched.value}"
            lr_results = []

            # Fine-grained LR sweep
            lrs = [1e-5, 3e-5, 1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1]

            for lr in lrs:
                config = TrainingRules(
                    optimizer=opt,
                    lr=lr,
                    lr_schedule=sched,
                    weight_decay_rule=WeightDecayRule.FIXED,
                    weight_decay=0.01,
                    gradient_clipping=GradientClipping.NORM_CLIP,
                    clip_value=1.0,
                    noise_injection=NoiseInjection.NONE,
                )

                model = TinyTransformer(model_config)
                result = evaluate_mutation(model, config, dataset, total_steps=200)

                lr_results.append({
                    "lr": lr,
                    "stable": result.stability,
                    "error_type": result.error_type,
                    "steps_to_instability": result.steps_to_instability,
                    "test_acc": result.test_acc if result.stability else 0.0,
                })

            # Find instability boundary
            stable_lrs = [r["lr"] for r in lr_results if r["stable"]]
            unstable_lrs = [r for r in lr_results if not r["stable"]]

            boundaries[key] = {
                "results": lr_results,
                "stable_range": min(stable_lrs) if stable_lrs else None,
                "unstable_range": max([r["lr"] for r in unstable_lrs]) if unstable_lrs else None,
                "boundary_lr": (min(stable_lrs) + max([r["lr"] for r in unstable_lrs])) / 2 if stable_lrs and unstable_lrs else None,
            }

            # Print summary
            print(f"\n  {key}:")
            for r in lr_results:
                status = "STABLE" if r["stable"] else f"UNSTABLE ({r['error_type']} at step {r['steps_to_instability']})"
                print(f"    lr={r['lr']:.4e}: {status} | acc={r['test_acc']:.3f}")

    return boundaries


def analyze_error_types(results: list[MutationResult]) -> dict[str, int]:
    """Analyze distribution of error types."""

    error_counts = {}
    for r in results:
        if not r.stability and r.error_type:
            error_counts[r.error_type] = error_counts.get(r.error_type, 0) + 1

    return error_counts


def run_full_mutation_experiment() -> dict[str, Any]:
    """Run all mutation experiments."""

    print("\n" + "=" * 60)
    print("BRANCH B: OPTIMIZER AND TRAINING-RULE MUTATION")
    print("=" * 60)
    print(f"Started: {datetime.now().isoformat()}")

    results = {
        "optimizer_sweep": [],
        "lr_schedule_sweep": [],
        "weight_decay_sweep": [],
        "gradient_clipping_sweep": [],
        "noise_injection_sweep": [],
        "instability_boundaries": {},
        "error_type_analysis": {},
    }

    # Run sweeps
    results["optimizer_sweep"] = sweep_optimizer_family()
    results["lr_schedule_sweep"] = sweep_lr_schedules()
    results["weight_decay_sweep"] = sweep_weight_decay_rules()
    results["gradient_clipping_sweep"] = sweep_gradient_clipping()
    results["noise_injection_sweep"] = sweep_noise_injection()

    # Find instability boundaries
    results["instability_boundaries"] = find_instability_boundaries()

    # Analyze error types from all results
    all_results = (
        results["optimizer_sweep"] +
        results["lr_schedule_sweep"] +
        results["weight_decay_sweep"] +
        results["gradient_clipping_sweep"] +
        results["noise_injection_sweep"]
    )
    results["error_type_analysis"] = analyze_error_types(all_results)

    print(f"\n{'='*60}")
    print(f"COMPLETE: {datetime.now().isoformat()}")
    print(f"{'='*60}")

    return results


def generate_report(results: dict[str, Any], report_path: str) -> None:
    """Generate mutation exploration report."""

    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    with open(report_path, "w") as f:
        f.write("# Branch B: Optimizer and Training-Rule Mutation Report\n\n")
        f.write(f"Generated: {datetime.now().isoformat()}\n\n")

        f.write("## Task\nExplore optimizer families, learning rate schedules, weight decay rules,\ngradient clipping strategies, and noise injection mechanisms. Identify instability\nboundaries and qualitative changes in error types.\n\n")

        # Summary statistics
        all_results = (
            results["optimizer_sweep"] +
            results["lr_schedule_sweep"] +
            results["weight_decay_sweep"] +
            results["gradient_clipping_sweep"] +
            results["noise_injection_sweep"]
        )
        stable_count = sum(1 for r in all_results if r.stability)
        unstable_count = len(all_results) - stable_count

        f.write("## Summary\n\n")
        f.write(f"- Total configurations tested: {len(all_results)}\n")
        f.write(f"- Stable configurations: {stable_count} ({100*stable_count/len(all_results):.1f}%)\n")
        f.write(f"- Unstable configurations: {unstable_count} ({100*unstable_count/len(all_results):.1f}%)\n\n")

        # Error type analysis
        f.write("## Error Type Analysis\n\n")
        for error_type, count in results.get("error_type_analysis", {}).items():
            f.write(f"- {error_type}: {count} occurrences\n")
        f.write("\n")

        # Optimizer family results
        f.write("## Optimizer Family Search\n\n")
        f.write("| Optimizer | Stable | Final Loss | Test Acc |\n")
        f.write("|-----------|--------|------------|----------|\n")
        for r in results.get("optimizer_sweep", []):
            f.write(f"| {r.rules.optimizer.value:12s} | {'✓' if r.stability else '✗'} | ")
            f.write(f"{r.final_train_loss:.4f if r.final_train_loss is not None else 'N/A':>10s} | {r.test_acc:.3f} |\n")
        f.write("\n")

        # LR schedule results
        f.write("## Learning Rate Schedule Search\n\n")
        f.write("| Schedule | Stable | Final Loss | Test Acc |\n")
        f.write("|----------|--------|------------|----------|\n")
        for r in results.get("lr_schedule_sweep", []):
            f.write(f"| {r.rules.lr_schedule.value:20s} | {'✓' if r.stability else '✗'} | ")
            f.write(f"{r.final_train_loss:.4f if r.final_train_loss is not None else 'N/A':>10s} | {r.test_acc:.3f} |\n")
        f.write("\n")

        # Weight decay results
        f.write("## Weight Decay Rule Search\n\n")
        f.write("| Rule | Stable | Final Loss | Test Acc |\n")
        f.write("|------|--------|------------|----------|\n")
        for r in results.get("weight_decay_sweep", []):
            f.write(f"| {r.rules.weight_decay_rule.value:12s} | {'✓' if r.stability else '✗'} | ")
            f.write(f"{r.final_train_loss:.4f if r.final_train_loss is not None else 'N/A':>10s} | {r.test_acc:.3f} |\n")
        f.write("\n")

        # Gradient clipping results
        f.write("## Gradient Clipping Search\n\n")
        f.write("| Strategy | Stable | Final Loss | Test Acc |\n")
        f.write("|----------|--------|------------|----------|\n")
        for r in results.get("gradient_clipping_sweep", []):
            f.write(f"| {r.rules.gradient_clipping.value:12s} | {'✓' if r.stability else '✗'} | ")
            f.write(f"{r.final_train_loss:.4f if r.final_train_loss is not None else 'N/A':>10s} | {r.test_acc:.3f} |\n")
        f.write("\n")

        # Noise injection results
        f.write("## Noise Injection Search\n\n")
        f.write("| Config | Stable | Final Loss | Test Acc |\n")
        f.write("|--------|--------|------------|----------|\n")
        for r in results.get("noise_injection_sweep", []):
            config_name = f"{r.rules.noise_injection.value}_std={r.rules.noise_std}"
            f.write(f"| {config_name:20s} | {'✓' if r.stability else '✗'} | ")
            f.write(f"{r.final_train_loss:.4f if r.final_train_loss is not None else 'N/A':>10s} | {r.test_acc:.3f} |\n")
        f.write("\n")

        # Instability boundaries
        f.write("## Instability Boundaries\n\n")
        for key, bounds in results.get("instability_boundaries", {}).items():
            f.write(f"### {key}\n\n")
            stable_lrs = [r["lr"] for r in bounds["results"] if r["stable"]]
            unstable_lrs = [r for r in bounds["results"] if not r["stable"]]

            if stable_lrs and unstable_lrs:
                f.write(f"- Stable range: lr ∈ [{min(stable_lrs):.4e}, {max(stable_lrs):.4e}]\n")
                f.write(f"- Unstable range: lr ∈ [{min([r['lr'] for r in unstable_lrs]):.4e}, {max([r['lr'] for r in unstable_lrs]):.4e}]\n")
                f.write(f"- Boundary LR: {bounds['boundary_lr']:.4e}\n")

                # Most common error type
                if unstable_lrs:
                    errors = [r["error_type"] for r in unstable_lrs]
                    error_counts = {}
                    for e in errors:
                        error_counts[e] = error_counts.get(e, 0) + 1
                    dominant_error = max(error_counts.items(), key=lambda x: x[1])
                    f.write(f"- Dominant error type: {dominant_error[0]} ({dominant_error[1]} cases)\n")
            else:
                f.write(f"- All stable or all unstable across tested LR range\n")
            f.write("\n")

        # Key findings
        f.write("## Key Findings\n\n")

        # Best configurations by accuracy (among stable)
        stable_results = [r for r in all_results if r.stability]
        if stable_results:
            best_acc = max(stable_results, key=lambda x: x.test_acc)
            f.write(f"- **Best configuration**: {best_acc.rules.optimizer.value} + {best_acc.rules.lr_schedule.value} (acc={best_acc.test_acc:.3f})\n")

            best_loss = min(stable_results, key=lambda x: x.final_train_loss)
            if best_loss.final_train_loss is not None:
                f.write(f"- **Lowest training loss**: {best_loss.rules.optimizer.value} + {best_loss.rules.lr_schedule.value} (loss={best_loss.final_train_loss:.4f})\n")

        # Error type summary
        error_analysis = results.get("error_type_analysis", {})
        if error_analysis:
            f.write("\n### Instability Patterns\n\n")
            for error_type, count in sorted(error_analysis.items(), key=lambda x: -x[1]):
                percentage = 100 * count / unstable_count if unstable_count > 0 else 0
                f.write(f"- {error_type}: {count} cases ({percentage:.1f}% of instability)\n")

        # Best weight decay rule by accuracy
        wd_results = [r for r in results.get("weight_decay_sweep", []) if r.stability]
        if wd_results:
            best_wd = max(wd_results, key=lambda x: x.test_acc)
            f.write(f"\n- **Best weight decay rule**: {best_wd.rules.weight_decay_rule.value} (acc={best_wd.test_acc:.3f})\n")

        # Best gradient clipping strategy
        clip_results = [r for r in results.get("gradient_clipping_sweep", []) if r.stability]
        if clip_results:
            best_clip = max(clip_results, key=lambda x: x.test_acc)
            f.write(f"- **Best gradient clipping**: {best_clip.rules.gradient_clipping.value} (acc={best_clip.test_acc:.3f})\n")

    print(f"Report written to: {report_path}")


if __name__ == "__main__":
    # Run experiments
    results = run_full_mutation_experiment()

    # Generate report
    report_path = "/Users/jakegearon/projects/tinymodels/.dgov/reports/branch-b-optimizer-mutation.md"
    generate_report(results, report_path)