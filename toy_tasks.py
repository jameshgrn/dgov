"""Toy tasks for research on emergent behavior in tiny transformers."""

import random
import torch
from torch.utils.data import Dataset, DataLoader


class ModularAddition(Dataset):
    """
    Modular Addition Task: Given two numbers (a, b) mod p, predict their sum.
    
    Designed to study phase transitions in learning arithmetic operations.
    Grokking phenomena observed: sudden transition from random to perfect accuracy.
    
    Args:
        p: Modulo prime (larger p = harder task)
        split: Train/test split ratio
        mode: 'train' or 'test'
        seed: Random seed for reproducibility
    """

    def __init__(self, p: int = 113, split: float = 0.8, mode: str = "train", seed: int = 42):
        self.p = p
        self.seed = seed
        random.seed(seed)

        # Generate all possible examples (a + b mod p) where a, b < p
        examples = []
        for a in range(p):
            for b in range(p):
                target = (a + b) % p
                # Create sequence: [a, b, target] with padding token at end
                seq_len = 3
                # Convert to vocabulary tokens (0..p+1 where p is padding, p+1 is start of answer)
                seq = [a, b, target, p + 1]
                labels = [p + 1] * 2 + [target] + [p + 1]
                examples.append((torch.tensor(seq), torch.tensor(labels)))

        # Shuffle and split
        random.shuffle(examples)
        n_train = int(len(examples) * split)

        if mode == "train":
            self.examples = examples[:n_train]
        else:
            self.examples = examples[n_train:]

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]


class ParityTask(Dataset):
    """
    Binary Parity Task: Given a sequence of bits, predict if their sum is even or odd.
    
    Requires counting and modulo-2 arithmetic - a classic test for emergence in transformers.
    
    Args:
        seq_len: Length of bit sequences (must be even)
        num_samples: Number of examples to generate
    """

    def __init__(self, seq_len: int = 32, num_samples: int = 1000):
        self.seq_len = seq_len
        self.num_samples = num_samples
        self.examples = []

        vocab_size = 4  # Tokens: 0-1 for bits, 2 for padding, 3 for start-of-answer
        two = 2
        three = 3

        for _ in range(num_samples):
            # Generate random bit sequence
            bits = [random.randint(0, 1) for _ in range(seq_len - 1)]
            parity = sum(bits) % 2

            # Create sequence: bits + target + padding
            seq = bits + [parity, two]
            labels = [three] * seq_len + [parity if parity == 0 else vocab_size // 2]

            self.examples.append((torch.tensor(seq), torch.tensor(labels)))

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        return self.examples[idx]


def get_dataloader(dataset: Dataset, batch_size: int = 32, shuffle: bool = True) -> DataLoader:
    """Create a dataloader from a dataset."""
    return DataLoader(dataset, batch_size=batch_size, shuffle=shuffle)