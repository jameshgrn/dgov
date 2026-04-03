import torch
from tqdm import tqdm
import json
import os
from datetime import datetime


class Trainer:
    def __init__(
        self,
        model,
        train_loader,
        test_loader,
        optimizer,
        device=None,
        exp_name="default",
    ):
        self.model = model
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.optimizer = optimizer
        self.device = device or (
            "cuda"
            if torch.cuda.is_available()
            else "mps"
            if torch.backends.mps.is_available()
            else "cpu"
        )
        self.exp_name = exp_name
        self.model.to(self.device)

        self.log_path = (
            f"reports/{exp_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
        )
        os.makedirs("reports", exist_ok=True)

    def train(self, epochs):
        for epoch in range(epochs):
            self.model.train()
            total_loss = 0
            for x, y in tqdm(self.train_loader, desc=f"Epoch {epoch}"):
                x, y = x.to(self.device), y.to(self.device)
                logits, loss = self.model(x, y)

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

                total_loss += loss.item()

            avg_loss = total_loss / len(self.train_loader)
            test_loss, test_acc = self.evaluate()

            log_entry = {
                "epoch": epoch,
                "train_loss": avg_loss,
                "test_loss": test_loss,
                "test_acc": test_acc,
            }
            with open(self.log_path, "a") as f:
                f.write(json.dumps(log_entry) + "\n")

            print(
                f"Epoch {epoch}: Train Loss: {avg_loss:.4f}, Test Loss: {test_loss:.4f}, Test Acc: {test_acc:.4f}"
            )

    @torch.no_grad()
    def evaluate(self):
        self.model.eval()
        total_loss = 0
        correct = 0
        total = 0
        for x, y in self.test_loader:
            x, y = x.to(self.device), y.to(self.device)
            logits, loss = self.model(x, y)
            total_loss += loss.item()

            preds = torch.argmax(logits[:, -1, :], dim=-1)
            correct += (preds == y[:, -1]).sum().item()
            total += y.size(0)

        return total_loss / len(self.test_loader), correct / total
