"""Research experiment: High weight decay effects on mod_add grokking."""

import argparse
import torch.optim as optim
from src.models.transformer import TinyTransformer
from src.data.toy_tasks import ModularAddition, get_dataloader
from src.utils.trainer import Trainer


def run(args):
    # Dataset selection
    train_ds = ModularAddition(
        p=args.p, split=args.split, mode="train", seed=args.seed
    )
    test_ds = ModularAddition(
        p=args.p, split=args.split, mode="test", seed=args.seed
    )
    vocab_size = args.p + 2  # p numbers + separator + padding

    train_loader = get_dataloader(train_ds, batch_size=args.batch_size)
    test_loader = get_dataloader(test_ds, batch_size=args.batch_size, shuffle=False)

    # Model config: fixed small architecture
    model = TinyTransformer(
        vocab_size=vocab_size,
        n_embd=args.n_embd,
        n_head=args.n_head,
        n_layer=args.n_layer,
        block_size=args.block_size,
        dropout=args.dropout,
        tie_weights=args.tie_weights,
        norm_type=args.norm_type,
    )

    # Optimizer selection
    if args.optimizer == "adamw":
        optimizer = optim.AdamW(
            model.parameters(), lr=args.lr, weight_decay=args.weight_decay
        )
    elif args.optimizer == "sgd":
        optimizer = optim.SGD(
            model.parameters(), lr=args.lr, weight_decay=args.weight_decay, momentum=0.9
        )
    else:
        raise ValueError(f"Unknown optimizer: {args.optimizer}")

    trainer = Trainer(
        model=model,
        train_loader=train_loader,
        test_loader=test_loader,
        optimizer=optimizer,
        exp_name=f"{args.optimizer}_wd{args.weight_decay}_{args.exp_id}",
    )

    trainer.train(args.epochs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--optimizer", type=str, default="adamw")
    parser.add_argument("--exp_id", type=str, default="001")
    parser.add_argument("--p", type=int, default=113)
    parser.add_argument("--split", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=42)

    # Fixed small architecture for mod_add
    parser.add_argument("--n_embd", type=int, default=64)
    parser.add_argument("--n_head", type=int, default=1)
    parser.add_argument("--n_layer", type=int, default=1)
    parser.add_argument("--block_size", type=int, default=128)
    parser.add_argument("--dropout", type=float, default=0.0)
    parser.add_argument("--tie_weights", type=bool, default=True)
    parser.add_argument("--norm_type", type=str, default="layer")

    parser.add_argument("--batch_size", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--lr", type=float, default=1e-2)
    parser.add_argument("--weight_decay", type=float, default=0.1)

    args = parser.parse_args()
    run(args)