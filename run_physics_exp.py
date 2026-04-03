import argparse
import torch
import torch.optim as optim
from src.models.physics_model import PhysicsTransformer
from src.data.river_physics import get_river_dataloader
import json
import os
from datetime import datetime
from tqdm import tqdm


def train(args):
    dataloader = get_river_dataloader(batch_size=args.batch_size)
    model = PhysicsTransformer(
        n_nodes=64,
        n_embd=args.n_embd,
        n_head=args.n_head,
        n_layer=args.n_layer,
    )

    device = torch.device(
        "cuda"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    model.to(device)

    optimizer = optim.AdamW(model.parameters(), lr=args.lr)

    os.makedirs("reports", exist_ok=True)
    log_path = f"reports/physics_{args.exp_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

    for epoch in range(args.epochs):
        model.train()
        total_loss = 0
        for x, y in tqdm(dataloader, desc=f"Epoch {epoch}"):
            x, y = x.to(device), y.to(device)
            _, loss = model(x, y)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        avg_loss = total_loss / len(dataloader)
        print(f"Epoch {epoch}: MSE Loss: {avg_loss:.6f}")

        with open(log_path, "a") as f:
            f.write(json.dumps({"epoch": epoch, "loss": avg_loss}) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--exp_id", type=str, default="exner_001")
    parser.add_argument("--n_embd", type=int, default=128)
    parser.add_argument("--n_head", type=int, default=4)
    parser.add_argument("--n_layer", type=int, default=2)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch_size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=1e-4)
    args = parser.parse_args()
    train(args)
