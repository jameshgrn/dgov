"""Phase Boundary Mapping: Grokking in Modular Addition.

This experiment systematically identifies phase boundaries where test accuracy
abruptly jumps (grokking transitions) as a function of model size, learning rate,
and weight decay. It seeks "stable weird regimes" - settings that should fail but
work surprisingly well.
"""

import os
import json
from dataclasses import dataclass, asdict
from typing import Any
from pathlib import Path

import torch
from torch.utils.data import DataLoader, Dataset

from tinymodels.model import (
    TransformerConfig,
    TinyTransformer,
    NormalizationStrategy,
    PositionalEncodingType,
)


@dataclass
class PhaseResult:
    """Results from a phase boundary experiment."""

    config_dict: dict[str, Any]
    num_params: int
    train_loss_history: list[float]
    test_acc_history: list[float]
    final_train_loss: float
    final_test_acc: float
    max_test_acc: float
    converged: bool
    grokked: bool
    grokking_step: int | None


class ModularAdditionDataset(Dataset):
    """Modular addition dataset for studying phase transitions."""

    def __init__(self, n_terms: int = 2, mod: int = 97, num_examples: int = 4000):
        self.n_terms = n_terms
        self.mod = mod
        self.num_examples = num_examples
        self.examples = []

        # Generate training and test data
        for _ in range(num_examples // 2):
            terms = [torch.randint(0, mod, (1,)).item() for _ in range(n_terms)]
            target = sum(terms) % mod

            # Encoding: pad with zeros, then list of tokens, then target
            seq_len = n_terms + 2
            seq = [0] * (n_terms + 1) + [target]
            labels = [-1] * n_terms + [vocab_size_for_mod(mod)]

            self.examples.append((torch.tensor(seq), torch.tensor(labels)))

        for _ in range(num_examples // 2):
            terms = [torch.randint(0, mod, (1,)).item() for _ in range(n_terms)]
            target = sum(terms) % mod

            seq = [0] * (n_terms + 1) + [target]
            labels = [-1] * n_terms + [vocab_size_for_mod(mod)]

            self.examples.append((torch.tensor(seq), torch.tensor(labels)))

        # Shuffle
        random_state = torch.get_rng_state()
        torch.random.manual_seed(42)
        indices = torch.randperm(len(self.examples))
        self.examples = [self.examples[i] for i in indices.tolist()]
        torch.set_rng_state(random_state)

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]


def vocab_size_for_mod(mod: int) -> int:
    """Return vocabulary size needed for modular addition with modulus mod."""
    # Need at least `mod` tokens for digits 0..mod-1, plus special tokens
    return max(128, mod + 64)


class TrainEvaluator:
    """Training and evaluation loop."""

    def __init__(self, model: TinyTransformer, lr: float = 0.001, weight_decay: float = 0.0):
        self.model = model
        self.lr = lr
        self.weight_decay = weight_decay
        self.optimizer = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)

    def train_step(self, tokens: torch.Tensor, labels: torch.Tensor) -> float:
        """Single training step. Returns loss."""
        self.optimizer.zero_grad()
        logits = self.model(tokens)[:, -1, :]
        loss = torch.nn.functional.cross_entropy(
            logits, labels, ignore_index=-1, reduction="mean"
        )
        loss.backward()

        # Gradient clipping for stability
        torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)
        self.optimizer.step()

        return loss.item()

    def evaluate(self, dataloader: DataLoader) -> tuple[float, float]:
        """Evaluate on dataset. Returns (loss, accuracy)."""
        self.model.eval()
        total_loss = 0.0
        correct = 0
        total = 0

        with torch.no_grad():
            for tokens, labels in dataloader:
                tokens = tokens.long()
                logits = self.model(tokens)[:, -1, :]
                loss = torch.nn.functional.cross_entropy(
                    logits, labels, ignore_index=-1, reduction="mean"
                )
                total_loss += loss.item() * len(labels)

                # Only evaluate on non-masked positions (last token)
                mask = labels != -1
                if mask.sum() > 0:
                    preds = torch.argmax(logits[mask], dim=-1)
                    targets = labels[mask]
                    correct += (preds == targets).sum().item()
                    total += len(targets)

        self.model.train()
        avg_loss = total_loss / max(len(dataloader), 1)
        acc = correct / max(total, 1)
        return avg_loss, acc


