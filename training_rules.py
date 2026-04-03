"""Training rule mutations: optimizer, schedules, regularization, noise."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import torch
import torch.nn.functional as F
from torch.optim import Adam, AdamW, SGD, RMSprop, Adagrad, Optimizer


class OptimizerFamily(str, Enum):
    """Optimizer family choices."""

    ADAM = "adam"
    ADAMW = "adamw"
    SGD_MOMENTUM = "sgd_momentum"
    RMSPROP = "rmsprop"
    ADAGRAD = "adagrad"


class LRSchedule(str, Enum):
    """Learning rate schedule choices."""

    CONSTANT = "constant"
    STEP_DECAY = "step_decay"
    EXPONENTIAL_DECAY = "exponential_decay"
    COSINE_ANNEALING = "cosine_annealing"
    WARMUP_COSINE = "warmup_cosine"


class WeightDecayRule(str, Enum):
    """Weight decay rule choices."""

    NONE = "none"
    FIXED = "fixed"
    ADAPTIVE = "adaptive"
    LAYERWISE = "layerwise"


class GradientClipping(str, Enum):
    """Gradient clipping strategy."""

    NONE = "none"
    VALUE_CLIP = "value_clip"
    NORM_CLIP = "norm_clip"


class NoiseInjection(str, Enum):
    """Noise injection strategies."""

    NONE = "none"
    GRADIENT_NOISE = "gradient_noise"
    PARAMETER_NOISE = "parameter_noise"


@dataclass
class TrainingRules:
    """Complete training rule configuration."""

    optimizer: OptimizerFamily = OptimizerFamily.ADAMW
    lr: float = 0.001
    lr_schedule: LRSchedule = LRSchedule.COSINE_ANNEALING
    weight_decay_rule: WeightDecayRule = WeightDecayRule.NONE
    weight_decay: float = 0.01
    gradient_clipping: GradientClipping = GradientClipping.NORM_CLIP
    clip_value: float = 1.0
    noise_injection: NoiseInjection = NoiseInjection.NONE
    noise_std: float = 0.0
    warmup_steps: int = 100


class LearningRateScheduler:
    """LR scheduler with multiple schedule types."""

    def __init__(
        self,
        optimizer: Optimizer,
        total_steps: int,
        schedule: LRSchedule = LRSchedule.COSINE_ANNEALING,
        warmup_steps: int = 0,
        base_lr: float = 0.001,
    ):
        self.optimizer = optimizer
        self.total_steps = total_steps
        self.schedule = schedule
        self.warmup_steps = warmup_steps
        self.base_lr = base_lr

    def step(self, global_step: int):
        """Update LR based on schedule."""
        if self.schedule == LRSchedule.CONSTANT:
            new_lr = self.base_lr
        elif self.schedule == LRSCHEDULE.STEP_DECAY:
            # Decay by 0.1 every 25% of training
            milestones = [
                int(self.total_steps * 0.25),
                int(self.total_steps * 0.5),
                int(self.total_steps * 0.75),
            ]
            decay_rate = 0.1 ** sum(global_step >= m for m in milestones)
            new_lr = self.base_lr * decay_rate
        elif self.schedule == LRSCHEDULE.EXPONENTIAL_DECAY:
            gamma = 0.98
            new_lr = self.base_lr * (gamma ** global_step)
        elif self.schedule == LRSCHEDULE.COSINE_ANNEALING:
            progress = max(global_step / self.total_steps, 1e-6)
            new_lr = self.base_lr * 0.5 * (1 + F.cos(progress * F.pi))
        elif self.schedule == LRSCHEDULE.WARMUP_COSINE:
            if global_step < self.warmup_steps:
                # Linear warmup
                new_lr = self.base_lr * (global_step / self.warmup_steps)
            else:
                progress = (global_step - self.warmup_steps) / max(
                    self.total_steps - self.warmup_steps, 1e-6
                )
                new_lr = self.base_lr * 0.5 * (1 + F.cos(progress * F.pi))
        else:
            new_lr = self.base_lr

        for param_group in self.optimizer.param_groups:
            param_group["lr"] = new_lr


class WeightDecayApplier:
    """Weight decay rule implementations."""

    @staticmethod
    def apply(
        params: list[torch.Tensor],
        step: int,
        base_wd: float,
        rule: WeightDecayRule,
        config_params: dict[str, torch.Tensor] | None = None,
    ) -> list[torch.Tensor]:
        """Apply weight decay rules."""

        if rule == WeightDecayRule.NONE:
            return [p.clone() for p in params]

        if rule == WeightDecayRule.FIXED:
            return [p - base_wd * p.grad.data if p.grad is not None else p.clone() for p in params]

        if rule == WeightDecayRule.ADAPTIVE:
            # Decay scales with lr (typical AdamW behavior)
            adapted_wd = base_wd * 0.1 ** (step / 10000)
            return [p - adapted_wd * p.grad.data if p.grad is not None else p.clone() for p in params]

        if rule == WeightDecayRule.LAYERWISE:
            new_params = []
            for i, p in enumerate(params):
                layer_factor = 1.0 / (i + 1) ** 0.5
                wd_scale = base_wd * layer_factor
                if p.grad is not None:
                    new_params.append(p - wd_scale * p.grad.data)
                else:
                    new_params.append(p.clone())
            return new_params

        return [p.clone() for p in params]


class GradientClippingHandler:
    """Gradient clipping strategies."""

    @staticmethod
    def clip(
        model: torch.nn.Module,
        strategy: GradientClipping,
        value: float = 1.0,
        max_norm: float = 1.0,
    ) -> bool:
        """Apply gradient clipping. Returns True if clipping occurred."""

        total_norm = 0.0
        for p in model.parameters():
            if p.grad is not None:
                param_norm = p.grad.data.norm(2)
                total_norm += param_norm.item() ** 2
        total_norm = total_norm**0.5

        clipped = False

        if strategy == GradientClipping.VALUE_CLIP:
            torch.nn.utils.clip_grad_value_(model.parameters(), value)
            clipped = True
        elif strategy == GradientClipping.NORM_CLIP:
            if total_norm > max_norm:
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm)
                clipped = True

        return clipped, total_norm


class NoiseInjector:
    """Noise injection strategies."""

    @staticmethod
    def add_gradient_noise(
        model: torch.nn.Module, std: float, global_step: int
    ) -> None:
        """Add Gaussian noise to gradients."""
        for p in model.parameters():
            if p.grad is not None:
                noise = torch.randn_like(p.grad) * std * (1.0 - 0.99 * min(global_step / 5000, 1.0))
                p.grad.data += noise

    @staticmethod
    def add_parameter_noise(
        model: torch.nn.Module, std: float
    ) -> list[torch.Tensor]:
        """Return parameters with injected noise."""
        noisy_params = []
        for p in model.parameters():
            noisy_p = p.clone()
            if p.grad is not None and std > 0:
                noise = torch.randn_like(p) * std
                noisy_p += noise
            noisy_params.append(noisy_p)
        return noisy_params


def build_optimizer(
    model: torch.nn.Module,
    config: TrainingRules,
) -> Optimizer:
    """Build optimizer based on training rules."""

    if config.optimizer == OptimizerFamily.ADAM:
        return Adam(model.parameters(), lr=config.lr, betas=(0.9, 0.999), weight_decay=0.0)
    elif config.optimizer == OptimizerFamily.ADAMW:
        return AdamW(model.parameters(), lr=config.lr, betas=(0.9, 0.999), weight_decay=config.weight_decay)
    elif config.optimizer == OptimizerFamily.SGD_MOMENTUM:
        return SGD(model.parameters(), lr=config.lr, momentum=0.9, nesterov=True)
    elif config.optimizer == OptimizerFamily.RMSPROP:
        return RMSprop(model.parameters(), lr=config.lr, alpha=0.99, eps=1e-8)
    elif config.optimizer == OptimizerFamily.ADAGRAD:
        return Adagrad(model.parameters(), lr=config.lr, lr_decay=0.0)

    raise ValueError(f"Unknown optimizer: {config.optimizer}")


def create_training_loop(
    model: torch.nn.Module,
    config: TrainingRules,
    loader: torch.utils.data.DataLoader,
    criterion: torch.nn.Module,
    total_steps: int,
) -> tuple[Callable, Callable, str]:
    """Create training loop components."""

    optimizer = build_optimizer(model, config)
    lr_scheduler = LearningRateScheduler(
        optimizer, total_steps, config.lr_schedule, config.warmup_steps, config.lr
    )

    history: dict[str, list[float]] = {
        "train_loss": [],
        "test_acc": [],
        "grad_norms": [],
        "lr_values": [],
        "clipped_count": [0],
        "max_grad_norm": [float("-inf")],
    }

    def train_step(batch, global_step: int) -> float:
        tokens, labels = batch
        model.train()
        optimizer.zero_grad()

        logits = model(tokens)[:, -1, :]
        loss = criterion(logits, labels.long())

        loss.backward()

        # Add gradient noise if configured
        if config.noise_injection == NoiseInjection.GRADIENT_NOISE:
            NoiseInjector.add_gradient_noise(model, config.noise_std, global_step)

        # Clip gradients
        clipped, norm = GradientClippingHandler.clip(
            model, config.gradient_clipping, config.clip_value, config.clip_value
        )
        history["clipped_count"][0] += int(clipped)
        history["max_grad_norm"][0] = max(history["max_grad_norm"][0], norm)

        # Apply weight decay manually if not using built-in (AdamW handles this automatically)
        if config.optimizer != OptimizerFamily.ADAMW:
            for p in model.parameters():
                if p.grad is not None and config.weight_decay > 0:
                    if config.weight_decay_rule == WeightDecayRule.FIXED:
                        p.grad.data += config.weight_decay * p.data

        optimizer.step()
        lr_scheduler.step(global_step)

        history["train_loss"].append(loss.item())
        history["grad_norms"].append(norm)
        history["lr_values"].append(optimizer.param_groups[0]["lr"])

        return loss.item()

    def get_state() -> dict:
        """Get current training state."""
        return {
            "current_lr": optimizer.param_groups[0]["lr"],
            "mean_grad_norm": torch.tensor(history["grad_norms"][-10:]).mean().item() if history["grad_norms"] else 0.0,
            "clip_ratio": history["clipped_count"][0] / max(global_step, 1),
        }

    def reset_history():
        """Reset history for new run."""
        for k in history:
            if isinstance(history[k], list):
                history[k].clear()
            elif isinstance(history[k], (int, float)):
                history[k] = type(history[k])(0)
            else:
                history[k][0] = 0

    return train_step, get_state, str(config)


def analyze_optimizers_and_schedules(
    base_lr_range: list[float] = [1e-4, 3e-4, 1e-3, 3e-3, 1e-2],
    optimizers_to_test: list[OptimizerFamily] | None = None,
    schedules_to_test: list[LRSchedule] | None = None,
) -> list[dict]:
    """Search over optimizer and schedule combinations."""

    from tinymodels.model import TransformerConfig, TinyTransformer
    from tinymodels.experiment import SyntheticDataset, evaluate_model
    from torch.utils.data import DataLoader

    optimizers_to_test = optimizers_to_test or [
        OptimizerFamily.ADAM,
        OptimizerFamily.ADAMW,
        OptimizerFamily.SGD_MOMENTUM,
        OptimizerFamily.RMSPROP,
    ]

    schedules_to_test = schedules_to_test or [
        LRSchedule.CONSTANT,
        LRSCHEDULE.STEP_DECAY,
        LRSCHEDULE.COSINE_ANNEALING,
        LRSCHEDULE.WARMUP_COSINE,
    ]

    results = []

    print("=" * 60)
    print("OPTIMIZER AND LEARNING RATE SCHEDULE EXPLORATION")
    print("=" * 60)

    # Base config
    model_config = TransformerConfig(
        vocab_size=256,
        hidden_dim=128,
        num_layers=4,
        num_heads=4,
        tie_embeddings=True,
        max_seq_len=32,
    )
    dataset = SyntheticDataset(vocab_size=256, seq_len=32)
    loader = DataLoader(dataset, batch_size=32, shuffle=True)

    # Test each optimizer with each schedule
    for opt in optimizers_to_test:
        for sched in schedules_to_test:
            for lr in base_lr_range:
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
                criterion = torch.nn.CrossEntropyLoss()
                total_steps = 500

                train_step, get_state, config_str = create_training_loop(
                    model, config, loader, criterion, total_steps
                )

                # Training loop with instability detection
                is_stable = True
                train_losses = []
                max_loss = float("-inf")

                for step in range(total_steps):
                    batch = next(iter(loader))
                    loss = train_step(batch, step)

                    if torch.isnan(torch.tensor(loss)) or torch.isinf(torch.tensor(loss)):
                        is_stable = False
                        print(f"  INSTABILITY at step {step}: optimizer={opt}, schedule={sched}, lr={lr}")
                        break

                    train_losses.append(loss)
                    max_loss = max(max_loss, loss)

                if is_stable:
                    test_acc = evaluate_model(model, dataset, num_batches=20)
                    final_lr = get_state()["current_lr"]
                    results.append(
                        {
                            "config": str(config),
                            "optimizer": opt.value,
                            "schedule": sched.value,
                            "lr": lr,
                            "final_loss": train_losses[-1] if train_losses else None,
                            "max_loss": max_loss,
                            "test_acc": test_acc,
                            "stable": True,
                        }
                    )
                    print(
                        f"  {opt.value:12s} | {sched.value:16s} | lr={lr:.4f} | loss={train_losses[-1]:.4f} | acc={test_acc:.3f}"
                    )

    return results