import argparse
import torch.optim as optim
from src.models.transformer import TinyTransformer
from src.data.toy_tasks import ModularAddition, ParityTask, get_dataloader
from src.utils.trainer import Trainer


def run(args):
    # Dataset selection
    if args.task == "mod_add":
        train_ds = ModularAddition(
            p=args.p, split=args.split, mode="train", seed=args.seed
        )
        test_ds = ModularAddition(
            p=args.p, split=args.split, mode="test", seed=args.seed
        )
        vocab_size = args.p + 2  # p numbers + separator + padding
    elif args.task == "parity":
        train_ds = ParityTask(seq_len=args.seq_len, num_samples=args.num_samples)
        test_ds = ParityTask(seq_len=args.seq_len, num_samples=args.num_samples // 10)
        vocab_size = 4  # 0, 1, separator, parity_result
    else:
        raise ValueError(f"Unknown task: {args.task}")

    train_loader = get_dataloader(train_ds, batch_size=args.batch_size)
    test_loader = get_dataloader(test_ds, batch_size=args.batch_size, shuffle=False)

    # Model config
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

    optimizer = optim.AdamW(
        model.parameters(), lr=args.lr, weight_decay=args.weight_decay
    )

    trainer = Trainer(
        model=model,
        train_loader=train_loader,
        test_loader=test_loader,
        optimizer=optimizer,
        exp_name=f"{args.task}_{args.exp_id}",
    )

    trainer.train(args.epochs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", type=str, default="mod_add")
    parser.add_argument("--exp_id", type=str, default="001")
    parser.add_argument("--p", type=int, default=113)
    parser.add_argument("--split", type=float, default=0.8)
    parser.add_argument("--seq_len", type=int, default=16)
    parser.add_argument("--num_samples", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)

    parser.add_argument("--n_embd", type=int, default=128)
    parser.add_argument("--n_head", type=int, default=4)
    parser.add_argument("--n_layer", type=int, default=2)
    parser.add_argument("--block_size", type=int, default=128)
    parser.add_argument("--dropout", type=float, default=0.0)
    parser.add_argument("--tie_weights", type=bool, default=True)
    parser.add_argument("--norm_type", type=str, default="layer")

    parser.add_argument("--batch_size", type=int, default=64)
    parser.add_argument("--epochs", type=int, default=10)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--weight_decay", type=float, default=1e-1)

    args = parser.parse_args()
    run(args)
