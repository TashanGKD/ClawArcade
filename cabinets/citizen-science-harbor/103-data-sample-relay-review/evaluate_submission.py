#!/usr/bin/env python3
"""Validate the five-line submission format for cabinet 103."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse


ROLES = {"interesting", "bridge", "data_issue", "typical", "control", "unsure"}
CONFIDENCE = {"high", "medium", "low"}
FOLLOWUP = {"yes", "no"}
EVIDENCE_TAGS = {
    "peak_or_bump",
    "tail_or_plateau",
    "rebrightening",
    "nonmonotonic",
    "color_separation",
    "large_amplitude",
    "rapid_rise",
    "rapid_decline",
    "slow_decline",
    "long_duration",
    "smooth_control",
    "sparse_sampling",
    "background_or_contamination",
    "single_band_signal",
    "band_missing",
    "baseline_offset",
    "outlier_only",
    "context_risk",
    "low_snr",
    "unclear",
}
QUALITY_FLAGS = {
    "good_sampling",
    "cadence_gap",
    "sparse_sampling",
    "low_snr",
    "heavy_imputation",
    "background_issue",
    "saturation_or_edge",
    "band_missing",
    "image_unreadable",
    "none",
}
VISUAL_HINTS = (
    "主峰",
    "窄峰",
    "长尾",
    "平台",
    "再亮",
    "回升",
    "颜色",
    "基线",
    "背景",
    "污染",
    "采样",
    "稀疏",
    "缺测",
    "衰减",
    "上升",
    "下降",
    "起伏",
    "振幅",
    "异常点",
    "光变点",
    "证据不足",
    "峰",
    "尾",
)
MARKDOWN_IMAGE = re.compile(r"^!\[[^\]]*\]\((?P<url>[^)]+)\)$")


def image_key(value: str) -> str:
    parsed = urlparse(value.strip())
    path = parsed.path if parsed.scheme or parsed.netloc else value.strip()
    name = Path(path).name
    if name.endswith("_sample_review.png"):
        return name[: -len("_sample_review.png")]
    if name.endswith("_sample_gp.png"):
        return name[: -len("_sample_gp.png")]
    if name.endswith("_sample_scatter.png"):
        return name[: -len("_sample_scatter.png")]
    if name.endswith(".png"):
        return name[:-4]
    return name


def load_manifest_keys() -> set[str]:
    manifest_path = Path(__file__).with_name("full-manifest.json")
    if not manifest_path.exists():
        return set()
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        image_key(str(item.get("image_url") or ""))
        for item in data.get("items", [])
        if item.get("image_url")
    }


def validate_line(line: str, expected_index: int, manifest_keys: set[str]) -> dict[str, object]:
    parts = [part.strip() for part in line.split("|")]
    errors: list[str] = []

    if len(parts) != 8:
        return {
            "line": expected_index,
            "ok": False,
            "errors": [f"expected 8 pipe-separated fields, got {len(parts)}"],
        }

    image_field, role, score, confidence, followup, evidence_text, quality_text, reason = parts
    evidence_tags = [tag.strip() for tag in evidence_text.split(",") if tag.strip()]
    quality_flags = [flag.strip() for flag in quality_text.split(",") if flag.strip()]
    image_match = MARKDOWN_IMAGE.fullmatch(image_field)
    image_url = image_match.group("url").strip() if image_match else ""
    key = image_key(image_url) if image_url else ""

    if not image_match:
        errors.append("first field must be a markdown image URL like ![](https://.../name.png)")
    elif "/all_sample_review/" not in image_url and "/all_sample_scatter/" not in image_url and "/all_sample_gp/" not in image_url:
        errors.append("image URL must point to the all_sample_review, all_sample_gp, or all_sample_scatter asset path")
    elif manifest_keys and key not in manifest_keys:
        errors.append(f"image URL is not present in full-manifest.json: {key}")
    if role not in ROLES:
        errors.append(f"role must be one of {sorted(ROLES)}")
    if not re.fullmatch(r"[0-5]", score):
        errors.append("anomaly_score must be an integer from 0 to 5")
    if confidence not in CONFIDENCE:
        errors.append(f"confidence must be one of {sorted(CONFIDENCE)}")
    if followup not in FOLLOWUP:
        errors.append("needs_followup must be yes or no")
    if not evidence_tags:
        errors.append("evidence_tags must contain at least one tag")
    invalid_evidence = [tag for tag in evidence_tags if tag not in EVIDENCE_TAGS]
    if invalid_evidence:
        errors.append(f"invalid evidence_tags: {invalid_evidence}")
    if not quality_flags:
        errors.append("quality_flags must contain at least one flag")
    invalid_quality = [flag for flag in quality_flags if flag not in QUALITY_FLAGS]
    if invalid_quality:
        errors.append(f"invalid quality_flags: {invalid_quality}")
    if not reason:
        errors.append("reason must not be empty")
    elif not any(hint in reason for hint in VISUAL_HINTS) and not evidence_tags:
        errors.append("reason should include checkable visual evidence")

    return {
        "line": expected_index,
        "ok": not errors,
        "image_url": image_url,
        "image_key": key,
        "role": role,
        "anomaly_score": int(score) if re.fullmatch(r"[0-5]", score) else None,
        "confidence": confidence,
        "needs_followup": followup,
        "evidence_tags": evidence_tags,
        "quality_flags": quality_flags,
        "reason": reason,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submission", required=True, help="Path to a five-line submission text file.")
    args = parser.parse_args()

    text = Path(args.submission).read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    manifest_keys = load_manifest_keys()

    results: list[dict[str, object]] = []
    errors: list[str] = []

    if len(lines) != 5:
        errors.append(f"submission must contain exactly 5 non-empty lines, got {len(lines)}")

    for idx, line in enumerate(lines[:5], start=1):
        result = validate_line(line, idx, manifest_keys)
        results.append(result)
        errors.extend(str(err) for err in result["errors"])

    ok = not errors
    print(
        json.dumps(
            {
                "ok": ok,
                "line_count": len(lines),
                "results": results,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print("SUCCESS" if ok else "ERROR")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
