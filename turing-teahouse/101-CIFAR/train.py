"""
Fixed CIFAR-10 training script: hyperparameters only via CLI; no search logic here.

Evaluates on the official test set (`train=False`) on a sparse-then-dense epoch schedule
(e.g. 40 epochs -> checkpoints at 1,10,20,25,30,35,36,37,38,39,40).

Usage: uv run python train.py --lr 0.01 --epochs 40

Stdout: (1–2) comma-separated epoch indices and test accuracies, then (3) **`SUCCESS`** or **`ERROR`**.
**stderr** uses `INFO: …` for progress; no files are written.

`train_once(cfg)` returns a dict (including `eval_epochs`, `test_acc`, `val_acc`, etc.).
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import asdict, dataclass
from typing import Any

import torch
import torch.nn as nn
import torch.nn.functional as F
import torchvision
import torchvision.transforms as T
from torch.utils.data import DataLoader

# CIFAR-10 101 cabinet: max training length for this dataset / arena rules.
MAX_EPOCHS = 80

# Fixed run environment (not CLI); edit here.
SEED = 42
NUM_WORKERS = 2
DATA_ROOT = "./data"
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


@dataclass
class TrainConfig:
    lr: float = 0.001
    weight_decay: float = 0.0
    batch_size: int = 128
    epochs: int = 10
    momentum: float = 0.9


class SmallCNN(nn.Module):
    """Small CNN matching the PyTorch CIFAR tutorial, for 32×32 inputs."""

    def __init__(self, num_classes: int = 10) -> None:
        super().__init__()
        self.conv1 = nn.Conv2d(3, 6, 5)
        self.pool = nn.MaxPool2d(2, 2)
        self.conv2 = nn.Conv2d(6, 16, 5)
        self.fc1 = nn.Linear(16 * 5 * 5, 120)
        self.fc2 = nn.Linear(120, 84)
        self.fc3 = nn.Linear(84, num_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.pool(F.relu(self.conv1(x)))
        x = self.pool(F.relu(self.conv2(x)))
        x = x.view(-1, 16 * 5 * 5)
        x = F.relu(self.fc1(x))
        x = F.relu(self.fc2(x))
        return self.fc3(x)


def set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _info(msg: str) -> None:
    """Progress on stderr; stdout is curve + SUCCESS/ERROR only."""
    print(f"INFO: {msg}", file=sys.stderr, flush=True)


def eval_epoch_schedule(total_epochs: int) -> list[int]:
    """
    Increasingly dense test-set checkpoints: every 10 early, every 5 from 25,
    then every epoch in the last 5 (matches e.g. 40 -> 1,10,20,25,30,35,36..40).
    """
    if total_epochs < 1:
        return []
    check: set[int] = {1, total_epochs}
    tail = min(5, total_epochs)
    dense_from = total_epochs - tail + 1
    for e in range(10, total_epochs, 10):
        if e < dense_from:
            check.add(e)
    for e in range(25, dense_from, 5):
        if 1 <= e <= total_epochs:
            check.add(e)
    for e in range(dense_from, total_epochs + 1):
        check.add(e)
    return sorted(check)


@torch.no_grad()
def evaluate_test_accuracy(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    criterion: nn.Module,
) -> tuple[float, float]:
    """Returns (loss, accuracy) on the loader."""
    model.eval()
    correct = 0
    total = 0
    loss_sum = 0.0
    for inputs, targets in loader:
        inputs = inputs.to(device, non_blocking=True)
        targets = targets.to(device, non_blocking=True)
        logits = model(inputs)
        loss_sum += criterion(logits, targets).item() * targets.size(0)
        pred = logits.argmax(dim=1)
        correct += (pred == targets).sum().item()
        total += targets.size(0)
    return loss_sum / total, correct / total


def train_once(cfg: TrainConfig) -> dict[str, Any]:
    if not (1 <= cfg.epochs <= MAX_EPOCHS):
        raise ValueError(f"epochs must be in [1, {MAX_EPOCHS}], got {cfg.epochs}")
    set_seed(SEED)
    device = torch.device(DEVICE)
    _info(f"loading CIFAR-10 under {DATA_ROOT!r} (first run downloads ~170MB)")

    transform_train = T.Compose(
        [
            T.RandomHorizontalFlip(),
            T.RandomCrop(32, padding=4),
            T.ToTensor(),
            T.Normalize((0.4914, 0.4822, 0.4465), (0.2470, 0.2435, 0.2616)),
        ]
    )
    transform_val = T.Compose(
        [
            T.ToTensor(),
            T.Normalize((0.4914, 0.4822, 0.4465), (0.2470, 0.2435, 0.2616)),
        ]
    )

    train_set = torchvision.datasets.CIFAR10(
        root=DATA_ROOT, train=True, download=True, transform=transform_train
    )
    test_set = torchvision.datasets.CIFAR10(
        root=DATA_ROOT, train=False, download=True, transform=transform_val
    )

    train_loader = DataLoader(
        train_set,
        batch_size=cfg.batch_size,
        shuffle=True,
        num_workers=NUM_WORKERS,
        pin_memory=device.type == "cuda",
    )
    test_loader = DataLoader(
        test_set,
        batch_size=cfg.batch_size,
        shuffle=False,
        num_workers=NUM_WORKERS,
        pin_memory=device.type == "cuda",
    )
    _info(f"training on {device}, epochs={cfg.epochs}")

    model = SmallCNN().to(device)
    criterion = nn.CrossEntropyLoss()
    optimizer = torch.optim.SGD(
        model.parameters(),
        lr=cfg.lr,
        momentum=cfg.momentum,
        weight_decay=cfg.weight_decay,
    )

    schedule = set(eval_epoch_schedule(cfg.epochs))
    eval_epochs: list[int] = []
    test_acc_list: list[float] = []
    test_loss_list: list[float] = []

    try:
        for epoch in range(1, cfg.epochs + 1):
            model.train()
            for inputs, targets in train_loader:
                inputs = inputs.to(device, non_blocking=True)
                targets = targets.to(device, non_blocking=True)
                optimizer.zero_grad(set_to_none=True)
                logits = model(inputs)
                loss = criterion(logits, targets)
                loss.backward()
                optimizer.step()

            if epoch in schedule:
                te_loss, te_acc = evaluate_test_accuracy(
                    model, test_loader, device, criterion
                )
                eval_epochs.append(epoch)
                test_loss_list.append(float(te_loss))
                test_acc_list.append(float(te_acc))
                _info(f"epoch {epoch}/{cfg.epochs} test_acc={te_acc:.4f}")
            else:
                _info(f"epoch {epoch}/{cfg.epochs}")

        val_loss = test_loss_list[-1]
        val_acc = test_acc_list[-1]
        return {
            "status": "ok",
            "val_loss": val_loss,
            "val_acc": val_acc,
            "epochs": cfg.epochs,
            "eval_epochs": eval_epochs,
            "test_acc": test_acc_list,
            "test_loss": test_loss_list,
        }
    except RuntimeError as e:
        if "out of memory" in str(e).lower():
            if device.type == "cuda":
                torch.cuda.empty_cache()
            return {
                "status": "oom",
                "val_loss": float("nan"),
                "val_acc": float("nan"),
                "epochs": cfg.epochs,
                "eval_epochs": eval_epochs,
                "test_acc": test_acc_list,
                "test_loss": test_loss_list,
                "error": str(e),
            }
        raise


def parse_args(argv: list[str] | None = None) -> TrainConfig:
    p = argparse.ArgumentParser(description="CIFAR-10 fixed architecture; hyperparameters only")
    p.add_argument("--lr", type=float, default=TrainConfig.lr)
    p.add_argument("--weight-decay", type=float, default=TrainConfig.weight_decay)
    p.add_argument("--batch-size", type=int, default=TrainConfig.batch_size)
    p.add_argument(
        "--epochs",
        type=int,
        default=TrainConfig.epochs,
        help=f"Training epochs (1–{MAX_EPOCHS} for this dataset)",
    )
    p.add_argument("--momentum", type=float, default=TrainConfig.momentum)
    args = p.parse_args(argv)
    if not (1 <= args.epochs <= MAX_EPOCHS):
        p.error(f"--epochs must be between 1 and {MAX_EPOCHS} (inclusive), got {args.epochs}")
    return TrainConfig(
        lr=args.lr,
        weight_decay=args.weight_decay,
        batch_size=args.batch_size,
        epochs=args.epochs,
        momentum=args.momentum,
    )


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    run = train_once(cfg)
    result = {
        **asdict(cfg),
        **run,
        "seed": SEED,
        "num_workers": NUM_WORKERS,
        "data_root": DATA_ROOT,
        "device": DEVICE,
    }
    ee = result.get("eval_epochs") or []
    ta = result.get("test_acc") or []
    line1 = ",".join(str(x) for x in ee)
    line2 = ",".join(f"{x:.4f}" for x in ta)
    status_word = "SUCCESS" if result.get("status") == "ok" else "ERROR"
    print(f"{line1}\n{line2}\n{status_word}", flush=True)
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
