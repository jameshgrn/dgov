"""Architecture exploration experiments."""

import random
from dataclasses import dataclass
from typing import List, Tuple

import torch
from torch.utils.data import Dataset, DataLoader

from .model import TransformerConfig, TinyTransformer


@dataclass
class ExperimentResult:
    """Results from an architectural configuration."""
    config: TransformerConfig
    num_params: int
    train_loss: float
    test_acc: float
    task: str


class SyntheticDataset(Dataset):
    """Synthetic dataset for testing in-context learning."""

    def __init__(self, vocab_size: int = 256, seq_len: int = 32, num_examples: int = 1000):
        self.vocab_size = vocab_size
        self.seq_len = seq_len
        self.examples = []

        for _ in range(num_examples):
            pattern_type = random.randint(0, 2)

            if pattern_type == 0:
                # Parity task: predict sum of last N bits mod 2
                prefix_len = 8
                input_seq = [random.randint(0, vocab_size - 1) for _ in range(prefix_len)]
                bits = [random.randint(0, 1) for _ in range(8)]
                target = sum(bits) % 2

                # Convert to vocabulary tokens (last dimension indicates "active" token)
                seq = input_seq + bits + [target]
                labels = [vocab_size - 1] * (prefix_len + 7) + [0 if target == 0 else vocab_size // 2]

            elif pattern_type == 1:
                # Next token prediction with simple repetition
                prefix_len = 4
                base_token = random.randint(0, vocab_size // 2 - 1)
                seq = [base_token] * 4 + [base_token] + [random.randint(vocab_size // 2, vocab_size - 1)]
                labels = [vocab_size - 1] * (prefix_len + 5) + [0]

            else:
                # Majority vote
                prefix_len = 8
                majority = random.randint(0, vocab_size // 3 - 1)
                seq = [random.randint(0, vocab_size - 1) for _ in range(prefix_len)]
                for i in range(4):
                    seq[random.randint(0, len(seq)-1)] = majority
                target = majority
                labels = [vocab_size - 1] * prefix_len + [0 if target == majority else vocab_size // 2]

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
            logits = model(tokens)[:, -1, :]  # Predict last token

            preds = torch.argmax(logits, dim=-1)
            correct += (preds == labels).sum().item()
            total += len(labels)

    return correct / total


def run_architecture_search():
    """Run systematic architecture search."""
    base_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        ffn_hidden_ratio=4.0,
        tie_embeddings=True,
        normalization="layer_norm",
        positional_encoding="absolute",
        residual_scale=1.0,
        dropout=0.0,
        max_seq_len=32,
    )

    results = []

    # Test 1: Vary depth (keep width constant)
    print("=== Depth Search ===")
    for num_layers in [1, 2, 4, 8]:
        config = TransformerConfig(
            **{**base_config.__dict__, "num_layers": num_layers}
        )
        model = TinyTransformer(config)
        dataset = SyntheticDataset()

        optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
        criterion = torch.nn.CrossEntropyLoss()

        # Train for a few epochs
        loader = DataLoader(dataset, batch_size=32, shuffle=True)
        for epoch in range(5):
            model.train()
            total_loss = 0
            for tokens, labels in loader:
                optimizer.zero_grad()
                logits = model(tokens)[:, -1, :]
                loss = criterion(logits, labels.long())
                loss.backward()
                optimizer.step()
                total_loss += loss.item()

        test_acc = evaluate_model(model, dataset)
        result = ExperimentResult(
            config=config,
            num_params=model.num_parameters(),
            train_loss=total_loss / len(loader),
            test_acc=test_acc,
            task="synthetic"
        )
        results.append(result)
        print(f"Layers={num_layers}: params={model.num_parameters():,}, test_acc={test_acc:.3f}")

    # Test 2: Vary width (keep depth constant)
    print("\n=== Width Search ===")
    for hidden_dim in [64, 128, 256]:
        config = TransformerConfig(
            **{**base_config.__dict__, "hidden_dim": hidden_dim}
        )
        model = TinyTransformer(config)
        dataset = SyntheticDataset()

        optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
        criterion = torch.nn.CrossEntropyLoss()

        loader = DataLoader(dataset, batch_size=32, shuffle=True)
        for epoch in range(5):
            model.train()
            total_loss = 0
            for tokens, labels in loader:
                optimizer.zero_grad()
                logits = model(tokens)[:, -1, :]
                loss = criterion(logits, labels.long())
                loss.backward()
                optimizer.step()
                total_loss += loss.item()

        test_acc = evaluate_model(model, dataset)
        result = ExperimentResult(
            config=config,
            num_params=model.num_parameters(),
            train_loss=total_loss / len(loader),
            test_acc=test_acc,
            task="synthetic"
        )
        results.append(result)
        print(f"Dim={hidden_dim}: params={model.num_parameters():,}, test_acc={test_acc:.3f}")

    # Test 3: Tie vs untied embeddings
    print("\n=== Embedding Tie Search ===")
    for tie in [True, False]:
        config = TransformerConfig(
            **{**base_config.__dict__, "tie_embeddings": tie}
        )
        model = TinyTransformer(config)
        dataset = SyntheticDataset()

        optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
        criterion = torch.nn.CrossEntropyLoss()

        loader = DataLoader(dataset, batch_size=32, shuffle=True)
        for epoch in range(5):
            model.train()
            total_loss = 0
            for tokens, labels in loader:
                optimizer.zero_grad()
                logits = model(tokens)[:, -1, :]
                loss = criterion(logits, labels.long())
                loss.backward()
                optimizer.step()
                total_loss += loss.item()

        test_acc = evaluate_model(model, dataset)
        result = ExperimentResult(
            config=config,
            num_params=model.num_parameters(),
            train_loss=total_loss / len(loader),
            test_acc=test_acc,
            task="synthetic"
        )
        results.append(result)
        print(f"Tie={tie}: params={model.num_parameters():,}, test_acc={test_acc:.3f}")

    # Test 4: RMS norm vs layer norm
    print("\n=== Normalization Search ===")
    for norm in ["layer_norm", "rms_norm"]:
        config = TransformerConfig(
            **{**base_config.__dict__, "normalization": norm}
        )
        model = TinyTransformer(config)
        dataset = SyntheticDataset()

        optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
        criterion = torch.nn.CrossEntropyLoss()

        loader = DataLoader(dataset, batch_size=32, shuffle=True)
        for epoch in range(5):
            model.train()
            total_loss = 0
            for tokens, labels in loader:
                optimizer.zero_grad()
                logits = model(tokens)[:, -1, :]
                loss = criterion(logits, labels.long())
                loss.backward()
                optimizer.step()
                total_loss += loss.item()

        test_acc = evaluate_model(model, dataset)
        result = ExperimentResult(
            config=config,
            num_params=model.num_parameters(),
            train_loss=total_loss / len(loader),
            test_acc=test_acc,
            task="synthetic"
        )
        results.append(result)
        print(f"Norm={norm}: params={model.num_parameters():,}, test_acc={test_acc:.3f}")

    # Test 5: Positional encoding
    print("\n=== Positional Encoding Search ===")
    for pos_enc in ["absolute", "rotary", "none"]:
        config = TransformerConfig(
            **{**base_config.__dict__, "positional_encoding": pos_enc}
        )
        model = TinyTransformer(config)
        dataset = SyntheticDataset()

        optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
        criterion = torch.nn.CrossEntropyLoss()

        loader = DataLoader(dataset, batch_size=32, shuffle=True)
        for epoch in range(5):
            model.train()
            total_loss = 0
            for tokens, labels in loader:
                optimizer.zero_grad()
                logits = model(tokens)[:, -1, :]
                loss = criterion(logits, labels.long())
                loss.backward()
                optimizer.step()
                total_loss += loss.item()

        test_acc = evaluate_model(model, dataset)
        result = ExperimentResult(
            config=config,
            num_params=model.num_parameters(),
            train_loss=total_loss / len(loader),
            test_acc=test_acc,
            task="synthetic"
        )
        results.append(result)
        print(f"PosEnc={pos_enc}: params={model.num_parameters():,}, test_acc={test_acc:.3f}")

    return results