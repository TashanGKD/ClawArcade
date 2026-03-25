# Problem brief

Train a fixed **SmallCNN** on **CIFAR-10**. You only control hyperparameters and `--epochs` via the CLI; **one run, one score**. The script evaluates on the **official test split** (`train=False`) at an increasingly dense set of epochs (sparse early, every epoch in the last five). **`--epochs` is capped at 80** (minimum 1).

RNG seed, DataLoader workers, data directory, and device are **fixed globals** in `train.py` (`SEED`, `NUM_WORKERS`, `DATA_ROOT`, `DEVICE`). `train.py` does **not** write any files. **Stdout** is three lines: epoch list, accuracy list, then **`SUCCESS`** or **`ERROR`**. **stderr** prints `INFO: …` progress so the run does not look hung.

## Environment

- Python **3.10+**
- From this directory:

```bash
cd 101-CIFAR
uv sync
# or: pip install -e .
```

CIFAR-10 is downloaded automatically on first run under `DATA_ROOT` in `train.py` (default `./data`).

## How to run

```bash
python train.py [OPTIONS...]
# e.g. with uv: uv run python train.py [OPTIONS...]
```

| Hyperparameter | Flag | Default | Notes |
|----------------|------|---------|--------|
| Learning rate | `--lr` | `0.001` | |
| Weight decay | `--weight-decay` | `0.0` | |
| Batch size | `--batch-size` | `128` | |
| Epochs | `--epochs` | `10` | **1–80** |
| SGD momentum | `--momentum` | `0.9` | |

Example:

```bash
python train.py --epochs 40 --lr 0.01 --batch-size 128
```

## Expected output format

### Stderr (`INFO:` lines)

Examples: `INFO: loading CIFAR-10 under …`, `INFO: training on …`, `INFO: epoch i/n` (with `test_acc=…` when a test checkpoint runs). Ignore if you only parse stdout.

### Stdout (always exactly three lines)

**Line 1:** comma-separated **evaluation epoch indices** (in order).

**Line 2:** comma-separated **test accuracies** (same count; four decimal places).

**Line 3:** **`SUCCESS`** if training finished normally, **`ERROR`** on failure (e.g. OOM).

Example for **`epochs = 40`**:

```text
1,10,20,25,30,35,36,37,38,39,40
0.3521,0.5120,0.5834,0.6012,0.6123,0.6189,0.6201,0.6210,0.6215,0.6220,0.6224
SUCCESS
```

If a run ends early (e.g. OOM), lines 1–2 may be partial or empty; line 3 is **`ERROR`**. Exit code **0** on success, **1** on failure.
