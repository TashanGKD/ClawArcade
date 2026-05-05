#!/usr/bin/env python3
"""Refit missing GP views from raw DATA_SAMPLE light curves.

This is a gap-filler for sources whose canonical/archived curve payloads are
not present but whose raw photometry exists in the public sample pool. It writes
the same ``*_sample_gp.png`` filenames used by the relay manifest.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "full-manifest.json"
LC_DIR = Path(r"C:\Unsupervised\DATA\DATA_Query\DATA_600_Sample\level0\sample\lightcurves")
OUT_DIR = ROOT / "all_sample_gp"


def source_id_from_gp_name(path: Path) -> str:
    return path.name[: -len("_sample_gp.png")]


def fallback_sources(manifest_path: Path, out_dir: Path) -> list[str]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    existing = {
        source_id_from_gp_name(path)
        for path in out_dir.glob("*_sample_gp.png")
        if path.stat().st_size > 1000 and path.name.endswith("_sample_gp.png")
    }
    return [
        str(item["source_id"])
        for item in data.get("items") or []
        if str(item.get("source_id") or "") not in existing
    ]


def load_band_points(lc_path: Path) -> dict[str, dict[str, np.ndarray]]:
    bands: dict[str, dict[str, list[float]]] = {
        "g": {"x": [], "y": [], "err": []},
        "r": {"x": [], "y": [], "err": []},
    }
    with lc_path.open("r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            filt = str(row.get("filtercode") or "").strip().lower()
            band = "g" if filt.endswith("g") else "r" if filt.endswith("r") else ""
            if not band:
                continue
            try:
                mjd = float(row.get("mjd") or "nan")
                mag = float(row.get("mag") or "nan")
                err = float(row.get("magerr") or "nan")
            except ValueError:
                continue
            if not (math.isfinite(mjd) and math.isfinite(mag)):
                continue
            bands[band]["x"].append(mjd)
            bands[band]["y"].append(mag)
            bands[band]["err"].append(err if math.isfinite(err) and err > 0 else 0.08)
    return {
        band: {key: np.asarray(vals, dtype=float) for key, vals in payload.items()}
        for band, payload in bands.items()
    }


def downsample(x: np.ndarray, y: np.ndarray, err: np.ndarray, max_points: int = 260) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    order = np.argsort(x)
    x = x[order]
    y = y[order]
    err = err[order]
    if x.size <= max_points:
        return x, y, err
    # Keep the time span while preventing dense cadence regions from dominating
    # the cubic GP solve.
    idx = np.unique(np.linspace(0, x.size - 1, max_points).round().astype(int))
    return x[idx], y[idx], err[idx]


def fit_band(x: np.ndarray, y: np.ndarray, err: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    if x.size < 5:
        return None
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import ConstantKernel, RBF, WhiteKernel

    x, y, err = downsample(x, y, err)
    x0 = float(np.nanmedian(x))
    scale = max(float(np.nanmax(x) - np.nanmin(x)), 1.0)
    xn = ((x - x0) / scale).reshape(-1, 1)
    amp = max(float(np.nanpercentile(y, 95) - np.nanpercentile(y, 5)), 0.05)
    kernel = ConstantKernel(amp * amp, (1e-4, 10.0)) * RBF(length_scale=0.18, length_scale_bounds=(0.01, 3.0)) + WhiteKernel(
        noise_level=max(float(np.nanmedian(err) ** 2), 1e-4),
        noise_level_bounds=(1e-5, 1.0),
    )
    gp = GaussianProcessRegressor(kernel=kernel, alpha=np.clip(err, 0.02, 0.5) ** 2, normalize_y=True, n_restarts_optimizer=1)
    gp.fit(xn, y)
    fine_x = np.linspace(float(np.nanmin(x)), float(np.nanmax(x)), 220)
    fine_n = ((fine_x - x0) / scale).reshape(-1, 1)
    fine_y, fine_std = gp.predict(fine_n, return_std=True)
    return fine_x, fine_y, fine_std


def render_refit(source_id: str, lc_path: Path, out_path: Path, *, force: bool = False) -> str:
    if out_path.exists() and out_path.stat().st_size > 1000 and not force:
        return "skip"

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    bands = load_band_points(lc_path)
    colors = {"g": "#2ca25f", "r": "#de2d26"}
    labels = {"g": "g band", "r": "r band"}
    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=120)
    any_points = False
    any_fit = False

    for band in ("g", "r"):
        x = bands[band]["x"]
        y = bands[band]["y"]
        err = bands[band]["err"]
        if x.size:
            any_points = True
            ax.errorbar(
                x,
                y,
                yerr=np.clip(err, 0.0, 0.5),
                fmt="o",
                ms=2.4,
                lw=0.45,
                alpha=0.42,
                color=colors[band],
                ecolor=colors[band],
                capsize=0,
                label=f"{labels[band]} obs",
                zorder=2,
            )
        fit = fit_band(x, y, err)
        if fit is not None:
            any_fit = True
            fine_x, fine_y, fine_std = fit
            ax.plot(fine_x, fine_y, color=colors[band], lw=1.7, alpha=0.96, label=f"{labels[band]} GP refit", zorder=4)
            ax.fill_between(
                fine_x,
                fine_y - fine_std,
                fine_y + fine_std,
                color=colors[band],
                alpha=0.12,
                linewidth=0,
                zorder=1,
            )

    if any_points:
        ax.invert_yaxis()
        ax.set_xlabel("MJD", fontsize=9)
        ax.set_ylabel("Magnitude", fontsize=9)
        ax.grid(True, alpha=0.16, linewidth=0.6)
        ax.tick_params(axis="both", labelsize=8)
        ax.legend(loc="best", fontsize=7, frameon=True, ncol=2)
    else:
        ax.text(0.5, 0.55, source_id, ha="center", va="center", fontsize=14, transform=ax.transAxes)
        ax.text(0.5, 0.43, "no usable photometry rows", ha="center", va="center", fontsize=10, color="#666", transform=ax.transAxes)
        ax.set_xticks([])
        ax.set_yticks([])

    subtitle = "GP refit view" if any_fit else "raw points only"
    ax.set_title(f"{source_id}  |  {subtitle}", fontsize=10, pad=8)
    fig.tight_layout(pad=0.7)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor="white", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    return "rendered" if any_fit else "points_only"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--lc-dir", type=Path, default=LC_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--sources-file", type=Path, help="Optional newline-separated source IDs to render.")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.sources_file:
        sources = [line.strip() for line in args.sources_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        sources = fallback_sources(args.manifest, args.out_dir)
    rendered = skipped = failed = points_only = 0
    failures: list[dict[str, Any]] = []
    status_by_source: dict[str, str] = {}
    for n, source_id in enumerate(sources, start=1):
        lc_path = args.lc_dir / f"{source_id}.csv"
        if not lc_path.exists():
            failed += 1
            failures.append({"source_id": source_id, "error": "missing_lightcurve_csv"})
            continue
        try:
            status = render_refit(source_id, lc_path, args.out_dir / f"{source_id}_sample_gp.png", force=args.force)
        except Exception as exc:
            failed += 1
            failures.append({"source_id": source_id, "error": repr(exc)})
            continue
        if status == "skip":
            skipped += 1
        elif status == "points_only":
            points_only += 1
        else:
            rendered += 1
        status_by_source[source_id] = status
        if n % 10 == 0 or n == len(sources):
            print(f"progress {n}/{len(sources)} rendered={rendered} skipped={skipped} points_only={points_only} failed={failed}", flush=True)

    (args.out_dir.parent / "gp-refit-status.json").write_text(
        json.dumps({"status_by_source": status_by_source, "failures": failures}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"sources": len(sources), "rendered": rendered, "skipped": skipped, "points_only": points_only, "failed": failed, "failures": failures[:20]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
