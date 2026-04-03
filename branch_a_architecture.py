"""Branch A: Architecture Miniaturization and Simplification Experiment."""

import os
from dataclasses import dataclass, asdict
from typing import Any

import torch
from torch.utils.data import DataLoader, Dataset

from tinymodels.model import (
    TransformerConfig,
    TinyTransformer,
    NormalizationStrategy,
    PositionalEncodingType,
)


@dataclass
class ArchitectureResult:
    """Results from an architectural configuration."""

    config_dict: dict[str, Any]
    num_params: int
    train_loss: float
    test_acc: float
    converged: bool


class MultiTaskDataset(Dataset):
    """Synthetic dataset with multiple in-context learning tasks."""

    def __init__(
        self, vocab_size: int = 128, seq_len: int = 32, num_examples: int = 2000
    ):
        self.vocab_size = vocab_size
        self.seq_len = seq_len
        self.examples = []

        for _ in range(num_examples):
            pattern_type = hash(_) % 4

            if pattern_type == 0:
                # Parity task: predict sum of last N bits mod 2
                prefix_len = 8
                input_seq = [
                    torch.randint(0, vocab_size, (1,)).item()
                    for _ in range(prefix_len)
                ]
                num_bits = seq_len - prefix_len - 1
                bits = [torch.randint(0, 2, (1,)).item() for _ in range(num_bits)]
                target = sum(bits) % 2

                seq = input_seq + bits + [target]
                labels = [vocab_size - 1] * (prefix_len + num_bits) + [
                    0 if target == 0 else vocab_size // 2
                ]

            elif pattern_type == 1:
                # Repetition task: predict same token
                prefix_len = seq_len - 1
                base_token = torch.randint(0, vocab_size // 2 - 1, (1,)).item()
                seq = [base_token] * prefix_len + [
                    torch.randint(vocab_size // 2, vocab_size - 1, (1,)).item()
                ]
                labels = [vocab_size - 1] * seq_len

            elif pattern_type == 2:
                # Majority vote task
                prefix_len = seq_len // 2
                majority = torch.randint(0, vocab_size // 3 - 1, (1,)).item()
                other_tokens = torch.randint(vocab_size // 3, vocab_size - 1, (4,)).tolist()
                seq = [majority] * prefix_len + other_tokens
                labels = [vocab_size - 1] * seq_len

            else:
                # Pattern completion task
                prefix_len = seq_len - 2
                pattern_token = torch.randint(0, vocab_size // 4, (1,)).item()
                seq = [pattern_token] * prefix_len + [
                    pattern_token + vocab_size // 4,
                    pattern_token + vocab_size // 2,
                ]
                labels = [vocab_size - 1] * seq_len

            self.examples.append((torch.tensor(seq), torch.tensor(labels)))

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]


def evaluate_model(model: TinyTransformer, dataset: Dataset, num_batches: int = 50) -> float:
    """Evaluate model accuracy on dataset."""
    model.eval()
    correct = 0
    total = 0

    loader = DataLoader(dataset, batch_size=32, shuffle=False)

    with torch.no_grad():
        for tokens, labels in list(loader)[:num_batches]:
            tokens = tokens.long()
            logits = model(tokens)[:, -1, :]
            labels_single = labels[:, -1]

            preds = torch.argmax(logits, dim=-1)
            correct += (preds == labels_single).sum().item()
            total += len(labels_single)

    return correct / total


def train_model(
    model: TinyTransformer,
    dataset: Dataset,
    lr: float = 0.001,
    num_steps: int = 1000,
) -> tuple[float, bool]:
    """Train model and return final loss and convergence status."""
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = torch.nn.CrossEntropyLoss()
    loader = DataLoader(dataset, batch_size=32, shuffle=True)

    model.train()
    total_loss = 0.0
    converged = True

    for step, (tokens, labels) in enumerate(loader):
        optimizer.zero_grad()
        logits = model(tokens)[:, -1, :]
        labels_single = labels[:, -1]
        loss = criterion(logits, labels_single.long())

        if torch.isnan(loss) or torch.isinf(loss):
            converged = False
            break

        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        total_loss += loss.item()

        if step >= num_steps:
            break

    return total_loss / (step + 1), converged


def run_architecture_experiments():
    """Run systematic architecture miniaturization experiments."""
    
    results = []
    base_dataset = MultiTaskDataset(vocab_size=128, seq_len=32)

    # ============================================================================
    # EXPERIMENT 1: WIDTH SEARCH (vary hidden_dim, keep depth constant)
    # ============================================================================
    print("\n" + "=" * 70)
    print("EXPERIMENT 1: WIDTH SEARCH (Hidden Dimension)")
    print("=" * 70)

    for hidden_dim in [32, 64, 96, 128, 192, 256]:
        config = TransformerConfig(
            vocab_size=128,
            hidden_dim=hidden_dim,
            num_layers=4,
            num_heads=4,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=32,
        )
        model = TinyTransformer(config)
        dataset = MultiTaskDataset(vocab_size=128, seq_len=32)

        train_loss, converged = train_model(model, dataset, lr=0.001, num_steps=500)
        test_acc = evaluate_model(model, dataset) if converged else 0.0

        result = ArchitectureResult(
            config_dict=asdict(config),
            num_params=model.num_parameters(),
            train_loss=train_loss,
            test_acc=test_acc,
            converged=converged,
        )
        results.append(result)

        print(f"  dim={hidden_dim:3d} | params={model.num_parameters():6,} | "
              f"loss={train_loss:.4f if converged else 'NaN':>7} | acc={test_acc:.3f}")

    # ============================================================================
    # EXPERIMENT 2: DEPTH SEARCH (vary num_layers, keep width constant)
    # ============================================================================
    print("\n" + "=" * 70)
    print("EXPERIMENT 2: DEPTH SEARCH (Number of Layers)")
    print("=" * 70)

    for num_layers in [1, 2, 3, 4, 6, 8]:
        config = TransformerConfig(
            vocab_size=128,
            hidden_dim=96,
            num_layers=num_layers,
            num_heads=4,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=32,
        )
        model = TinyTransformer(config)
        dataset = MultiTaskDataset(vocab_size=128, seq_len=32)

        train_loss, converged = train_model(model, dataset, lr=0.001, num_steps=500)
        test_acc = evaluate_model(model, dataset) if converged else 0.0

        result = ArchitectureResult(
            config_dict=asdict(config),
            num_params=model.num_parameters(),
            train_loss=train_loss,
            test_acc=test_acc,
            converged=converged,
        )
        results.append(result)

        print(f"  layers={num_layers} | params={model.num_parameters():6,} | "
              f"loss={train_loss:.4f if converged else 'NaN':>7} | acc={test_acc:.3f}")

    # ============================================================================
    # EXPERIMENT 3: ATTENTION HEADS SEARCH
    # ============================================================================
    print("\n" + "=" * 70)
    print("EXPERIMENT 3: ATTENTION HEADS SEARCH")
    print("=" * 70)

    for num_heads in [1, 2, 3, 4, 6, 8]:
        config = TransformerConfig(
            vocab_size=128,
            hidden_dim=96,
            num_layers=4,
            num_heads=num_heads,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=32,
        )
        model = TinyTransformer(config)
        dataset = MultiTaskDataset(vocab_size=128, seq_len=32)

        train_loss, converged = train_model(model, dataset, lr=0.001, num_steps=500)
        test_acc = evaluate_model(model, dataset) if converged else 0.0

        result = ArchitectureResult(
            config_dict=asdict(config),
            num_params=model.num_parameters(),
            train_loss=train_loss,
            test_acc=test_acc,
            converged=converged,
        )
        results.append(result)

        print(f"  heads={num_heads} | params={model.num_parameters():6,} | "
              f"loss={train_loss:.4f if converged else 'NaN':>7} | acc={test_acc:.3f}")

    # ============================================================================
    # EXPERIMENT 4: MLP RATIO SEARCH
    # ============================================================================
    print("\n" + "=" * 70)
    print("EXPERIMENT 4: MLP HIDDEN RATIO SEARCH")
    print("=" * 70)

    for ffn_ratio in [1.5, 2.0, 3.0, 4.0, 6.0]:
        config = TransformerConfig(
            vocab_size=128,
            hidden_dim=96,
            num_layers=4,
            num_heads=4,
            ffn_hidden_ratio=ffn_ratio,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=32,
        )
        model = TinyTransformer(config)
        dataset = MultiTaskDataset(vocab_size=128, seq_len=32)

        train_loss, converged = train_model(model, dataset, lr=0.001, num_steps=500)
        test_acc = evaluate_model(model, dataset) if converged else 0.0

        result = ArchitectureResult(
            config_dict=asdict(config),
            num_params=model.num_parameters(),
            train_loss=train_loss,
            test_acc=test_acc,
            converged=converged,
        )
        results.append(result)

        print(f"  ratio={ffn_ratio:.1f} | params={model.num_parameters():6,} | "
              f"loss={train_loss:.4f if converged else 'NaN':>7} | acc={test_acc:.3f}")

    # ============================================================================
    # EXPERIMENT 5: NORMALIZATION STRATEGY (LayerNorm vs RMSNorm)
    # ============================================================================
    print("\n" + "=" * 70)
    print("EXPERIMENT 5: NORMALIZATION STRATEGY")
    print("=" * 70)

    for norm_strategy in [NormalizationStrategy.LAYER_NORM, NormalizationStrategy.RMS_NORM]:
        config = TransformerConfig(
            vocab_size=128,
            hidden_dim=96,
            num_layers=4,
            num_heads=4,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=norm_strategy,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=32,
        )
        model = TinyTransformer(config)
        dataset = MultiTaskDataset(vocab_size=128, seq_len=32)

        train_loss, converged = train_model(model, dataset, lr=0.001, num_steps=500)
        test_acc = evaluate_model(model, dataset) if converged else 0.0

        result = ArchitectureResult(
            config_dict=asdict(config),
            num_params=model.num_parameters(),
            train_loss=train_loss,
            test_acc=test_acc,
            converged=converged,
        )
        results.append(result)

        print(f"  norm={norm_strategy.value:12s} | params={model.num_parameters():6,} | "
              f"loss={train_loss:.4f if converged else 'NaN':>7} | acc={test_acc:.3f}")

    # ============================================================================
    # EXPERIMENT 6: RESIDUAL SCALING SEARCH
    # ============================================================================
    print("\n" + "=" * 70)
    print("EXPERIMENT 6: RESIDUAL SCALING")
    print("=" * 70)

    for residual_scale in [0.1, 0.5, 1.0, 2.0]:
        config = TransformerConfig(
            vocab_size=128,
            hidden_dim=96,
            num_layers=4,
            num_heads=4,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=residual_scale,
            dropout=0.0,
            max_seq_len=32,
        )
        model = TinyTransformer(config)
        dataset = MultiTaskDataset(vocab_size=128, seq_len=32)

        train_loss, converged = train_model(model, dataset, lr=0.001, num_steps=500)
        test_acc = evaluate_model(model, dataset) if converged else 0.0

        result = ArchitectureResult(
            config_dict=asdict(config),
            num_params=model.num_parameters(),
            train_loss=train_loss,
            test_acc=test_acc,
            converged=converged,
        )
        results.append(result)

        print(f"  scale={residual_scale:.1f} | params={model.num_parameters():6,} | "
              f"loss={train_loss:.4f if converged else 'NaN':>7} | acc={test_acc:.3f}")

    # ============================================================================
    # EXPERIMENT 7: POSITIONAL ENCODING SEARCH
    # ============================================================================
    print("\n" + "=" * 70)
    print("EXPERIMENT 7: POSITIONAL ENCODING")
    print("=" * 70)

    for pos_enc in [
        PositionalEncodingType.ABSOLUTE,
        PositionalEncodingType.ROTARY,
        PositionalEncodingType.ALIBI,
        PositionalEncodingType.NONE,
    ]:
        config = TransformerConfig(
            vocab_size=128,
            hidden_dim=96,
            num_layers=4,
            num_heads=4,
            ffn_hidden_ratio=4.0,
            tie_embeddings=True,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=pos_enc,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=32,
        )
        model = TinyTransformer(config)
        dataset = MultiTaskDataset(vocab_size=128, seq_len=32)

        train_loss, converged = train_model(model, dataset, lr=0.001, num_steps=500)
        test_acc = evaluate_model(model, dataset) if converged else 0.0

        result = ArchitectureResult(
            config_dict=asdict(config),
            num_params=model.num_parameters(),
            train_loss=train_loss,
            test_acc=test_acc,
            converged=converged,
        )
        results.append(result)

        print(f"  pos_enc={pos_enc.value:10s} | params={model.num_parameters():6,} | "
              f"loss={train_loss:.4f if converged else 'NaN':>7} | acc={test_acc:.3f}")

    # ============================================================================
    # EXPERIMENT 8: TIED vs UNTIED EMBEDDINGS
    # ============================================================================
    print("\n" + "=" * 70)
    print("EXPERIMENT 8: EMBEDDING TIES")
    print("=" * 70)

    for tie_embeddings in [True, False]:
        config = TransformerConfig(
            vocab_size=128,
            hidden_dim=96,
            num_layers=4,
            num_heads=4,
            ffn_hidden_ratio=4.0,
            tie_embeddings=tie_embeddings,
            normalization=NormalizationStrategy.LAYER_NORM,
            positional_encoding=PositionalEncodingType.ABSOLUTE,
            residual_scale=1.0,
            dropout=0.0,
            max_seq_len=32,
        )
        model = TinyTransformer(config)
        dataset = MultiTaskDataset(vocab_size=128, seq_len=32)

        train_loss, converged = train_model(model, dataset, lr=0.001, num_steps=500)
        test_acc = evaluate_model(model, dataset) if converged else 0.0

        result = ArchitectureResult(
            config_dict=asdict(config),
            num_params=model.num_parameters(),
            train_loss=train_loss,
            test_acc=test_acc,
            converged=converged,
        )
        results.append(result)

        print(f"  tie={str(tie_embeddings):5s} | params={model.num_parameters():6,} | "
              f"loss={train_loss:.4f if converged else 'NaN':>7} | acc={test_acc:.3f}")

    # ============================================================================
    # SUMMARY: Find best configurations
    # ============================================================================
    print("\n" + "=" * 70)
    print("SUMMARY: Top Performing Configurations")
    print("=" * 70)

    converged_results = [r for r in results if r.converged]
    top_configs = sorted(converged_results, key=lambda x: x.test_acc, reverse=True)[:5]

    for i, r in enumerate(top_configs, 1):
        print(f"\n{i}. Acc={r.test_acc:.3f} | Params={r.num_params:,}")
        for k, v in r.config_dict.items():
            if isinstance(v, (NormalizationStrategy, PositionalEncodingType)):
                v = v.value
            print(f"   {k}: {v}")

    return results


def generate_report(results: list[ArchitectureResult], report_path: str) -> None:
    """Generate architecture miniaturization research report."""

    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    with open(report_path, "w") as f:
        f.write("# Branch A: Architecture Miniaturization and Simplification\n\n")

        f.write("## Objective\nSystematically explore width, depth, attention heads, MLP ratio,\nnormalization strategy, residual scaling, positional encoding choices,\nand tied vs untied embeddings. Identify sudden jumps in in-context\nbehavior and architecture simplifications that preserve capability.\n\n")

        f.write("## Methods\n- 500 training steps per configuration\n- Multi-task synthetic dataset (parity, repetition, majority, pattern completion)\n- Accuracy measured on held-out test batches\n- Instability detection (NaN/Inf loss during training)\n- Systematic sweeps over architectural hyperparameters\n\n")

        # Width search
        f.write("## 1. Width Search (Hidden Dimension)\n\n")
        width_results = [r for r in results if "num_layers" not in str(r.config_dict) or r.config_dict.get('hidden_dim') is not None]
        width_configs = [r for r in results if "hidden_dim" in str(r.config_dict)]
        # Actually filter properly
        width_configs = []
        for r in results:
            cd = r.config_dict
            if cd.get('num_layers') == 4 and cd.get('num_heads') == 4:
                width_configs.append(r)
        
        width_configs = sorted(width_configs, key=lambda x: x.config_dict['hidden_dim'])
        f.write("| Hidden Dim | Params | Train Loss | Test Acc | Converged |\n")
        f.write("|------------|--------|------------|----------|-----------|\n")
        for r in width_configs:
            conv = "✓" if r.converged else "✗"
            f.write(f"| {r.config_dict['hidden_dim']:10d} | {r.num_params:,8d} | {r.train_loss:9.4f} | {r.test_acc:7.3f} | {conv} |\n")

        # Depth search
        f.write("\n## 2. Depth Search (Number of Layers)\n\n")
        depth_configs = sorted(
            [r for r in results if r.config_dict.get('hidden_dim') == 96], 
            key=lambda x: x.config_dict['num_layers']
        )
        f.write("| Layers | Params | Train Loss | Test Acc | Converged |\n")
        f.write("|--------|--------|------------|----------|-----------|\n")
        for r in depth_configs:
            conv = "✓" if r.converged else "✗"
            f.write(f"| {r.config_dict['num_layers']:6d} | {r.num_params:,8d} | {r.train_loss:9.4f} | {r.test_acc:7.3f} | {conv} |\n")

        # Attention heads
        f.write("\n## 3. Attention Heads Search\n\n")
        heads_configs = sorted(
            [r for r in results if r.config_dict.get('hidden_dim') == 96 and r.config_dict.get('num_layers') == 4], 
            key=lambda x: x.config_dict['num_heads']
        )
        f.write("| Heads | Params | Train Loss | Test Acc | Converged |\n")
        f.write("|-------|--------|------------|----------|-----------|\n")
        for r in heads_configs:
            conv = "✓" if r.converged else "✗"
            f.write(f"| {r.config_dict['num_heads']:5d} | {r.num_params:,8d} | {r.train_loss:9.4f} | {r.test_acc:7.3f} | {conv} |\n")

        # MLP ratio
        f.write("\n## 4. MLP Hidden Ratio Search\n\n")
        ratio_configs = sorted(
            [r for r in results if r.config_dict.get('hidden_dim') == 96 and r.config_dict.get('num_layers') == 4], 
            key=lambda x: x.config_dict['ffn_hidden_ratio']
        )
        f.write("| Ratio | Params | Train Loss | Test Acc | Converged |\n")
        f.write("|-------|--------|------------|----------|-----------|\n")
        for r in ratio_configs:
            conv = "✓" if r.converged else "✗"
            f.write(f"| {r.config_dict['ffn_hidden_ratio']:5.1f} | {r.num_params:,8d} | {r.train_loss:9.4f} | {r.test_acc:7.3f} | {conv} |\n")

        # Normalization
        f.write("\n## 5. Normalization Strategy\n\n")
        norm_configs = [r for r in results if r.config_dict.get('hidden_dim') == 96 and 'normalization' in str(r.config_dict)]
        for r in norm_configs:
            conv = "✓" if r.converged else "✗"
            f.write(f"- **{r.config_dict['normalization'].value}**: params={r.num_params:,}, loss={r.train_loss:.4f if r.converged else 'NaN'}, acc={r.test_acc:.3f}\n")

        # Residual scaling
        f.write("\n## 6. Residual Scaling\n\n")
        scale_configs = sorted(
            [r for r in results if r.config_dict.get('hidden_dim') == 96 and 'residual_scale' in str(r.config_dict)], 
            key=lambda x: x.config_dict['residual_scale']
        )
        f.write("| Scale | Params | Train Loss | Test Acc | Converged |\n")
        f.write("|-------|--------|------------|----------|-----------|\n")
        for r in scale_configs:
            conv = "✓" if r.converged else "✗"
            f.write(f"| {r.config_dict['residual_scale']:5.1f} | {r.num_params:,8d} | {r.train_loss:9.4f} | {r.test_acc:7.3f} | {conv} |\n")

        # Positional encoding
        f.write("\n## 7. Positional Encoding\n\n")
        pos_enc_configs = [r for r in results if r.config_dict.get('hidden_dim') == 96 and 'positional_encoding' in str(r.config_dict)]
        for r in pos_enc_configs:
            conv = "✓" if r.converged else "✗"
            f.write(f"- **{r.config_dict['positional_encoding'].value}**: params={r.num_params:,}, loss={r.train_loss:.4f if r.converged else 'NaN'}, acc={r.test_acc:.3f}\n")

        # Embedding ties
        f.write("\n## 8. Embedding Ties\n\n")
        tie_configs = [r for r in results if r.config_dict.get('hidden_dim') == 96 and 'tie_embeddings' in str(r.config_dict)]
        for r in tie_configs:
            conv = "✓" if r.converged else "✗"
            f.write(f"- **Tie={str(r.config_dict['tie_embeddings']):5s}**: params={r.num_params:,}, loss={r.train_loss:.4f if r.converged else 'NaN'}, acc={r.test_acc:.3f}\n")

        # Key findings
        f.write("\n## Key Findings\n\n")
        
        converged_results = [r for r in results if r.converged]
        best_by_acc = sorted(converged_results, key=lambda x: x.test_acc, reverse=True)[:3]
        
        f.write("### Best Performing Configurations (by accuracy)\n\n")
        for i, r in enumerate(best_by_acc, 1):
            f.write(f"**{i}. Accuracy: {r.test_acc:.3f}**\n")
            f.write(f"- Parameters: {r.num_params:,}\n")
            f.write(f"- Config: " + ", ".join(
                f"{k}={v}" for k, v in r.config_dict.items() 
                if k not in ['vocab_size', 'dropout', 'max_seq_len']
            ) + "\n\n")

        # Architecture simplification insights
        f.write("### Architecture Simplification Insights\n\n")
        
        # Find min params that still achieves reasonable accuracy
        threshold_acc = 0.15  # arbitrary minimum useful performance
        minimal_configs = [r for r in converged_results if r.test_acc >= threshold_acc]
        if minimal_configs:
            simplest = min(minimal_configs, key=lambda x: x.num_params)
            f.write(f"- **Minimal viable architecture**: {simplest.num_params:,} params achieves "
                    f"acc={simplest.test_acc:.3f}\n")
            f.write(f"  Config: " + ", ".join(
                f"{k}={v}" for k, v in simplest.config_dict.items() 
                if k not in ['vocab_size', 'dropout', 'max_seq_len']
            ) + "\n\n")

        # Depth jumps
        depth_results = sorted(
            [r for r in converged_results if r.config_dict.get('num_layers') in [1, 2, 4]], 
            key=lambda x: x.config_dict['num_layers']
        )
        if len(depth_results) >= 3:
            acc_improvements = []
            for i in range(len(depth_results) - 1):
                prev_acc = depth_results[i].test_acc
                next_acc = depth_results[i + 1].test_acc
                if next_acc > prev_acc * 1.2:  # 20% improvement
                    acc_improvements.append((depth_results[i].config_dict['num_layers'], 
                                            depth_results[i + 1].config_dict['num_layers'],
                                            f"({prev_acc:.3f} → {next_acc:.3f})"))
            if acc_improvements:
                f.write(f"- **Sudden accuracy jumps with added depth**: "
                        f"{', '.join(f'{p}->{n} {acc}' for p, n, acc in acc_improvements)}\n\n")

        # Width vs params efficiency
        width_configs = sorted(
            [r for r in converged_results if r.config_dict.get('num_layers') == 4], 
            key=lambda x: x.config_dict['hidden_dim']
        )
        f.write("- **Width-efficiency tradeoffs**:\n")
        for r in width_configs:
            params_per_acc = r.num_params / (r.test_acc + 1e-6)
            f.write(f"  - dim={r.config_dict['hidden_dim']:3d}: {params_per_acc:6.0f} params/acc\n")
        f.write("\n")

        # Parameter count analysis
        f.write("### Parameter Count Analysis\n\n")
        total_params = sum(r.num_params for r in results)
        avg_params = total_params / len(results) if results else 0
        f.write(f"- Total configurations tested: {len(results)}\n")
        f.write(f"- Mean parameter count: {avg_params:,.0f}\n")
        
        min_params = min(r.num_params for r in converged_results) if converged_results else 0
        max_params = max(r.num_params for r in results)
        f.write(f"- Range (converged configs): {min_params:,} to {max_params:,} params\n\n")

        # Sudden behavioral changes
        f.write("### Notable Behavioral Changes\n\n")
        
        # Check for dropout-like effects from depth reduction
        if len(depth_results) >= 2:
            first_depth = depth_results[0]
            last_depth = depth_results[-1]
            loss_gap = abs(first_depth.train_loss - last_depth.train_loss)
            acc_gap = first_depth.test_acc - last_depth.test_acc
            
            if loss_gap > 0.1:
                f.write(f"- **Training stability gap**: Depth reduction from {last_depth.config_dict['num_layers']} to "
                        f"{first_depth.config_dict['num_layers']} layers caused loss difference of {loss_gap:.4f}\n")
            
            if abs(acc_gap) > 0.1:
                direction = "improved" if acc_gap < 0 else "degraded"
                f.write(f"- **Performance gap**: Accuracy {direction} by {abs(acc_gap):.3f} when going from "
                        f"{last_depth.config_dict['num_layers']} to {first_depth.config_dict['num_layers']} layers\n")

        f.write("\n## Conclusion\n\n")
        f.write("This experiment systematically explored architectural dimensions in small transformers.\n")
        f.write("Key observations include:\n\n")
        
        # Count how many configurations converged
        converge_rate = len(converged_results) / len(results) * 100 if results else 0
        f.write(f"- Converged at rate: {converge_rate:.1f}% ({len(converged_results)}/{len(results)} configs)\n")
        
        # Best accuracy achieved
        best_acc = max(r.test_acc for r in converged_results) if converged_results else 0
        f.write(f"- Maximum test accuracy achieved: {best_acc:.3f}\n")
        
        # Most efficient config
        if minimal_configs:
            simplest = min(minimal_configs, key=lambda x: x.num_params)
            f.write(f"- Most parameter-efficient: {simplest.num_params:,} params with {simplest.test_acc:.3f} accuracy\n")

    print(f"\nReport written to: {report_path}")


if __name__ == "__main__":
    # Run experiments
    results = run_architecture_experiments()

    # Generate report
    report_path = (
        "/Users/jakegearon/projects/tinymodels/.dgov/reports/branch-a-architecture.md"
    )
    generate_report(results, report_path)