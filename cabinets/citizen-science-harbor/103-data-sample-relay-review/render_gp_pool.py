#!/usr/bin/env python3
"""Render GP-fit light-curve images for the DATA_SAMPLE relay pool.

The relay originally used raw scatter plots as the public image URL. This
script keeps those scatter URLs as a fallback, renders GP-fit views when curve
payloads are available, and writes updated manifest files where ``image_url``
points at the GP view by default.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import pickle
from pathlib import Path
from typing import Any


DEFAULT_GP_ROOT = Path(r"C:\Unsupervised\DATA\DATA_Query\DATA_600_GP")
DEFAULT_EXTRA_CURVE_DIRS = [
    Path(r"C:\Unsupervised\DATA\DATA_Query\_archive\DATA_600_GP_curves_sample"),
    Path(r"C:\Unsupervised\DATA\DATA_Query\_archive\DATA_600_GP_curves_remaining"),
    Path(r"C:\Unsupervised\DATA\DATA_Query\_archive\DATA_600_GP_curves_old"),
    Path(r"C:\Unsupervised\DATA\DATA_Query\_archive\DATA_600_GP_curves_unclass"),
]
DEFAULT_ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = DEFAULT_ROOT / "full-manifest.json"
DEFAULT_OUT_DIR = DEFAULT_ROOT / "all_sample_gp"
DEFAULT_REMOTE_BASE = "http://49.233.162.81:8788/all_sample_gp"
BATCH_SIZE = 5


def finite_pairs(xs: list[Any], ys: list[Any]) -> tuple[list[float], list[float]]:
    out_x: list[float] = []
    out_y: list[float] = []
    for x, y in zip(xs or [], ys or []):
        try:
            xf = float(x)
            yf = float(y)
        except (TypeError, ValueError):
            continue
        if math.isfinite(xf) and math.isfinite(yf):
            out_x.append(xf)
            out_y.append(yf)
    return out_x, out_y


def render_one(source_id: str, payload: dict[str, Any], out_path: Path, *, force: bool = False) -> str:
    if out_path.exists() and out_path.stat().st_size > 1000 and not force:
        return "skip"

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {
        "g": "#2ca25f",
        "r": "#de2d26",
    }
    labels = {
        "g": "g band",
        "r": "r band",
    }

    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=120)
    rendered_any = False

    for band in ("g", "r"):
        band_payload = payload.get(band) or {}
        color = colors[band]
        obs_x, obs_y = finite_pairs(band_payload.get("mjd_obs") or [], band_payload.get("mag_obs") or [])
        obs_err = band_payload.get("magerr_obs") or []
        fine_x, fine_y = finite_pairs(band_payload.get("mjd_fine") or [], band_payload.get("mag_fine") or [])
        fine_err = band_payload.get("mag_err_fine") or []

        if obs_x and obs_y:
            rendered_any = True
            try:
                yerr = [float(v) if math.isfinite(float(v)) else 0.0 for v in obs_err[: len(obs_y)]]
            except (TypeError, ValueError):
                yerr = []
            ax.errorbar(
                obs_x,
                obs_y,
                yerr=yerr if len(yerr) == len(obs_y) else None,
                fmt="o",
                ms=2.6,
                lw=0.45,
                alpha=0.45,
                color=color,
                ecolor=color,
                capsize=0,
                label=f"{labels[band]} obs",
                zorder=2,
            )

        if fine_x and fine_y:
            rendered_any = True
            ax.plot(
                fine_x,
                fine_y,
                color=color,
                lw=1.7,
                alpha=0.96,
                label=f"{labels[band]} GP",
                zorder=4,
            )
            try:
                err_vals = [float(v) for v in fine_err[: len(fine_y)]]
            except (TypeError, ValueError):
                err_vals = []
            if len(err_vals) == len(fine_y):
                lower = [y - e for y, e in zip(fine_y, err_vals)]
                upper = [y + e for y, e in zip(fine_y, err_vals)]
                ax.fill_between(fine_x, lower, upper, color=color, alpha=0.12, linewidth=0, zorder=1)

    if not rendered_any:
        ax.text(0.5, 0.56, source_id, ha="center", va="center", fontsize=14, transform=ax.transAxes)
        ax.text(
            0.5,
            0.44,
            "no usable GP curve payload",
            ha="center",
            va="center",
            fontsize=10,
            color="#666666",
            transform=ax.transAxes,
        )
        ax.set_xticks([])
        ax.set_yticks([])
    else:
        ax.invert_yaxis()
        ax.set_xlabel("MJD", fontsize=9)
        ax.set_ylabel("Magnitude", fontsize=9)
        ax.grid(True, alpha=0.16, linewidth=0.6)
        ax.tick_params(axis="both", labelsize=8)
        ax.legend(loc="best", fontsize=7, frameon=True, ncol=2)

    ax.set_title(f"{source_id}  |  GP fit view", fontsize=10, pad=8)
    fig.tight_layout(pad=0.7)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor="white", bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    return "rendered" if rendered_any else "placeholder"


def iter_curve_batches(curve_dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for curves_dir in curve_dirs:
        if not curves_dir.exists():
            continue
        files.extend(sorted(curves_dir.glob("*.pkl.gz")))
    if not files:
        raise FileNotFoundError("no GP curve batches found")
    return list(dict.fromkeys(files))


def build_manifest(
    original: dict[str, Any],
    rendered_ids: set[str],
    *,
    remote_base: str,
    out_dir: Path,
) -> dict[str, Any]:
    existing_ids = {
        path.name[: -len("_sample_gp.png")]
        for path in out_dir.glob("*_sample_gp.png")
        if path.stat().st_size > 1000 and path.name.endswith("_sample_gp.png")
    }
    rendered_ids = rendered_ids | existing_ids
    refit_status_path = out_dir.parent / "gp-refit-status.json"
    refit_status: dict[str, str] = {}
    if refit_status_path.exists():
        try:
            refit_status = (json.loads(refit_status_path.read_text(encoding="utf-8")).get("status_by_source") or {})
        except Exception:
            refit_status = {}

    public_items: list[dict[str, Any]] = []
    for item in original.get("items") or []:
        source_id = str(item.get("source_id") or "")
        scatter_url = item.get("scatter_image_url") or item.get("image_url") or ""
        gp_file = f"{source_id}_sample_gp.png"
        gp_url = f"{remote_base.rstrip('/')}/{gp_file}"
        next_item = dict(item)
        next_item["scatter_image_url"] = scatter_url
        if source_id in rendered_ids:
            next_item["gp_image_url"] = gp_url
            next_item["image_url"] = gp_url
            if source_id in refit_status:
                next_item["image_mode"] = "gp_refit" if refit_status[source_id] == "rendered" else "raw_points_only"
            else:
                next_item["image_mode"] = "gp_fit"
        else:
            next_item["gp_image_url"] = ""
            next_item["image_url"] = scatter_url
            next_item["image_mode"] = "scatter_fallback"
        public_items.append(next_item)

    batches = []
    for batch_idx, start in enumerate(range(0, len(public_items), BATCH_SIZE), start=1):
        group = public_items[start : start + BATCH_SIZE]
        batch_items = []
        for local_idx, item in enumerate(group, start=1):
            batch_items.append({**item, "index": local_idx, "is_filler": False})
        batches.append({"batch": batch_idx, "items": batch_items})

    updated = dict(original)
    updated["image_default"] = "gp_fit"
    updated["image_fallback"] = "scatter_image_url"
    updated["items"] = public_items
    updated["pool_size"] = len(public_items)
    updated["batch_size"] = BATCH_SIZE
    updated["batch_count"] = len(batches)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--gp-root", type=Path, default=DEFAULT_GP_ROOT)
    parser.add_argument(
        "--include-archive-curves",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Also read historical GP curve archives to fill gaps in the current curve bundle.",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--remote-base", default=DEFAULT_REMOTE_BASE)
    parser.add_argument("--limit", type=int, default=0, help="Render at most N matching sources; 0 renders all.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--write-manifest", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    wanted = {str(item.get("source_id") or "") for item in manifest.get("items") or []}
    rendered_ids: set[str] = set()
    skipped = rendered = placeholder = absent = 0

    curve_dirs = [args.gp_root / "curves"]
    if args.include_archive_curves:
        curve_dirs.extend(DEFAULT_EXTRA_CURVE_DIRS)

    for batch_path in iter_curve_batches(curve_dirs):
        if args.limit and len(rendered_ids) >= args.limit:
            break
        with gzip.open(batch_path, "rb") as fh:
            curves = pickle.load(fh)
        for source_id, payload in curves.items():
            if source_id not in wanted or source_id in rendered_ids:
                continue
            if args.limit and len(rendered_ids) >= args.limit:
                break
            out_path = args.out_dir / f"{source_id}_sample_gp.png"
            status = render_one(source_id, payload, out_path, force=args.force)
            rendered_ids.add(source_id)
            if status == "skip":
                skipped += 1
            elif status == "placeholder":
                placeholder += 1
            else:
                rendered += 1
            if len(rendered_ids) % 500 == 0:
                print(
                    f"progress rendered_ids={len(rendered_ids)}/{len(wanted)} rendered={rendered} skipped={skipped} placeholder={placeholder}",
                    flush=True,
                )

    absent = len(wanted - rendered_ids)
    updated = build_manifest(manifest, rendered_ids, remote_base=args.remote_base, out_dir=args.out_dir)
    if args.write_manifest:
        args.manifest.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        batches = []
        for batch_idx, start in enumerate(range(0, len(updated["items"]), BATCH_SIZE), start=1):
            group = updated["items"][start : start + BATCH_SIZE]
            batches.append({"batch": batch_idx, "items": [{**item, "index": i} for i, item in enumerate(group, start=1)]})
        (args.manifest.parent / "full-batches.json").write_text(
            json.dumps(
                {
                    "task_id": updated.get("task_id", "103-data-sample-relay-review"),
                    "pool_size": len(updated["items"]),
                    "batch_size": BATCH_SIZE,
                    "batch_count": len(batches),
                    "batches": batches,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    print(
        json.dumps(
            {
                "wanted": len(wanted),
                "rendered_or_existing": len(rendered_ids),
                "newly_rendered": rendered,
                "skipped_existing": skipped,
                "placeholder": placeholder,
                "absent": absent,
                "out_dir": str(args.out_dir),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