def detect_grokking(acc_history: list[float], window: int = 50) -> tuple[bool, int | None]:
    """Detect if model exhibits grokking behavior.

    Grokking is characterized by:
    - Low test accuracy during training (stuck in memorization)
    - Abrupt jump to high test accuracy later in training
    - Final test accuracy significantly higher than train accuracy
    """
    if len(acc_history) < window * 2:
        return False, None

    # Check for abrupt improvement
    early_acc = sum(acc_history[:len(acc_history)//2]) / (len(acc_history) // 2)
    late_acc = sum(acc_history[len(acc_history)//2:]) / (len(acc_history) - len(acc_history)//2)

    if late_acc - early_acc < 0.3:  # Need at least 30% improvement
        return False, None

    # Find the step where accuracy jumped
    grokking_step = None
    for i in range(window, len(acc_history)):
        prev_window = sum(acc_history[i-window:i]) / window
        curr_window = sum(acc_history[i:i+window]) / (window + 1)
        if curr_window - prev_window > 0.2:  # Abrupt jump of 20%+
            grokking_step = i
            return True, grokking_step

    return False, None


def run_phase_boundary_experiments():
    """Run systematic phase boundary experiments."""

    results: list[PhaseResult] = []
    mod = 97
    vocab_size = vocab_size_for_mod(mod)
    seq_len = 10

    # Create train and test datasets
    train_dataset = ModularAdditionDataset(n_terms=2, mod=mod, num_examples=4000)
    test_dataset = ModularAdditionDataset(n_terms=2, mod=mod, num_examples=1000)

    train_loader = DataLoader(train_dataset, batch_size=64, shuffle=True)
    test_loader = DataLoader(test_dataset, batch_size=64, shuffle=False)

    # ============================================================
    # PHASE 1: WIDTH BOUNDARY (minimal competent embedding dimension)
    # ============================================================
    print("\n" + "=" * 70)
    print("PHASE 1: WIDTH BOUNDARY - Minimal Competent Embedding")
    print("=" * 70)

    for n_embd in [8, 16, 24, 32, 48, 64]:
        config = TransformerConfig(
            vocab_size=vocab_size,
            hidden_dim=n_embd,
            num_layers=2,
            num_heads=min(2, n_embd // 8) if n_embd >= 16 else 1,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=seq_len,
        )
        model = TinyTransformer(config)
        evaluator = TrainEvaluator(model, lr=0.001, weight_decay=0.1)

        train_losses = []
        test_accs = []
        converged = True

        for step in range(500):
            tokens, labels = next(iter(train_loader))
            loss = evaluator.train_step(tokens, labels)

            if torch.isnan(torch.tensor(loss)) or torch.isinf(torch.tensor(loss)):
                converged = False
                print(f"  n_embd={n_embd:3d} | Step {step}: NaN/Inf loss - STOPPED")
                break

            train_losses.append(loss)
            if step % 50 == 0:
                _, test_acc = evaluator.evaluate(test_loader)
                test_accs.append(test_acc)

        if not converged:
            result = PhaseResult(
                config_dict=asdict(config),
                num_params=model.num_parameters(),
                train_loss_history=train_losses,
                test_acc_history=[],
                final_train_loss=float('nan'),
                final_test_acc=0.0,
                max_test_acc=0.0,
                converged=False,
                grokked=False,
                grokking_step=None,
            )
        else:
            final_loss, final_acc = evaluator.evaluate(test_loader)
            _, test_acc = evaluator.evaluate(test_loader)
            max_acc = max(test_accs) if test_accs else 0.0

            # Check for initial memorization (low train loss, low test acc)
            early_train_loss = sum(train_losses[:100]) / min(100, len(train_losses))
            grokked, grokking_step = detect_grokking(test_accs)

            print(f"  n_embd={n_embd:3d} | params={model.num_parameters():6,} "
                  f"| loss={early_train_loss:.4f} | test_acc={final_acc:.3f} | "
                  f"max_acc={max_acc:.3f} | grokked={'yes' if grokked else 'no'}")

            result = PhaseResult(
                config_dict=asdict(config),
                num_params=model.num_parameters(),
                train_loss_history=train_losses,
                test_acc_history=test_accs,
                final_train_loss=final_loss,
                final_test_acc=final_acc,
                max_test_acc=max_acc,
                converged=True,
                grokked=grokked,
                grokking_step=grokked_step,
            )

        results.append(result)

    # ============================================================
    # PHASE 2: DEPTH BOUNDARY (minimal competent layer count)
    # ============================================================
    print("\n" + "=" * 70)
    print("PHASE 2: DEPTH BOUNDARY - Minimal Competent Depth")
    print("=" * 70)

    for n_layers in [1, 2, 3, 4]:
        config = TransformerConfig(
            vocab_size=vocab_size,
            hidden_dim=64,
            num_layers=n_layers,
            num_heads=4,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=seq_len,
        )
        model = TinyTransformer(config)
        evaluator = TrainEvaluator(model, lr=0.001, weight_decay=0.05)

        train_losses = []
        test_accs = []
        converged = True

        for step in range(500):
            tokens, labels = next(iter(train_loader))
            loss = evaluator.train_step(tokens, labels)

            if torch.isnan(torch.tensor(loss)) or torch.isinf(torch.tensor(loss)):
                converged = False
                print(f"  n_layers={n_layers} | Step {step}: NaN/Inf loss - STOPPED")
                break

            train_losses.append(loss)
            if step % 50 == 0:
                _, test_acc = evaluator.evaluate(test_loader)
                test_accs.append(test_acc)

        if not converged:
            result = PhaseResult(
                config_dict=asdict(config),
                num_params=model.num_parameters(),
                train_loss_history=train_losses,
                test_acc_history=[],
                final_train_loss=float('nan'),
                final_test_acc=0.0,
                max_test_acc=0.0,
                converged=False,
                grokked=False,
                grokking_step=None,
            )
        else:
            final_loss, _ = evaluator.evaluate(test_loader)
            max_acc = max(test_accs) if test_accs else 0.0
            grokked, grokking_step = detect_grokking(test_accs)

            print(f"  n_layers={n_layers} | params={model.num_parameters():6,} "
                  f"| test_acc={final_loss:.4f} | max_acc={max_acc:.3f} | "
                  f"grokked={'yes' if grokked else 'no'}")

            result = PhaseResult(
                config_dict=asdict(config),
                num_params=model.num_parameters(),
                train_loss_history=train_losses,
                test_acc_history=test_accs,
                final_train_loss=final_loss,
                final_test_acc=max_acc,
                max_test_acc=max_acc,
                converged=True,
                grokked=grokked,
                grokking_step=grokked_step,
            )

        results.append(result)

    # ============================================================
    # PHASE 3: LEARNING RATE BOUNDARY (critical LR for convergence)
    # ============================================================
    print("\n" + "=" * 70)
    print("PHASE 3: LEARNING RATE BOUNDARY - Critical Learning Rate")
    print("=" * 70)

    lr_values = [0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01]

    for lr in lr_values:
        config = TransformerConfig(
            vocab_size=vocab_size,
            hidden_dim=64,
            num_layers=2,
            num_heads=4,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=seq_len,
        )
        model = TinyTransformer(config)
        evaluator = TrainEvaluator(model, lr=lr, weight_decay=0.1)

        train_losses = []
        test_accs = []
        converged = True

        for step in range(500):
            tokens, labels = next(iter(train_loader))
            loss = evaluator.train_step(tokens, labels)

            if torch.isnan(torch.tensor(loss)) or torch.isinf(torch.tensor(loss)):
                converged = False
                print(f"  lr={lr:.4f} | Step {step}: NaN/Inf loss - STOPPED")
                break

            train_losses.append(loss)
            if step % 50 == 0:
                _, test_acc = evaluator.evaluate(test_loader)
                test_accs.append(test_acc)

        if not converged:
            result = PhaseResult(
                config_dict=asdict(config),
                num_params=model.num_parameters(),
                train_loss_history=train_losses,
                test_acc_history=[],
                final_train_loss=float('nan'),
                final_test_acc=0.0,
                max_test_acc=0.0,
                converged=False,
                grokked=False,
                grokking_step=None,
            )
        else:
            _, final_acc = evaluator.evaluate(test_loader)
            max_acc = max(test_accs) if test_accs else 0.0

            print(f"  lr={lr:.4f} | params={model.num_parameters():6,} "
                  f"| test_acc={final_acc:.3f} | max_acc={max_acc:.3f}")

            result = PhaseResult(
                config_dict=asdict(config),
                num_params=model.num_parameters(),
                train_loss_history=train_losses,
                test_acc_history=test_accs,
                final_train_loss=float('nan'),
                final_test_acc=final_acc,
                max_test_acc=max_acc,
                converged=True,
                grokked=False,
                grokking_step=None,
            )

        results.append(result)

    # ============================================================
    # PHASE 4: WEIGHT DECA Y BOUNDARY (stable weird regimes)
    # ============================================================
    print("\n" + "=" * 70)
    print("PHASE 4: WEIGHT DECA Y BOUNDARY - Stable Weird Regimes")
    print("=" * 70)

    wd_values = [0.0, 0.01, 0.05, 0.1, 0.2, 0.5]

    for wd in wd_values:
        config = TransformerConfig(
            vocab_size=vocab_size,
            hidden_dim=64,
            num_layers=2,
            num_heads=4,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=seq_len,
        )
        model = TinyTransformer(config)
        evaluator = TrainEvaluator(model, lr=0.001, weight_decay=wd)

        train_losses = []
        test_accs = []
        converged = True

        for step in range(500):
            tokens, labels = next(iter(train_loader))
            loss = evaluator.train_step(tokens, labels)

            if torch.isnan(torch.tensor(loss)) or torch.isinf(torch.tensor(loss)):
                converged = False
                print(f"  wd={wd:.2f} | Step {step}: NaN/Inf loss - STOPPED")
                break

            train_losses.append(loss)
            if step % 50 == 0:
                _, test_acc = evaluator.evaluate(test_loader)
                test_accs.append(test_acc)

        if not converged:
            result = PhaseResult(
                config_dict=asdict(config),
                num_params=model.num_parameters(),
                train_loss_history=train_losses,
                test_acc_history=[],
                final_train_loss=float('nan'),
                final_test_acc=0.0,
                max_test_acc=0.0,
                converged=False,
                grokked=False,
                grokking_step=None,
            )
        else:
            _, final_acc = evaluator.evaluate(test_loader)
            max_acc = max(test_accs) if test_accs else 0.0
            grokked, grokking_step = detect_grokking(test_accs)

            print(f"  wd={wd:.2f} | params={model.num_parameters():6,} "
                  f"| test_acc={final_acc:.3f} | max_acc={max_acc:.3f} | "
                  f"grokked={'yes' if grokked else 'no'}")

            result = PhaseResult(
                config_dict=asdict(config),
                num_params=model.num_parameters(),
                train_loss_history=train_losses,
                test_acc_history=test_accs,
                final_train_loss=float('nan'),
                final_test_acc=final_acc,
                max_test_acc=max_acc,
                converged=True,
                grokked=grokked,
                grokking_step=grokked_step,
            )

        results.append(result)

    # ============================================================
    # Save Results
    # ============================================================
    output_dir = Path("outputs/phase_boundary")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Serialize results to JSON
    json_results = []
    for r in results:
        json_results.append({
            "config_dict": r.config_dict,
            "num_params": r.num_params,
            "final_train_loss": r.final_train_loss if not (r.final_train_loss != r.final_train_loss) else None,  # NaN check
            "final_test_acc": r.final_test_acc,
            "max_test_acc": r.max_test_acc,
            "converged": r.converged,
            "grokked": r.grokked,
            "grokking_step": r.grokking_step,
        })

    with open(output_dir / "phase_boundary_results.json", "w") as f:
        json.dump(json_results, f, indent=2)

    print("\n" + "=" * 70)
    print("EXPERIMENT COMPLETE. Results saved to outputs/phase_boundary/")
    print("=" * 70)

    return results


if __name__ == "__main__":
    run_phase_boundary_experiments()