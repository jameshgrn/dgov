"""Comprehensive training rule exploration experiments."""

import random
from dataclasses import dataclass
from typing import Any

import torch
from torch.utils.data import DataLoader

from .model import TransformerConfig, TinyTransformer
from .experiment import SyntheticDataset, evaluate_model as base_evaluate
from .training_rules import (
    OptimizerFamily,
    LRSchedule,
    WeightDecayRule,
    GradientClipping,
    NoiseInjection,
    TrainingRules,
    LearningRateScheduler,
    create_training_loop,
    analyze_optimizers_and_schedules,
)


@dataclass
class TrainingExperimentResult:
    """Results from a training rule configuration."""

    rules: TrainingRules
    num_params: int
    final_train_loss: float
    test_acc: float
    stability: bool
    max_grad_norm: float
    clipping_ratio: float


def evaluate_training_rule(
    model: TinyTransformer,
    rules: TrainingRules,
    dataset: SyntheticDataset,
    total_steps: int = 500,
) -> TrainingExperimentResult:
    """Evaluate a single training rule configuration."""

    loader = DataLoader(dataset, batch_size=32, shuffle=True)
    criterion = torch.nn.CrossEntropyLoss()

    train_step, get_state, config_str = create_training_loop(
        model, rules, loader, criterion, total_steps
    )

    # Training with instability detection
    is_stable = True
    final_loss = float("nan")

    for step in range(total_steps):
        batch = next(iter(loader))
        loss = train_step(batch, step)

        if torch.isnan(torch.tensor(loss)) or torch.isinf(torch.tensor(loss)):
            is_stable = False
            break

        final_loss = loss

    test_acc = base_evaluate(model, dataset, num_batches=20) if is_stable else 0.0
    state = get_state()

    return TrainingExperimentResult(
        rules=rules,
        num_params=model.num_parameters(),
        final_train_loss=final_loss,
        test_acc=test_acc,
        stability=is_stable,
        max_grad_norm=state["mean_grad_norm"],
        clipping_ratio=state.get("clip_ratio", 0.0),
    )


def explore_weight_decay_rules(
    lr: float = 0.001,
) -> list[TrainingExperimentResult]:
    """Search over weight decay rules with fixed optimizer."""

    print("\n" + "=" * 60)
    print("WEIGHT DECAY RULE EXPLORATION")
    print("=" * 60)

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

    for wd_rule in [WeightDecayRule.NONE, WeightDecayRule.FIXED, WeightDecayRule.ADAPTIVE, WeightDecayRule.LAYERWISE]:
        config = TrainingRules(
            optimizer=OptimizerFamily.ADAMW,
            lr=lr,
            lr_schedule=LRSchedule.COSINE_ANNEALING,
            weight_decay_rule=wd_rule,
            weight_decay=0.01,
            gradient_clipping=GradientClipping.NORM_CLIP,
            clip_value=1.0,
            noise_injection=NoiseInjection.NONE,
        )

        model = TinyTransformer(model_config)
        result = evaluate_training_rule(model, config, dataset, total_steps=500)

        results.append(result)
        print(
            f"  {wd_rule.value:12s} | stable={result.stability:5s} | loss={result.final_train_loss:.4f} | acc={result.test_acc:.3f} | max_grad={result.max_grad_norm:.3f}"
        )

    return results


def explore_gradient_clipping(
    base_lr: float = 0.001,
) -> list[TrainingExperimentResult]:
    """Search over gradient clipping strategies."""

    print("\n" + "=" * 60)
    print("GRADIENT CLIPPING EXPLORATION")
    print("=" * 60)

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

    for clip_strategy in [GradientClipping.NONE, GradientClipping.VALUE_CLIP, GradientClipping.NORM_CLIP]:
        config = TrainingRules(
            optimizer=OptimizerFamily.ADAMW,
            lr=base_lr,
            lr_schedule=LRSchedule.COSINE_ANNEALING,
            weight_decay_rule=WeightDecayRule.FIXED,
            weight_decay=0.01,
            gradient_clipping=clip_strategy,
            clip_value=1.0 if clip_strategy != GradientClipping.NONE else 1.0,
            noise_injection=NoiseInjection.NONE,
        )

        model = TinyTransformer(model_config)
        result = evaluate_training_rule(model, config, dataset, total_steps=500)

        results.append(result)
        print(
            f"  {clip_strategy.value:12s} | stable={result.stability:5s} | loss={result.final_train_loss:.4f} | acc={result.test_acc:.3f}"
        )

    return results


def explore_noise_injection(
    base_lr: float = 0.001,
) -> list[TrainingExperimentResult]:
    """Search over noise injection strategies."""

    print("\n" + "=" * 60)
    print("NOISE INJECTION EXPLORATION")
    print("=" * 60)

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
        config = TrainingRules(
            optimizer=OptimizerFamily.ADAMW,
            lr=base_lr,
            lr_schedule=LRSCHEDULE.COSINE_ANNEALING,
            weight_decay_rule=WeightDecayRule.FIXED,
            weight_decay=0.01,
            gradient_clipping=GradientClipping.NORM_CLIP,
            clip_value=1.0,
            noise_injection=noise_type,
            noise_std=0.01 if noise_type != NoiseInjection.NONE else 0.0,
        )

        model = TinyTransformer(model_config)
        result = evaluate_training_rule(model, config, dataset, total_steps=500)

        results.append(result)
        print(
            f"  {noise_type.value:12s} | stable={result.stability:5s} | loss={result.final_train_loss:.4f} | acc={result.test_acc:.3f}"
        )

    return results


def find_instability_boundaries(
    opt_families: list[OptimizerFamily] | None = None,
) -> dict[str, Any]:
    """Find instability boundaries for different optimizers and schedules."""

    print("\n" + "=" * 60)
    print("INSTABILITY BOUNDARY SEARCH")
    print("=" * 60)

    model_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        tie_embeddings=True,
        max_seq_len=32,
    )
    dataset = SyntheticDataset(vocab_size=256, seq_len=32)

    opt_families = opt_families or [OptimizerFamily.ADAM, OptimizerFamily.ADAMW]

    boundaries = {}

    for opt in opt_families:
        for sched in [LRSchedule.CONSTANT, LRSCHEDULE.COSINE_ANNEALING]:
            # Search over learning rates to find instability
            unstable_lrs = []
            stable_lrs = []

            print(f"\n  Optimizer={opt.value}, Schedule={sched.value}")

            for lr in [1e-4, 3e-4, 1e-3, 3e-3, 1e-2, 3e-2, 1e-1]:
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
                result = evaluate_training_rule(model, config, dataset, total_steps=200)

                if not result.stability:
                    unstable_lrs.append(lr)
                    print(f"    lr={lr:.4f} -> UNSTABLE")
                else:
                    stable_lrs.append(lr)
                    print(f"    lr={lr:.4f} -> stable, acc={result.test_acc:.3f}")

            boundaries[f"{opt.value}_{sched.value}"] = {
                "stable_range": min(stable_lrs) if stable_lrs else None,
                "unstable_range": max(unstable_lrs) if unstable_lrs else None,
            }

    return boundaries


def run_comprehensive_training_search():
    """Run all training rule experiments."""

    print("=" * 60)
    print("COMPREHENSIVE TRAINING RULE EXPLORATION")
    print("=" * 60)

    # Weight decay exploration
    wd_results = explore_weight_decay_rules()

    # Gradient clipping exploration
    clip_results = explore_gradient_clipping()

    # Noise injection exploration
    noise_results = explore_noise_injection()

    # Instability boundaries
    instability = find_instability_boundaries()

    return {
        "weight_decay": wd_results,
        "gradient_clipping": clip_results,
        "noise_injection": noise_results,
        "instability_boundaries": instability,
    }


def generate_report(
    results: dict[str, Any], report_path: str
) -> None:
    """Generate training rules exploration report."""

    import os

    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    with open(report_path, "w") as f:
        f.write("# Branch B: Training Rule Mutation Report\n\n")

        f.write("## Task\nExplore optimizer families, learning rate schedules, weight decay rules,\ngradient clipping strategies, and noise injection mechanisms. Identify instability\nboundaries and qualitative changes in error types.\n\n")

        f.write("## Methods\n- 500 training steps per configuration\n- Synthetic dataset with parity/repetition/majority tasks\n- Instability detection (NaN/Inf loss)\n- Systematic sweep over hyperparameters\n\n")

        # Weight decay section
        f.write("## Weight Decay Rules\n\n")
        for r in results.get("weight_decay", []):
            f.write(f"### {r.rules.weight_decay_rule.value}\n")
            f.write(f"- Stable: {r.stability}\n")
            f.write(f"- Final loss: {r.final_train_loss:.4f}\n")
            f.write(f"- Test accuracy: {r.test_acc:.3f}\n")
            f.write(f"- Max grad norm: {r.max_grad_norm:.3f}\n\n")

        # Gradient clipping section
        f.write("## Gradient Clipping Strategies\n\n")
        for r in results.get("gradient_clipping", []):
            f.write(f"### {r.rules.gradient_clipping.value}\n")
            f.write(f"- Stable: {r.stability}\n")
            f.write(f"- Final loss: {r.final_train_loss:.4f}\n")
            f.write(f"- Test accuracy: {r.test_acc:.3f}\n\n")

        # Noise injection section
        f.write("## Noise Injection\n\n")
        for r in results.get("noise_injection", []):
            f.write(f"### {r.rules.noise_injection.value}\n")
            f.write(f"- Stable: {r.stability}\n")
            f.write(f"- Final loss: {r.final_train_loss:.4f}\n")
            f.write(f"- Test accuracy: {r.test_acc:.3f}\n\n")

        # Instability boundaries section
        f.write("## Instability Boundaries\n\n")
        for key, bounds in results.get("instability_boundaries", {}).items():
            f.write(f"### {key}\n")
            f.write(f"- Stable LR range: {bounds['stable_range']}\n")
            f.write(f"- Unstable LR range: {bounds['unstable_range']}\n\n")

        # Key findings
        f.write("## Key Findings\n\n")

        # Analyze results
        stable_configs = [r for r in results.get("weight_decay", []) if r.stability]
        if stable_configs:
            best_wd = max(stable_configs, key=lambda x: x.test_acc)
            f.write(f"- Best weight decay rule by accuracy: {best_wd.rules.weight_decay_rule.value} (acc={best_wd.test_acc:.3f})\n")

        unstable_boundaries = [
            k for k, v in results.get("instability_boundaries", {}).items()
            if v["unstable_range"] is not None
        ]
        if unstable_boundaries:
            f.write(f"- Found instability boundaries for: {', '.join(unstable_boundaries)}\n")

    print(f"\nReport written to: {report_path}")


if __name__ == "__main__":
    # Run experiments
    results = run_comprehensive_training_search()

    # Generate report
    report_path = (
        "/Users/jakegearon/projects/tinymodels/.dgov/reports/branch-b-optimizer.md"
    )
    generate_report(results, report_path)