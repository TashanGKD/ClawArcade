#!/usr/bin/env python3
"""Stateful data server for the DATA_SAMPLE public-science relay.

It keeps image URLs and batch assignment available to TopicLab Arcade. Official
submissions and evaluator feedback live in TopicLab branches, not in this
service.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
except Exception:  # pragma: no cover - image composition is an optional display aid.
    Image = None
    ImageChops = None
    ImageDraw = None
    ImageFont = None


ROOT = Path(__file__).resolve().parent
STATE_PATH = Path(os.environ["RELAY_STATE_PATH"]) if os.environ.get("RELAY_STATE_PATH") else ROOT / "relay-state.json"
MANIFEST_PATH = ROOT / "full-manifest.json"
FEATURE_CARDS_PATH = ROOT / "feature-cards.json"
RESULTS_DIR = Path(os.environ["RELAY_RESULTS_DIR"]) if os.environ.get("RELAY_RESULTS_DIR") else ROOT / "results"
LOG_PATHS = [
    Path(os.environ["RELAY_ACCESS_LOG"]) if os.environ.get("RELAY_ACCESS_LOG") else None,
    ROOT / "server.err.log",
    ROOT.parent / "data-sample-http-8788.err.log",
]
DEFAULT_CLAIM_TTL_SECONDS = 6 * 60 * 60

ALLOWED_ROLES = {"interesting", "bridge", "data_issue", "typical", "control", "unsure"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_FOLLOWUP = {"yes", "no"}
ALLOWED_EVIDENCE_TAGS = {
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
ALLOWED_QUALITY_FLAGS = {
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
TAG_SUGGESTION_HINTS = {
    "bright": ["rapid_rise", "large_amplitude", "peak_or_bump"],
    "fast": ["rapid_rise", "rapid_decline"],
    "rise": ["rapid_rise"],
    "decline": ["rapid_decline", "slow_decline"],
    "fade": ["rapid_decline", "slow_decline"],
    "flat": ["smooth_control"],
    "stable": ["smooth_control"],
    "smooth": ["smooth_control"],
    "amp": ["large_amplitude"],
    "amplitude": ["large_amplitude"],
    "peak": ["peak_or_bump"],
    "bump": ["peak_or_bump"],
    "tail": ["tail_or_plateau"],
    "plateau": ["tail_or_plateau"],
    "rebright": ["rebrightening"],
    "color": ["color_separation"],
    "single": ["single_band_signal"],
    "band": ["single_band_signal", "band_missing"],
    "background": ["background_or_contamination"],
    "contamination": ["background_or_contamination"],
    "sample": ["sparse_sampling", "good_sampling"],
    "sampling": ["sparse_sampling", "good_sampling"],
    "quality": ["good_sampling", "image_unreadable"],
    "cadence": ["cadence_gap"],
    "snr": ["low_snr"],
    "noise": ["low_snr"],
    "impute": ["heavy_imputation"],
    "missing": ["band_missing"],
    "unreadable": ["image_unreadable"],
}
VISUAL_WORDS = {
    "峰",
    "主峰",
    "窄峰",
    "长尾",
    "拖尾",
    "平台",
    "再亮",
    "颜色",
    "色",
    "基线",
    "背景",
    "污染",
    "采样",
    "稀疏",
    "噪声",
    "信噪",
    "低信噪",
    "衰减",
    "上升",
    "下降",
    "起伏",
    "平稳",
    "散点",
    "波段",
    "同步",
    "振幅",
    "异常点",
    "断裂",
    "缺测",
    "光变",
    "scatter",
    "peak",
    "tail",
    "plateau",
    "baseline",
    "background",
    "contamination",
    "sampling",
    "decline",
    "rebrightening",
    "color",
}
EVIDENCE_AXES = {
    "peak_or_bump": ["峰", "主峰", "窄峰", "peak", "双峰"],
    "tail_or_plateau": ["长尾", "拖尾", "平台", "tail", "plateau"],
    "rebrightening": ["再亮", "回升", "先降后升", "变暗再变亮", "rebrightening"],
    "nonmonotonic": ["非单调", "起伏", "交错", "反向", "颜色反转", "先降后升", "波动", "nonmono"],
    "color_separation": ["颜色", "色", "分层", "交叉", "g/r", "g波段", "r波段", "i波段", "color"],
    "amplitude": ["振幅", "幅度", "等", "mag", "magnitude"],
    "sparse_sampling": ["采样", "稀疏", "缺测", "点少", "孤立点", "sampling"],
    "background_or_contamination": ["背景", "污染", "边界", "饱和", "contamination", "background"],
    "context_risk": ["AGN", "恒星", "宿主", "host", "gaia", "wise", "dlr"],
    "smooth_control": ["平稳", "稳定", "普通", "对照", "无明显", "分布均匀"],
}
EVIDENCE_TAG_TO_AXIS = {
    "large_amplitude": "amplitude",
    "rapid_rise": "nonmonotonic",
    "rapid_decline": "nonmonotonic",
    "slow_decline": "nonmonotonic",
    "long_duration": "tail_or_plateau",
    "low_snr": "sparse_sampling",
    "single_band_signal": "sparse_sampling",
    "band_missing": "sparse_sampling",
    "baseline_offset": "background_or_contamination",
    "outlier_only": "sparse_sampling",
    "unclear": "sparse_sampling",
    **{tag: tag for tag in ALLOWED_EVIDENCE_TAGS if tag not in {"large_amplitude", "rapid_rise", "rapid_decline", "slow_decline", "long_duration", "low_snr", "single_band_signal", "band_missing", "baseline_offset", "outlier_only", "unclear"}},
}
LEGACY_CLASS_PATTERN = re.compile(r"(?i)(?<![A-Za-z0-9])(CV|YSO|WD|SN)(?![A-Za-z0-9])")
NUMERIC_EVIDENCE_PATTERN = re.compile(
    r"(?i)(\d+(?:\.\d+)?\s*(?:mag|mjd|snr|等)|[gri]\s*波段|[gri]-[gri]|振幅|幅度|色差|间隔)"
)
PHYSICAL_MECHANISM_PATTERN = re.compile(
    r"(?i)(真实(瞬变|变化|结构|爆发)|短时标|长期变源|能量|冷却|衰减|爆发后|"
    r"伪影|离群|测光(问题|伪影)|单波段(伪影|离群)|宿主|AGN|背景|污染|采样空窗|"
    r"低信噪|信噪比|噪声|拟合震荡|信号微弱|趋势不清|不可靠|普通对照|"
    r"峰前采样|原始散点|图像质量|波段同步|环境相互作用|核区|校准|颜色演化|"
    r"周期性|调制|辐射|抛射|温度变化|反复爆发|脉冲|增亮|系统偏差|观测问题|物理过程)"
)
REASONING_CONNECTOR_PATTERN = re.compile(
    r"(?i)(更像|不像|不太像|而不是|可能|介于|难以区分|无法区分|缺少.*证据|"
    r"暗示|反映|符合|指向|说明|类似|因此|所以|建议复核|后续.*复核|先查|"
    r"需要复核|需复核|值得复核|可作.*对照|适合作为|建议回看|先回看|按.*标记)"
)
MECHANICAL_REASON_PATTERN = re.compile(
    r"(?i)(^\s*\d+\s*个观测点|^feature\s*显示|^再亮计数约?\d+|^SNR\s*极低|"
    r"^[gri]\s*波段|^r\s*(?:和|/)?\s*g\s*波段|^g\s*(?:和|/)?\s*r\s*波段|"
    r"^两波段.*振幅|^三波段.*振幅|^振幅(?:仅|约)|^MJD|"
    r"^.*波段.*(?:等|MJD).*振幅)"
)
GENERIC_EVIDENCE_TAGS = {"unclear", "smooth_control"}
QUALITY_RISK_FLAGS = {
    "cadence_gap",
    "sparse_sampling",
    "low_snr",
    "heavy_imputation",
    "background_issue",
    "saturation_or_edge",
    "band_missing",
    "image_unreadable",
}
EVIDENCE_TAG_ALIASES = {
    "band_difference": "color_separation",
    "flat_baseline": "smooth_control",
    "flat_light_curve": "smooth_control",
    "fading_tail": "tail_or_plateau",
    "small_amplitude": "smooth_control",
    "stable_photometry": "smooth_control",
    "regular_oscillation": "nonmonotonic",
    "periodic_variation": "nonmonotonic",
    "periodic_variability": "nonmonotonic",
    "rapid_variation": "nonmonotonic",
    "transient_outburst": "peak_or_bump",
    "monotonic_decline": "slow_decline",
    "smooth_decline": "slow_decline",
    "slow_trend": "slow_decline",
    "multi_peak": "peak_or_bump",
    "multiple_peak": "peak_or_bump",
    "sharp_peaks": "peak_or_bump",
    "single_peak_decay": "peak_or_bump",
    "weak_variability": "smooth_control",
    "poor_quality": "unclear",
    "moderate_sampling": "sparse_sampling",
    "good_sample": "smooth_control",
}
QUALITY_FLAG_ALIASES = {
    "poor_quality": "image_unreadable",
    "moderate_sampling": "good_sampling",
    "good_sample": "good_sampling",
    "dense_sampling": "good_sampling",
    "good_gp_fit": "good_sampling",
    "good_snr": "good_sampling",
    "high_cadence": "good_sampling",
    "multi_band": "good_sampling",
    "rare_gap": "cadence_gap",
    "stable_noise": "good_sampling",
    "variable_quality": "sparse_sampling",
    "clean": "good_sampling",
    "ok": "none",
    "no_issue": "none",
}
ROLE_ALIASES = {
    "nonmonotonic": "bridge",
    "peak_or_bump": "interesting",
    "tail_or_plateau": "interesting",
    "rebrightening": "interesting",
    "data_quality": "data_issue",
    "quality_issue": "data_issue",
}
FOLLOWUP_ROLES = {"interesting", "bridge", "data_issue"}
LOW_PRIORITY_ROLES = {"typical", "control"}
LINE_SCORE_MAX = 10
PUBLIC_STATIC_FILES = {
    "/all_sample_gp.tar",
    "/all_sample_scatter.tar",
}
PUBLIC_IMAGE_PREFIXES = (
    "/public/",
    "/all_sample_review/",
    "/all_sample_gp/",
    "/all_sample_scatter/",
)
PUBLIC_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".svg")


state_lock = threading.Lock()
manifest_cache: dict[str, Any] | None = None
manifest_key_cache: dict[str, dict[str, Any]] | None = None
feature_cards_cache: dict[str, dict[str, Any]] | None = None
focus_source_cache: set[str] | None = None
priority_source_cache: set[str] | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_manifest() -> dict[str, Any]:
    global manifest_cache
    if manifest_cache is None:
        manifest_cache = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return manifest_cache


def load_feature_cards() -> dict[str, dict[str, Any]]:
    global feature_cards_cache
    if feature_cards_cache is None:
        if not FEATURE_CARDS_PATH.exists():
            feature_cards_cache = {}
        else:
            payload = json.loads(FEATURE_CARDS_PATH.read_text(encoding="utf-8"))
            feature_cards_cache = payload.get("cards") or {}
    return feature_cards_cache


def source_id_from_filename(path: Path) -> str:
    name = path.name
    for suffix in ("_sample_review.png", "_sample_gp.png", "_sample_scatter.png", "_level_scatter.png", "_scatter.png"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def priority_sources() -> set[str]:
    global priority_source_cache
    if priority_source_cache is None:
        folder = ROOT / "sample_shortlist_scatter"
        priority_source_cache = {source_id_from_filename(path) for path in folder.glob("*.png")}
    return priority_source_cache


def focus_sources() -> set[str]:
    global focus_source_cache
    if focus_source_cache is None:
        folder = ROOT / "level_scatter"
        focus_source_cache = {source_id_from_filename(path) for path in folder.glob("*.png")}
    return focus_source_cache


def canonical_path(url_or_path: str) -> str:
    text = str(url_or_path or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    path = parsed.path if parsed.scheme or parsed.netloc else text
    if not path.startswith("/"):
        path = "/" + path.lstrip("./")
    return path


def image_key(url_or_path: str) -> str:
    path = canonical_path(url_or_path)
    if "/all_sample_review/" in path:
        return "/all_sample_review/" + path.split("/all_sample_review/", 1)[1]
    if "/all_sample_gp/" in path:
        return "/all_sample_gp/" + path.split("/all_sample_gp/", 1)[1]
    if "/all_sample_scatter/" in path:
        return "/all_sample_scatter/" + path.split("/all_sample_scatter/", 1)[1]
    return path


def batch_number_for_index(global_index: int, batch_size: int) -> int:
    return ((global_index - 1) // batch_size) + 1


def batch_items(batch_number: int) -> list[dict[str, Any]]:
    manifest = load_manifest()
    batch_size = int(manifest.get("batch_size") or 5)
    items = manifest.get("items") or []
    start = (batch_number - 1) * batch_size
    return [normalize_item(item) for item in items[start : start + batch_size]]


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    source_id = str(normalized.get("source_id") or "").strip()
    if source_id:
        normalized["gp_image_url"] = normalized.get("gp_image_url") or normalized.get("image_url")
        normalized["scatter_image_url"] = normalized.get("scatter_image_url") or f"/all_sample_scatter/{source_id}_sample_scatter.png"
        normalized["review_image_url"] = f"/all_sample_review/{source_id}_sample_review.png"
        normalized["image_url"] = normalized["review_image_url"]
        normalized["image_mode"] = "scatter_with_feature_card"
    normalized["image_key"] = str(source_id or image_key(normalized.get("image_url", "")))
    normalized["batch_number"] = batch_number_for_index(
        int(normalized.get("global_index") or 1),
        int(load_manifest().get("batch_size") or 5),
    )
    card = load_feature_cards().get(str(normalized.get("source_id") or ""), {})
    if card.get("feature_text"):
        normalized["feature_text"] = card["feature_text"]
    return normalized


def manifest_items_by_key() -> dict[str, dict[str, Any]]:
    global manifest_key_cache
    if manifest_key_cache is not None:
        return manifest_key_cache
    by_key: dict[str, dict[str, Any]] = {}
    for item in load_manifest().get("items") or []:
        normalized = normalize_item(item)
        if normalized.get("source_id"):
            by_key[str(normalized["source_id"])] = normalized
        by_key[str(normalized.get("image_key") or "")] = normalized
        for field in ("image_url", "gp_image_url", "scatter_image_url"):
            key = image_key(item.get(field, ""))
            if key:
                by_key[key] = normalized
        if normalized.get("review_image_url"):
            by_key[image_key(str(normalized["review_image_url"]))] = normalized
    manifest_key_cache = by_key
    return by_key


def enrich_submission_for_public(submission: dict[str, Any]) -> dict[str, Any]:
    by_key = manifest_items_by_key()
    enriched = {
        "submission_id": submission.get("submission_id"),
        "participant_id": submission.get("participant_id"),
        "batch_number": submission.get("batch_number"),
        "created_at": submission.get("created_at"),
        "line_count": submission.get("line_count"),
        "valid_count": submission.get("valid_count"),
        "first_coverage_count": submission.get("first_coverage_count"),
    }
    rows: list[dict[str, Any]] = []
    for row in submission.get("rows", []):
        next_row = {
            "line": row.get("line"),
            "valid": row.get("valid"),
            "role": row.get("role"),
            "anomaly_score": row.get("anomaly_score"),
            "confidence": row.get("confidence"),
            "needs_followup": row.get("needs_followup"),
            "evidence_tags": row.get("evidence_tags") or [],
            "quality_flags": row.get("quality_flags") or [],
            "reason": row.get("reason"),
            "source_id": row.get("source_id"),
            "global_index": row.get("global_index"),
            "image_key": row.get("image_key"),
            "image_url": row.get("image_url"),
            "gp_image_url": row.get("gp_image_url"),
            "scatter_image_url": row.get("scatter_image_url"),
            "image_mode": row.get("image_mode"),
            "feature_text": row.get("feature_text"),
            "anomaly_decision": row.get("anomaly_decision") or anomaly_decision(row),
            "errors": row.get("errors") or [],
            "warnings": row.get("warnings") or [],
        }
        lookup_keys = [
            str(next_row.get("image_key") or ""),
            str(next_row.get("source_id") or ""),
            image_key(next_row.get("image_url", "")),
            image_key(next_row.get("gp_image_url", "")),
        ]
        item = next((by_key[key] for key in lookup_keys if key and key in by_key), None)
        if item:
            for field in ("source_id", "global_index", "image_key", "image_url", "gp_image_url", "scatter_image_url", "image_mode", "feature_text"):
                value = item.get(field)
                if value and not next_row.get(field):
                    next_row[field] = value
        rows.append(next_row)
    enriched["rows"] = rows
    return enriched


def is_public_submission(submission: dict[str, Any]) -> bool:
    return bool((submission.get("line_count") or 0) > 0 or submission.get("rows"))


def canonical_coverage_key(key: str, entry: dict[str, Any] | None = None) -> str:
    if entry and entry.get("source_id"):
        return str(entry["source_id"])
    by_key = manifest_items_by_key()
    item = by_key.get(key)
    if item and item.get("source_id"):
        return str(item["source_id"])
    name = Path(canonical_path(key)).name
    for suffix in ("_sample_review.png", "_sample_gp.png", "_sample_scatter.png", "_level_scatter.png", "_scatter.png"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return key


def parse_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def organizer_context_for_source(source_id: str) -> dict[str, Any]:
    card = load_feature_cards().get(source_id, {})
    is_focus = source_id in focus_sources()
    is_priority = source_id in priority_sources()
    return {
        "source_id": source_id,
        "focus_rendered": is_focus,
        "priority_candidate": is_priority,
        "review_tier": "focus" if is_focus else ("priority" if is_priority else "pool"),
        "context_family": card.get("context_family"),
        "quality_tier": card.get("quality_tier"),
        "quality_score": parse_float(card.get("quality_score")),
        "feature_completeness": parse_float(card.get("feature_completeness")),
        "n_obs": parse_int(card.get("n_obs")),
        "z": parse_float(card.get("z")),
        "host_dlr": parse_float(card.get("host_dlr")),
        "gaia_is_stellar": bool(card.get("gaia_is_stellar")),
        "var_evidence": bool(card.get("var_evidence")),
        "agn_evidence": bool(card.get("agn_evidence")),
        "wise_agn": bool(card.get("wise_agn")),
        "M_completed": parse_float(card.get("M_completed")),
        "dm15_completed": parse_float(card.get("dm15_completed")),
        "color_completed": parse_float(card.get("color_completed")),
        "amplitude_completed": parse_float(card.get("amplitude_completed")),
        "rebrightening_completed": parse_int(card.get("rebrightening_completed")),
        "nonmono_completed": parse_float(card.get("nonmono_completed")),
        "peak_snr": parse_float(card.get("peak_snr")),
    }


def participant_followup_signal(row: dict[str, Any]) -> bool:
    if not row.get("valid"):
        return False
    score = parse_int(row.get("anomaly_score")) or 0
    return bool(
        row.get("needs_followup") == "yes"
        or row.get("role") in {"interesting", "bridge", "data_issue"}
        or score >= 3
    )


def anomaly_decision(row: dict[str, Any]) -> str:
    if not row.get("valid"):
        return "invalid"
    role = str(row.get("role") or "")
    followup = str(row.get("needs_followup") or "")
    try:
        score = int(row.get("anomaly_score") or 0)
    except (TypeError, ValueError):
        score = 0
    if role == "data_issue":
        return "data_issue"
    if followup == "yes" or role in {"interesting", "bridge"} or score >= 3:
        return "followup_candidate"
    if role in {"typical", "control"} and score <= 2:
        return "ordinary_or_control"
    return "uncertain"


def extract_evidence_axes(reason: str) -> list[str]:
    lower = str(reason or "").lower()
    axes = []
    for axis, words in EVIDENCE_AXES.items():
        if any(word.lower() in lower for word in words):
            axes.append(axis)
    return axes


def parse_tag_list(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def unique_values(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        if value and value not in output:
            output.append(value)
    return output


def suggest_allowed_values(values: list[str], allowed: set[str], limit: int = 3) -> list[str]:
    suggestions: list[str] = []
    for value in values:
        token = str(value or "").lower()
        for hint, candidates in TAG_SUGGESTION_HINTS.items():
            if hint in token:
                suggestions.extend(candidate for candidate in candidates if candidate in allowed)
    joined = " ".join(str(value or "") for value in values).replace("_", " ").replace("-", " ")
    suggestions.extend(difflib.get_close_matches(joined, sorted(allowed), n=limit, cutoff=0.35))
    return unique_values(suggestions)[:limit]


def warning_with_suggestions(prefix: str, invalid_values: list[str], allowed: set[str]) -> str:
    base = f"{prefix}: {','.join(invalid_values)}"
    suggestions = suggest_allowed_values(invalid_values, allowed)
    if suggestions:
        return f"{base}；可参考 {', '.join(suggestions)}"
    return base


def normalize_tag_aliases(values: list[str], aliases: dict[str, str], allowed: set[str]) -> tuple[list[str], bool]:
    changed = False
    normalized: list[str] = []
    for value in values:
        tag = str(value or "").strip()
        mapped = aliases.get(tag, tag)
        if mapped != tag:
            changed = True
        if mapped in allowed:
            normalized.append(mapped)
        else:
            normalized.append(tag)
    return unique_values(normalized), changed


def append_backfill_field(row: dict[str, Any], field: str) -> None:
    fields = list(row.get("backfilled_fields") or [])
    if field not in fields:
        fields.append(field)
    row["backfilled_fields"] = fields
    row.setdefault("backfill_source", "rule_based_from_reason_and_features")


def infer_evidence_tags(row: dict[str, Any], context: dict[str, Any] | None = None) -> list[str]:
    context = context or {}
    reason = str(row.get("reason") or "").lower()
    role = str(row.get("role") or "")
    try:
        score = int(row.get("anomaly_score") or 0)
    except (TypeError, ValueError):
        score = 0
    tags: list[str] = []

    def add(tag: str) -> None:
        if tag in ALLOWED_EVIDENCE_TAGS and tag not in tags:
            tags.append(tag)

    keyword_map = [
        ("peak_or_bump", ["峰", "主峰", "窄峰", "凸起", "peak", "bump"]),
        ("tail_or_plateau", ["长尾", "拖尾", "平台", "tail", "plateau"]),
        ("rebrightening", ["再亮", "回升", "反弹", "变暗再变亮", "先降后升", "rebrightening"]),
        ("nonmonotonic", ["非单调", "起伏", "交错", "反向", "波动", "先降后升", "颜色反转"]),
        ("color_separation", ["颜色", "色差", "分层", "交叉", "g/r", "g波段", "r波段", "i波段", "三波段", "双波段"]),
        ("large_amplitude", ["振幅", "幅度", "大振幅", "large amplitude"]),
        ("rapid_rise", ["快速上升", "急剧增亮", "上升快"]),
        ("rapid_decline", ["快速衰减", "快速下降", "骤降", "急剧变暗"]),
        ("slow_decline", ["缓慢衰减", "缓慢下降", "逐渐下降"]),
        ("long_duration", ["长时间", "持续", "全程", "宽峰"]),
        ("smooth_control", ["平稳", "稳定", "普通", "对照", "无明显", "分布均匀"]),
        ("sparse_sampling", ["采样", "稀疏", "缺测", "点少", "孤立点"]),
        ("background_or_contamination", ["背景", "污染", "边界", "饱和", "contamination", "background"]),
        ("single_band_signal", ["单波段", "仅g", "仅r", "仅i"]),
        ("band_missing", ["缺波段", "波段缺失", "仅左侧", "覆盖不足"]),
        ("baseline_offset", ["基线", "零点", "漂移"]),
        ("outlier_only", ["异常点", "孤立点"]),
        ("context_risk", ["agn", "恒星", "宿主", "host", "gaia", "wise", "dlr"]),
        ("low_snr", ["snr低", "信噪比低", "低信噪比"]),
        ("unclear", ["不确定", "难以判断", "证据不足", "不可读"]),
    ]
    for tag, words in keyword_map:
        if any(word.lower() in reason for word in words):
            add(tag)

    if (context.get("rebrightening_completed") or 0) >= 2:
        add("rebrightening")
    if (context.get("nonmono_completed") or 0) >= 0.5:
        add("nonmonotonic")
    if (context.get("amplitude_completed") or 0) >= 0.5:
        add("large_amplitude")
    if (context.get("peak_snr") is not None and context.get("peak_snr") < 2) or (
        context.get("quality_score") is not None and context.get("quality_score") < 0.5
    ):
        add("low_snr")
    if context.get("var_evidence") or context.get("agn_evidence") or context.get("wise_agn") or context.get("gaia_is_stellar"):
        add("context_risk")

    if role == "data_issue" and not (set(tags) & {"background_or_contamination", "sparse_sampling", "low_snr", "band_missing", "unclear"}):
        add("unclear")
    if role in LOW_PRIORITY_ROLES and score <= 2 and not tags:
        add("smooth_control")
    if role in FOLLOWUP_ROLES and score >= 3 and not tags:
        add("nonmonotonic")
    if not tags:
        add("unclear")
    return tags[:4]


def infer_quality_flags(row: dict[str, Any], context: dict[str, Any] | None = None) -> list[str]:
    context = context or {}
    reason = str(row.get("reason") or "").lower()
    tags = set(row.get("evidence_tags") or [])
    flags: list[str] = []

    def add(flag: str) -> None:
        if flag in ALLOWED_QUALITY_FLAGS and flag not in flags:
            flags.append(flag)

    if any(word in reason for word in ["空窗", "缺测", "间隙", "gap"]):
        add("cadence_gap")
    if any(word in reason for word in ["采样稀疏", "点少", "孤立", "稀疏"]):
        add("sparse_sampling")
    if any(word in reason for word in ["snr低", "信噪比低", "低信噪比"]):
        add("low_snr")
    if any(word in reason for word in ["插值", "imputation"]):
        add("heavy_imputation")
    if any(word in reason for word in ["背景", "污染", "宿主", "边界"]):
        add("background_issue")
    if any(word in reason for word in ["饱和", "贴边", "裁切"]):
        add("saturation_or_edge")
    if any(word in reason for word in ["缺波段", "单波段", "仅g", "仅r", "仅i"]):
        add("band_missing")
    if any(word in reason for word in ["不可读", "乱码", "无法读取"]):
        add("image_unreadable")

    if "sparse_sampling" in tags or (context.get("n_obs") is not None and context.get("n_obs") < 40):
        add("sparse_sampling")
    if "low_snr" in tags or (context.get("peak_snr") is not None and context.get("peak_snr") < 2):
        add("low_snr")
    if {"background_or_contamination", "context_risk"} & tags:
        add("background_issue")
    if {"single_band_signal", "band_missing"} & tags:
        add("band_missing")

    if not flags:
        if row.get("role") in LOW_PRIORITY_ROLES or row.get("confidence") == "high":
            add("good_sampling")
        else:
            add("none")
    return flags[:3]


def backfill_row_annotations(row: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    evidence_tags, evidence_changed = normalize_tag_aliases(
        list(row.get("evidence_tags") or []),
        EVIDENCE_TAG_ALIASES,
        ALLOWED_EVIDENCE_TAGS,
    )
    quality_flags, quality_changed = normalize_tag_aliases(
        list(row.get("quality_flags") or []),
        QUALITY_FLAG_ALIASES,
        ALLOWED_QUALITY_FLAGS,
    )
    if evidence_changed:
        row["evidence_tags"] = evidence_tags
        append_backfill_field(row, "evidence_tags_alias_normalized")
    if quality_changed:
        row["quality_flags"] = quality_flags
        append_backfill_field(row, "quality_flags_alias_normalized")

    if not row.get("evidence_tags"):
        row["evidence_tags"] = infer_evidence_tags(row, context)
        append_backfill_field(row, "evidence_tags")
    if not row.get("quality_flags"):
        row["quality_flags"] = infer_quality_flags(row, context)
        append_backfill_field(row, "quality_flags")
    row["anomaly_decision"] = anomaly_decision(row)
    return row


def evidence_axes_from_row(row: dict[str, Any]) -> list[str]:
    tags = row.get("evidence_tags") or []
    axes = []
    for tag in tags:
        axis = EVIDENCE_TAG_TO_AXIS.get(str(tag))
        if axis and axis not in axes:
            axes.append(axis)
    if axes:
        return axes
    return extract_evidence_axes(str(row.get("reason") or ""))


def expected_axes_from_context(context: dict[str, Any]) -> list[str]:
    axes = []
    if (context.get("rebrightening_completed") or 0) >= 2:
        axes.append("rebrightening")
    if (context.get("nonmono_completed") or 0) >= 0.5:
        axes.append("nonmonotonic")
    if (context.get("amplitude_completed") or 0) >= 0.5:
        axes.append("amplitude")
    if (
        (context.get("n_obs") is not None and context.get("n_obs") < 40)
        or (context.get("quality_score") is not None and context.get("quality_score") < 0.5)
        or (context.get("peak_snr") is not None and context.get("peak_snr") < 1.0)
    ):
        axes.append("sparse_sampling")
    if context.get("var_evidence") or context.get("agn_evidence") or context.get("wise_agn") or context.get("gaia_is_stellar"):
        axes.append("context_risk")
    return axes


def axis_alignment(row: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    evidence_axes = evidence_axes_from_row(row)
    expected_axes = expected_axes_from_context(context)
    matched = [axis for axis in evidence_axes if axis in expected_axes]
    missing = [axis for axis in expected_axes if axis not in evidence_axes]
    extra = [axis for axis in evidence_axes if axis not in expected_axes]
    return {
        "evidence_axes": evidence_axes,
        "expected_axes": expected_axes,
        "matched_axes": matched,
        "missing_expected_axes": missing,
        "extra_axes": extra,
        "axis_match_count": len(matched),
    }


def crosscheck_bucket(row: dict[str, Any], context: dict[str, Any]) -> str:
    if not row.get("valid"):
        return "invalid"
    participant_flag = participant_followup_signal(row)
    system_flag = bool(context.get("focus_rendered") or context.get("priority_candidate"))
    if participant_flag and system_flag:
        return "hit_existing_candidate"
    if participant_flag and not system_flag:
        return "new_public_candidate"
    if not participant_flag and system_flag:
        return "missed_existing_candidate"
    return "control_agreement"


def crosscheck_row(submission: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    source_id = str(row.get("source_id") or "")
    context = organizer_context_for_source(source_id)
    alignment = axis_alignment(row, context)
    return {
        "submission_id": submission.get("submission_id"),
        "participant_id": submission.get("participant_id"),
        "batch_number": submission.get("batch_number"),
        "created_at": submission.get("created_at"),
        "review_key": {
            "source_id": source_id,
            "global_index": row.get("global_index"),
            "image_key": row.get("image_key"),
            "image_url": row.get("image_url"),
        },
        "participant_signal": {
            "valid": bool(row.get("valid")),
            "role": row.get("role"),
            "anomaly_score": row.get("anomaly_score"),
            "confidence": row.get("confidence"),
            "needs_followup": row.get("needs_followup"),
            "evidence_tags": row.get("evidence_tags") or [],
            "quality_flags": row.get("quality_flags") or [],
            "backfilled_fields": row.get("backfilled_fields") or [],
            "backfill_source": row.get("backfill_source"),
            "reason": row.get("reason"),
            "evidence_axes": alignment["evidence_axes"],
            "errors": row.get("errors") or [],
            "public_points": row.get("public_points"),
        },
        "organizer_context": context,
        "crosscheck": {
            "bucket": crosscheck_bucket(row, context),
            "participant_followup": participant_followup_signal(row),
            "system_priority": bool(context.get("focus_rendered") or context.get("priority_candidate")),
            "axis_alignment": alignment,
        },
    }


def organizer_review_payload(state: dict[str, Any]) -> dict[str, Any]:
    rows = [
        crosscheck_row(submission, row)
        for submission in state.get("submissions", [])
        for row in submission.get("rows", [])
    ]
    bucket_counts: dict[str, int] = {}
    for row in rows:
        bucket = row["crosscheck"]["bucket"]
        bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
    ranked = sorted(
        rows,
        key=lambda item: (
            int(item["participant_signal"].get("valid") or 0),
            parse_int(item["participant_signal"].get("anomaly_score")) or 0,
            int(item["crosscheck"].get("system_priority") or 0),
            int(item["crosscheck"].get("participant_followup") or 0),
        ),
        reverse=True,
    )
    by_source: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_id = row["review_key"]["source_id"]
        summary = by_source.setdefault(
            source_id,
            {
                "source_id": source_id,
                "image_url": row["review_key"].get("image_url"),
                "global_index": row["review_key"].get("global_index"),
                "organizer_context": row["organizer_context"],
                "review_count": 0,
                "valid_count": 0,
                "participant_followup_count": 0,
                "max_anomaly_score": 0,
                "mean_anomaly_score": 0.0,
                "roles": {},
                "buckets": {},
                "evidence_axes": {},
                "axis_match_count": 0,
                "latest_reason": "",
            },
        )
        signal = row["participant_signal"]
        summary["review_count"] += 1
        if signal.get("valid"):
            summary["valid_count"] += 1
            score = parse_int(signal.get("anomaly_score")) or 0
            summary["max_anomaly_score"] = max(summary["max_anomaly_score"], score)
            summary["mean_anomaly_score"] += score
            role = str(signal.get("role") or "")
            summary["roles"][role] = summary["roles"].get(role, 0) + 1
            if row["crosscheck"]["participant_followup"]:
                summary["participant_followup_count"] += 1
            summary["latest_reason"] = str(signal.get("reason") or "")
            for axis in signal.get("evidence_axes") or []:
                summary["evidence_axes"][axis] = summary["evidence_axes"].get(axis, 0) + 1
            summary["axis_match_count"] += int(
                row["crosscheck"].get("axis_alignment", {}).get("axis_match_count") or 0
            )
        bucket = row["crosscheck"]["bucket"]
        summary["buckets"][bucket] = summary["buckets"].get(bucket, 0) + 1
    source_summary = []
    for summary in by_source.values():
        if summary["valid_count"]:
            summary["mean_anomaly_score"] = round(summary["mean_anomaly_score"] / summary["valid_count"], 2)
        source_summary.append(summary)
    source_summary.sort(
        key=lambda item: (
            int(item["participant_followup_count"]),
            int(item["max_anomaly_score"]),
            int(item["axis_match_count"]),
            int(item["organizer_context"].get("focus_rendered") or 0),
            int(item["organizer_context"].get("priority_candidate") or 0),
            int(item.get("valid_count") or 0),
        ),
        reverse=True,
    )
    return {
        "ok": True,
        "generated_at": now_iso(),
        "summary": {
            "row_count": len(rows),
            "valid_count": sum(1 for row in rows if row["participant_signal"]["valid"]),
            "bucket_counts": bucket_counts,
            "priority_source_count": len(priority_sources()),
            "focus_source_count": len(focus_sources()),
        },
        "items": ranked,
        "source_summary": source_summary,
    }


def organizer_review_csv(payload: dict[str, Any]) -> str:
    output = io.StringIO()
    fields = [
        "source_id",
        "global_index",
        "image_url",
        "review_count",
        "valid_count",
        "participant_followup_count",
        "max_anomaly_score",
        "mean_anomaly_score",
        "roles",
        "buckets",
        "evidence_axes",
        "axis_match_count",
        "review_tier",
        "priority_candidate",
        "focus_rendered",
        "quality_tier",
        "quality_score",
        "n_obs",
        "amplitude_completed",
        "rebrightening_completed",
        "nonmono_completed",
        "peak_snr",
        "latest_reason",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for item in payload.get("source_summary") or []:
        context = item.get("organizer_context") or {}
        writer.writerow(
            {
                "source_id": item.get("source_id"),
                "global_index": item.get("global_index"),
                "image_url": item.get("image_url"),
                "review_count": item.get("review_count"),
                "valid_count": item.get("valid_count"),
                "participant_followup_count": item.get("participant_followup_count"),
                "max_anomaly_score": item.get("max_anomaly_score"),
                "mean_anomaly_score": item.get("mean_anomaly_score"),
                "roles": json.dumps(item.get("roles") or {}, ensure_ascii=False),
                "buckets": json.dumps(item.get("buckets") or {}, ensure_ascii=False),
                "evidence_axes": json.dumps(item.get("evidence_axes") or {}, ensure_ascii=False),
                "axis_match_count": item.get("axis_match_count"),
                "review_tier": context.get("review_tier"),
                "priority_candidate": context.get("priority_candidate"),
                "focus_rendered": context.get("focus_rendered"),
                "quality_tier": context.get("quality_tier"),
                "quality_score": context.get("quality_score"),
                "n_obs": context.get("n_obs"),
                "amplitude_completed": context.get("amplitude_completed"),
                "rebrightening_completed": context.get("rebrightening_completed"),
                "nonmono_completed": context.get("nonmono_completed"),
                "peak_snr": context.get("peak_snr"),
                "latest_reason": item.get("latest_reason"),
            }
        )
    return output.getvalue()


def submission_rows_for_export(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for submission in state.get("submissions", []):
        for row in submission.get("rows", []):
            source_id = str(row.get("source_id") or "")
            context = organizer_context_for_source(source_id)
            rows.append(
                {
                    "submission_id": submission.get("submission_id"),
                    "participant_id": submission.get("participant_id"),
                    "claim_id": submission.get("claim_id"),
                    "batch_number": submission.get("batch_number"),
                    "created_at": submission.get("created_at"),
                    "line": row.get("line"),
                    "valid": bool(row.get("valid")),
                    "source_id": source_id,
                    "global_index": row.get("global_index"),
                    "image_key": row.get("image_key"),
                    "image_url": row.get("image_url"),
                    "gp_image_url": row.get("gp_image_url"),
                    "scatter_image_url": row.get("scatter_image_url"),
                    "image_mode": row.get("image_mode"),
                    "anomaly_decision": row.get("anomaly_decision") or anomaly_decision(row),
                    "role": row.get("role"),
                    "anomaly_score": row.get("anomaly_score"),
                    "confidence": row.get("confidence"),
                    "needs_followup": row.get("needs_followup"),
                    "evidence_tags": ",".join(row.get("evidence_tags") or []),
                    "quality_flags": ",".join(row.get("quality_flags") or []),
                    "backfilled_fields": ",".join(row.get("backfilled_fields") or []),
                    "backfill_source": row.get("backfill_source"),
                    "reason": row.get("reason"),
                    "errors": ";".join(row.get("errors") or []),
                    "public_points": row.get("public_points"),
                    "review_status": "pending_weekly_review",
                    "weekly_review_points": "",
                    "review_tier": context.get("review_tier"),
                    "priority_candidate": context.get("priority_candidate"),
                    "focus_rendered": context.get("focus_rendered"),
                    "quality_tier": context.get("quality_tier"),
                    "quality_score": context.get("quality_score"),
                    "n_obs": context.get("n_obs"),
                    "amplitude_completed": context.get("amplitude_completed"),
                    "rebrightening_completed": context.get("rebrightening_completed"),
                    "nonmono_completed": context.get("nonmono_completed"),
                    "peak_snr": context.get("peak_snr"),
                }
            )
    return rows


def write_results_exports(state: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rows = submission_rows_for_export(state)
    fields = [
        "submission_id",
        "participant_id",
        "claim_id",
        "batch_number",
        "created_at",
        "line",
        "valid",
        "source_id",
        "global_index",
        "image_key",
        "image_url",
        "gp_image_url",
        "scatter_image_url",
        "image_mode",
        "anomaly_decision",
        "role",
        "anomaly_score",
        "confidence",
        "needs_followup",
        "evidence_tags",
        "quality_flags",
        "backfilled_fields",
        "backfill_source",
        "reason",
        "errors",
        "public_points",
        "review_status",
        "weekly_review_points",
        "review_tier",
        "priority_candidate",
        "focus_rendered",
        "quality_tier",
        "quality_score",
        "n_obs",
        "amplitude_completed",
        "rebrightening_completed",
        "nonmono_completed",
        "peak_snr",
    ]
    with (RESULTS_DIR / "submission_rows.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with (RESULTS_DIR / "submission_rows.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (RESULTS_DIR / "leaderboard.json").write_text(
        json.dumps(public_leaderboard_payload(state), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    review_payload = organizer_review_payload(state)
    (RESULTS_DIR / "organizer_review.json").write_text(
        json.dumps(review_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (RESULTS_DIR / "organizer_review.csv").write_text(
        organizer_review_csv(review_payload),
        encoding="utf-8-sig",
    )


def read_json(path: Path, fallback: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(fallback)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        backup = path.with_suffix(path.suffix + f".broken-{int(time.time())}")
        try:
            path.replace(backup)
        except OSError:
            pass
        return dict(fallback)


def empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "claims": {},
        "covered": {},
        "submissions": [],
    }


def load_state() -> dict[str, Any]:
    state = read_json(STATE_PATH, empty_state())
    state.setdefault("claims", {})
    state.setdefault("covered", {})
    state.setdefault("submissions", [])
    if state.get("covered"):
        migrated: dict[str, Any] = {}
        changed = False
        for key, entry in state["covered"].items():
            canonical = canonical_coverage_key(str(key), entry if isinstance(entry, dict) else None)
            migrated[canonical] = entry
            changed = changed or canonical != key
        if changed:
            state["covered"] = migrated
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def parse_access_seen() -> set[str]:
    seen: set[str] = set()
    pattern = re.compile(r'"GET\s+(\S+)\s+HTTP/')
    by_key = manifest_items_by_key()
    for log_path in LOG_PATHS:
        if not log_path or not log_path.exists():
            continue
        try:
            with log_path.open("rb") as fh:
                try:
                    fh.seek(max(0, log_path.stat().st_size - 2_000_000))
                except OSError:
                    pass
                text = fh.read().decode("utf-8", errors="ignore")
            for line in text.splitlines():
                match = pattern.search(line)
                if not match:
                    continue
                path = match.group(1)
                if path.startswith("/all_sample_review/") or path.startswith("/all_sample_scatter/") or path.startswith("/all_sample_gp/"):
                    key = image_key(path)
                    item = by_key.get(key)
                    seen.add(str(item.get("image_key")) if item else key)
        except OSError:
            continue
    return seen


def active_claimed_keys(state: dict[str, Any], ttl_seconds: int = DEFAULT_CLAIM_TTL_SECONDS) -> set[str]:
    active: set[str] = set()
    cutoff = time.time() - ttl_seconds
    for claim in state.get("claims", {}).values():
        if claim.get("status") == "submitted":
            continue
        claimed_ts = float(claim.get("claimed_ts") or 0)
        if claimed_ts < cutoff:
            continue
        active.update(claim.get("image_keys") or [])
    return active


def historically_claimed_keys(state: dict[str, Any]) -> set[str]:
    """Images handed out at least once.

    TopicLab Arcade is the official submission surface, so this data service
    may not see a matching `/api/submit` call. Treat claims themselves as
    soft coverage for future assignment, then fall back only when the pool is
    exhausted or fragmented.
    """
    claimed: set[str] = set()
    for claim in state.get("claims", {}).values():
        claimed.update(claim.get("image_keys") or [])
    return claimed


def status_payload(state: dict[str, Any]) -> dict[str, Any]:
    manifest = load_manifest()
    pool_size = int(manifest.get("pool_size") or len(manifest.get("items") or []))
    batch_count = int(manifest.get("batch_count") or 0)
    seen_from_log = parse_access_seen()
    covered = set(state.get("covered", {}).keys())
    active = active_claimed_keys(state)
    assigned = historically_claimed_keys(state)
    effective_seen = covered | assigned
    next_batch, next_items, next_mode = select_claim_items(state, skip_seen_logs=False)
    return {
        "ok": True,
        "task_id": manifest.get("task_id", "103-data-sample-relay-review"),
        "pool_size": pool_size,
        "batch_size": int(manifest.get("batch_size") or 5),
        "batch_count": batch_count,
        "covered_count": len(covered),
        "assigned_count": len(assigned),
        "access_seen_count": len(seen_from_log),
        "active_claimed_count": len(active),
        "effective_seen_count": len(effective_seen),
        "coverage_rate": round(len(covered) / pool_size, 6) if pool_size else 0,
        "effective_seen_rate": round(len(effective_seen) / pool_size, 6) if pool_size else 0,
        "claim_count": len(state.get("claims", {})),
        "submission_count": len(state.get("submissions", [])),
        "updated_at": state.get("updated_at"),
        "next_batch": next_batch,
        "next_claim_mode": next_mode,
        "next_claim_size": len(next_items),
    }


def public_leaderboard_payload(state: dict[str, Any]) -> dict[str, Any]:
    weekly_reviews = state.get("weekly_reviews", {}) if isinstance(state.get("weekly_reviews"), dict) else {}
    participant_reviews = weekly_reviews.get("participants", {}) if isinstance(weekly_reviews.get("participants"), dict) else {}
    participants: dict[str, dict[str, Any]] = {}
    for submission in state.get("submissions", []):
        participant_id = str(submission.get("participant_id") or "anonymous")
        entry = participants.setdefault(
            participant_id,
            {
                "participant_id": participant_id,
                "submissions": 0,
                "total_rows": 0,
                "valid_rows": 0,
                "first_coverage": 0,
                "public_points": 0,
                "score_100_sum": 0.0,
                "reason_quality_rows": 0,
                "followup_rows": 0,
                "last_seen_at": "",
                "weekly_review_points": 0,
                "weekly_review_status": "pending",
            },
        )
        rows = submission.get("rows") or []
        entry["submissions"] += 1
        entry["total_rows"] += int(submission.get("line_count") or len(rows))
        entry["valid_rows"] += int(submission.get("valid_count") or 0)
        entry["first_coverage"] += int(submission.get("first_coverage_count") or 0)
        entry["public_points"] += int(submission.get("public_points") or 0)
        entry["score_100_sum"] += float(submission.get("score_100") or 0)
        entry["last_seen_at"] = max(str(entry["last_seen_at"] or ""), str(submission.get("created_at") or ""))
        for row in rows:
            if not row.get("valid"):
                continue
            if row.get("reason_ok"):
                entry["reason_quality_rows"] += 1
            if row.get("needs_followup") == "yes":
                entry["followup_rows"] += 1

    items = []
    for entry in participants.values():
        participant_id = str(entry.get("participant_id") or "anonymous")
        review = participant_reviews.get(participant_id, {}) if isinstance(participant_reviews.get(participant_id), dict) else {}
        submissions = max(int(entry["submissions"]), 1)
        valid_rows = int(entry["valid_rows"])
        total_rows = max(int(entry["total_rows"]), 1)
        entry["avg_score_100"] = round(float(entry["score_100_sum"]) / submissions, 1)
        entry["valid_rate"] = round(valid_rows / total_rows, 4)
        entry["reason_quality_rate"] = round(int(entry["reason_quality_rows"]) / valid_rows, 4) if valid_rows else 0
        entry["participation_points"] = valid_rows
        entry["preliminary_effectiveness_score"] = round(
            entry["avg_score_100"] * 0.5
            + entry["valid_rate"] * 30
            + entry["reason_quality_rate"] * 20,
            1,
        )
        entry["weekly_review_points"] = int(review.get("points") or 0)
        entry["weekly_review_status"] = str(review.get("status") or ("reviewed" if review else "pending"))
        entry["effective_points"] = int(entry["weekly_review_points"])
        entry.pop("score_100_sum", None)
        items.append(entry)

    participation_items = sorted(
        items,
        key=lambda item: (
            int(item["participation_points"]),
            int(item["first_coverage"]),
            int(item["submissions"]),
            float(item["valid_rate"]),
        ),
        reverse=True,
    )
    for rank, item in enumerate(participation_items, start=1):
        item["participation_rank"] = rank

    effectiveness_items = sorted(
        items,
        key=lambda item: (
            int(item["weekly_review_points"]),
            float(item["preliminary_effectiveness_score"]),
            float(item["valid_rate"]),
            float(item["reason_quality_rate"]),
            int(item["valid_rows"]),
        ),
        reverse=True,
    )
    for rank, item in enumerate(effectiveness_items, start=1):
        item["effectiveness_rank"] = rank
        item["rank"] = rank

    return {
        "ok": True,
        "generated_at": now_iso(),
        "rules": {
            "public_note": "公开接口只展示接力进展和参与者聚合状态；隐藏候选和阶段复核标准不进入现场判读。",
        },
        "items": effectiveness_items,
        "participation_items": participation_items,
        "effectiveness_items": effectiveness_items,
    }


def organizer_leaderboard_payload(state: dict[str, Any]) -> dict[str, Any]:
    public = public_leaderboard_payload(state)
    review = organizer_review_payload(state)
    by_participant = {
        item["participant_id"]: {
            **item,
            "hit_existing_candidate": 0,
            "new_public_candidate": 0,
            "control_agreement": 0,
            "invalid": 0,
            "axis_match_count": 0,
            "review_bonus": 0,
            "competition_score": int(item.get("public_points") or 0),
        }
        for item in public.get("items", [])
    }
    for row in review.get("items", []):
        participant_id = str(row.get("participant_id") or "anonymous")
        entry = by_participant.setdefault(
            participant_id,
            {
                "participant_id": participant_id,
                "rank": 0,
                "submissions": 0,
                "total_rows": 0,
                "valid_rows": 0,
                "first_coverage": 0,
                "public_points": 0,
                "avg_score_100": 0,
                "valid_rate": 0,
                "reason_quality_rate": 0,
                "reason_quality_rows": 0,
                "followup_rows": 0,
                "last_seen_at": "",
                "hit_existing_candidate": 0,
                "new_public_candidate": 0,
                "control_agreement": 0,
                "invalid": 0,
                "axis_match_count": 0,
                "review_bonus": 0,
                "competition_score": 0,
            },
        )
        bucket = row.get("crosscheck", {}).get("bucket")
        if bucket in {"hit_existing_candidate", "new_public_candidate", "control_agreement", "invalid"}:
            entry[bucket] += 1
        entry["axis_match_count"] += int(
            row.get("crosscheck", {}).get("axis_alignment", {}).get("axis_match_count") or 0
        )

    for entry in by_participant.values():
        entry["review_bonus"] = (
            int(entry["hit_existing_candidate"]) * 20
            + int(entry["new_public_candidate"]) * 8
            + int(entry["control_agreement"]) * 1
            + int(entry["axis_match_count"]) * 2
            - int(entry["invalid"]) * 3
        )
        entry["competition_score"] = int(entry.get("public_points") or 0) + int(entry["review_bonus"])

    items = sorted(
        by_participant.values(),
        key=lambda item: (
            int(item["competition_score"]),
            int(item["hit_existing_candidate"]),
            int(item["new_public_candidate"]),
            int(item["public_points"]),
        ),
        reverse=True,
    )
    for rank, item in enumerate(items, start=1):
        item["organizer_rank"] = rank

    return {
        "ok": True,
        "generated_at": now_iso(),
        "rules": {
            "public_points": "valid + first_coverage + schema + evidence_detail + consistency",
            "review_bonus": {
                "hit_existing_candidate": 20,
                "new_public_candidate": 8,
                "control_agreement": 1,
                "axis_match": 2,
                "invalid": -3,
            },
            "visibility": "organizer-only; do not expose hidden candidate agreement during live play",
        },
        "items": items,
        "review_summary": review.get("summary"),
    }


def find_next_batch(state: dict[str, Any], skip_seen_logs: bool) -> int | None:
    manifest = load_manifest()
    batch_count = int(manifest.get("batch_count") or 0)
    covered = set(state.get("covered", {}).keys())
    active = active_claimed_keys(state)
    assigned = historically_claimed_keys(state)
    historical = parse_access_seen() if skip_seen_logs else set()
    preferred_blocked = covered | assigned | historical
    safe_blocked = covered | active | historical

    fallback: int | None = None
    for batch_number in range(1, batch_count + 1):
        keys = [item["image_key"] for item in batch_items(batch_number)]
        if keys and all(key not in safe_blocked for key in keys) and fallback is None:
            fallback = batch_number
        if keys and all(key not in preferred_blocked for key in keys):
            return batch_number
    return fallback


def select_claim_items(state: dict[str, Any], skip_seen_logs: bool) -> tuple[int | None, list[dict[str, Any]], str]:
    """Pick the next claim without relying only on fixed manifest batches.

    Fixed 5-image batches are still preferred because they are easy to reason
    about. If historical submissions or organizer backfills partially cover a
    fixed batch, this function can stitch together remaining unseen images so
    the tail of the pool does not get stranded.
    """
    manifest = load_manifest()
    batch_size = int(manifest.get("batch_size") or 5)
    batch_count = int(manifest.get("batch_count") or 0)
    all_items = [normalize_item(item) for item in (manifest.get("items") or [])]
    covered = set(state.get("covered", {}).keys())
    active = active_claimed_keys(state)
    assigned = historically_claimed_keys(state)
    historical = parse_access_seen() if skip_seen_logs else set()
    preferred_blocked = covered | assigned | historical
    safe_blocked = covered | active | historical

    fallback_batch: int | None = None
    fallback_items: list[dict[str, Any]] = []
    for batch_number in range(1, batch_count + 1):
        items = batch_items(batch_number)
        keys = [item["image_key"] for item in items]
        if keys and all(key not in safe_blocked for key in keys) and fallback_batch is None:
            fallback_batch = batch_number
            fallback_items = items
        if keys and all(key not in preferred_blocked for key in keys):
            return batch_number, items, "scheduled_batch"

    stitched = [item for item in all_items if item.get("image_key") not in preferred_blocked]
    if stitched:
        return None, stitched[:batch_size], "stitched_unseen"

    if fallback_batch is not None:
        return fallback_batch, fallback_items, "recycled_batch"

    recycled = [item for item in all_items if item.get("image_key") not in safe_blocked]
    if recycled:
        return None, recycled[:batch_size], "stitched_recycled"

    return None, [], "exhausted"


def make_claim(body: dict[str, Any]) -> dict[str, Any]:
    participant_id = str(body.get("participant_id") or "anonymous").strip()[:80] or "anonymous"
    skip_seen_logs = bool(body.get("skip_seen_logs", False))
    requested_batch = body.get("batch_number")
    with state_lock:
        state = load_state()
        if requested_batch is None:
            batch_number, items, claim_mode = select_claim_items(state, skip_seen_logs=skip_seen_logs)
        else:
            batch_number = int(requested_batch)
            items = batch_items(batch_number)
            claim_mode = "manual_batch"
        if not items:
            return {"ok": False, "error": "no available batch"}

        claim_id = uuid.uuid4().hex
        claim = {
            "claim_id": claim_id,
            "participant_id": participant_id,
            "batch_number": batch_number,
            "claim_mode": claim_mode,
            "items": items,
            "image_keys": [item["image_key"] for item in items],
            "status": "claimed",
            "claimed_at": now_iso(),
            "claimed_ts": time.time(),
        }
        state.setdefault("claims", {})[claim_id] = claim
        save_state(state)

    return {
        "ok": True,
        "claim_id": claim_id,
        "participant_id": participant_id,
        "batch_number": batch_number,
        "claim_mode": claim_mode,
        "items": items,
        "submit_to": "TopicLab Arcade branch reply",
        "output_format": "![](image_url) | role | anomaly_score | confidence | needs_followup | evidence_tags | quality_flags | reason",
        "note": "This service only assigns images. Submit the five-line answer in the TopicLab Arcade branch.",
    }


def review_source_id_from_path(path: str) -> str:
    name = Path(urlparse(path).path).name
    for suffix in ("_sample_review.png", "_review.png"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def image_file_for_source(source_id: str, kind: str) -> Path:
    if kind == "scatter":
        candidates = [
            ROOT / "all_sample_scatter" / f"{source_id}_sample_scatter.png",
            ROOT / "all_sample_scatter" / f"{source_id}_scatter.png",
        ]
    else:
        candidates = [
            ROOT / "all_sample_gp" / f"{source_id}_sample_gp.png",
            ROOT / "all_sample_gp" / f"{source_id}_gp.png",
        ]
    return next((path for path in candidates if path.exists()), candidates[0])


def load_display_font(size: int, *, bold: bool = False) -> Any:
    if ImageFont is None:
        return None
    candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simkai.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/ukai.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def wrap_text(draw: Any, text: str, font: Any, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in str(text or ""):
        candidate = current + char
        bbox = draw.textbbox((0, 0), candidate, font=font)
        if current and bbox[2] - bbox[0] > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def compact_feature_lines(source_id: str) -> list[str]:
    card = load_feature_cards().get(source_id, {})
    if not card:
        return ["特征卡暂缺；以图像形态为主。"]
    rows = [
        f"观测点 {card.get('n_obs') or '-'}；z={card.get('z') or '-'}；host_dlr={card.get('host_dlr') or '-'}",
        f"质量 {card.get('quality_tier') or '-'}；完整度 {card.get('feature_completeness') or '-'}",
        f"M≈{card.get('M_completed') or '-'}；dm15≈{card.get('dm15_completed') or '-'}；振幅≈{card.get('amplitude_completed') or '-'}",
        f"再亮计数≈{card.get('rebrightening_completed') or '0'}；峰值SNR≈{card.get('peak_snr') or '-'}",
    ]
    context: list[str] = []
    if card.get("gaia_is_stellar") or card.get("var_evidence"):
        context.append("恒星/变源上下文")
    if card.get("agn_evidence") or card.get("wise_agn"):
        context.append("AGN/核区上下文")
    rows.append("上下文：" + ("、".join(context) if context else "未见强先验提示"))
    return rows


def compact_feature_metrics(source_id: str) -> tuple[list[tuple[str, str]], list[str]]:
    card = load_feature_cards().get(source_id, {})
    if not card:
        return [], []
    quality_label = {
        "A_completed_high": "A 高质量",
        "B_completed_good": "B 可用",
        "C_imputed_usable": "C 插补可用",
        "D_low_quality": "D 低质量",
    }.get(str(card.get("quality_tier") or ""), str(card.get("quality_tier") or "-"))
    metrics = [
        ("观测点", str(card.get("n_obs") or "-")),
        ("红移 z", str(card.get("z") or "-")),
        ("宿主距离", str(card.get("host_dlr") or "-")),
        ("特征质量", quality_label),
        ("完整度", str(card.get("feature_completeness") or "-")),
        ("绝对星等", str(card.get("M_completed") or "-")),
        ("15天衰减", str(card.get("dm15_completed") or "-")),
        ("振幅", str(card.get("amplitude_completed") or "-")),
        ("再亮计数", str(card.get("rebrightening_completed") or "0")),
        ("峰值SNR", str(card.get("peak_snr") or "-")),
    ]
    context: list[str] = []
    if card.get("gaia_is_stellar") or card.get("var_evidence"):
        context.append("恒星/变源上下文")
    if card.get("agn_evidence") or card.get("wise_agn"):
        context.append("AGN/核区上下文")
    return metrics, context


def crop_plot_whitespace(image: Any, padding: int = 14) -> Any:
    if ImageChops is None:
        return image
    bg = Image.new(image.mode, image.size, image.getpixel((0, 0)))
    diff = ImageChops.difference(image, bg)
    bbox = diff.getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    left = max(left - padding, 0)
    top = max(top - padding, 0)
    right = min(right + padding, image.width)
    bottom = min(bottom + padding, image.height)
    # Avoid accidental over-cropping if the source image is already tight.
    if (right - left) < image.width * 0.45 or (bottom - top) < image.height * 0.45:
        return image
    return image.crop((left, top, right, bottom))


def fit_image(image: Any, max_size: tuple[int, int], max_upscale: float = 2.2) -> Any:
    max_w, max_h = max_size
    scale = min(max_w / image.width, max_h / image.height, max_upscale)
    if scale <= 0:
        return image
    target = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
    if target == image.size:
        return image
    return image.resize(target, Image.Resampling.LANCZOS)


def build_review_image(source_id: str) -> bytes:
    if Image is None or ImageDraw is None:
        raise RuntimeError("Pillow is required to render review composite images")

    scatter_path = image_file_for_source(source_id, "scatter")
    gp_path = image_file_for_source(source_id, "gp")
    main_path = scatter_path if scatter_path.exists() else gp_path
    if not main_path.exists():
        raise FileNotFoundError(f"source image not found: {source_id}")

    main = crop_plot_whitespace(Image.open(main_path).convert("RGB"))

    canvas_w, canvas_h = 1320, 620
    margin = 20
    card_w = 360
    main_w = canvas_w - card_w - margin * 3
    main_h = canvas_h - margin * 2

    canvas = Image.new("RGB", (canvas_w, canvas_h), "#f8fafc")
    draw = ImageDraw.Draw(canvas)
    title_font = load_display_font(26, bold=True)
    body_font = load_display_font(18)
    small_font = load_display_font(15)
    label_font = load_display_font(14)

    main = fit_image(main, (main_w, main_h))
    main_x = margin + (main_w - main.width) // 2
    main_y = margin + (main_h - main.height) // 2
    canvas.paste(main, (main_x, main_y))
    draw.rounded_rectangle((main_x - 1, main_y - 1, main_x + main.width + 1, main_y + main.height + 1), radius=14, outline="#cbd5e1", width=2)

    card_x = margin * 2 + main_w
    draw.rounded_rectangle((card_x, margin, canvas_w - margin, canvas_h - margin), radius=20, fill="#ffffff", outline="#dbe3ee", width=2)
    draw.text((card_x + 24, margin + 24), source_id, fill="#0f172a", font=title_font)
    draw.text((card_x + 24, margin + 62), "辅助观察卡 · 只作旁证", fill="#64748b", font=small_font)

    metrics, context_notes = compact_feature_metrics(source_id)
    y = margin + 100
    box_gap = 10
    box_w = (card_w - 60 - box_gap) // 2
    box_h = 58
    for idx, (label, value) in enumerate(metrics[:10]):
        col = idx % 2
        row = idx // 2
        x = card_x + 24 + col * (box_w + box_gap)
        top = y + row * (box_h + box_gap)
        draw.rounded_rectangle((x, top, x + box_w, top + box_h), radius=12, fill="#f8fafc", outline="#e2e8f0", width=1)
        draw.text((x + 12, top + 8), label, fill="#64748b", font=label_font)
        value_text = str(value)
        if len(value_text) > 13:
            value_text = value_text[:12] + "…"
        draw.text((x + 12, top + 30), value_text, fill="#0f172a", font=body_font)

    y = y + 5 * (box_h + box_gap) + 12
    if context_notes:
        draw.rounded_rectangle((card_x + 24, y, canvas_w - margin - 24, y + 72), radius=14, fill="#eef6ff", outline="#dbeafe", width=1)
        draw.text((card_x + 38, y + 12), "先验线索", fill="#1e3a8a", font=label_font)
        note_y = y + 34
        for line in wrap_text(draw, "；".join(context_notes), small_font, card_w - 84)[:2]:
            draw.text((card_x + 38, note_y), line, fill="#334155", font=small_font)
            note_y += 22

    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def has_visual_evidence(reason: str) -> bool:
    lower = reason.lower()
    return any(word.lower() in lower for word in VISUAL_WORDS)


def has_legacy_class_label(reason: str) -> bool:
    return bool(LEGACY_CLASS_PATTERN.search(reason))


def has_numeric_evidence(reason: str) -> bool:
    return bool(NUMERIC_EVIDENCE_PATTERN.search(reason or ""))


def has_physical_interpretation(reason: str) -> bool:
    text = str(reason or "").strip()
    if not text or MECHANICAL_REASON_PATTERN.search(text):
        return False
    return bool(PHYSICAL_MECHANISM_PATTERN.search(text) and REASONING_CONNECTOR_PATTERN.search(text))


def row_decision_consistency(row: dict[str, Any]) -> bool:
    role = str(row.get("role") or "")
    followup = str(row.get("needs_followup") or "")
    tags = set(row.get("evidence_tags") or [])
    quality = set(row.get("quality_flags") or [])
    try:
        anomaly = int(row.get("anomaly_score") or 0)
    except (TypeError, ValueError):
        return False

    strong_tags = {
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
    }
    has_strong_signal = bool(tags & strong_tags)
    has_quality_risk = bool(
        quality & QUALITY_RISK_FLAGS
        or tags & {"sparse_sampling", "background_or_contamination", "single_band_signal", "band_missing", "outlier_only", "low_snr", "unclear"}
    )

    if role in FOLLOWUP_ROLES and anomaly == 0:
        return False
    if role in LOW_PRIORITY_ROLES and anomaly >= 4:
        return False
    if role == "data_issue" and not has_quality_risk:
        return False
    if followup == "yes" and role in LOW_PRIORITY_ROLES and anomaly <= 1 and not has_quality_risk:
        return False
    if followup == "no" and role == "interesting" and anomaly >= 4 and has_strong_signal:
        return False
    if anomaly >= 3 and not (has_strong_signal or has_quality_risk):
        return False
    return True


def row_confidence_consistency(row: dict[str, Any]) -> bool:
    confidence = str(row.get("confidence") or "")
    quality = set(row.get("quality_flags") or [])
    tags = set(row.get("evidence_tags") or [])
    reason = str(row.get("reason") or "")
    has_quality_risk = bool(quality & QUALITY_RISK_FLAGS or tags & {"unclear", "low_snr", "sparse_sampling", "band_missing", "outlier_only"})

    if confidence == "high" and has_quality_risk:
        return False
    if confidence == "high" and not has_numeric_evidence(reason):
        return False
    if confidence == "low" and not has_quality_risk:
        return False
    return True


def line_score(row: dict[str, Any], first_coverage: bool) -> tuple[int, list[str]]:
    points = 0
    notes: list[str] = []
    if not row.get("valid"):
        return points, notes
    points += 1
    notes.append("+1 valid")
    if first_coverage:
        points += 1
        notes.append("+1 first_coverage")
    if row.get("protocol_version") == "v2":
        points += 1
        notes.append("+1 v2_schema")
    if row.get("reason_ok"):
        points += 1
        notes.append("+1 visual_reason")
    if has_numeric_evidence(str(row.get("reason") or "")) and has_physical_interpretation(str(row.get("reason") or "")):
        points += 1
        notes.append("+1 measured_physical_reason")

    tags = set(row.get("evidence_tags") or [])
    quality = set(row.get("quality_flags") or [])
    if tags and (tags - GENERIC_EVIDENCE_TAGS) and len(tags) <= 4:
        points += 2
        notes.append("+2 focused_evidence_tags")
    elif tags:
        points += 1
        notes.append("+1 evidence_tags")
    if quality and (quality != {"none"} or row.get("role") in LOW_PRIORITY_ROLES):
        points += 1
        notes.append("+1 quality_flags")
    if row_decision_consistency(row):
        points += 1
        notes.append("+1 decision_consistency")
    if row_confidence_consistency(row):
        points += 1
        notes.append("+1 confidence_consistency")
    return points, notes


def parse_submission_lines(text: str, claim: dict[str, Any] | None) -> list[dict[str, Any]]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    rows: list[dict[str, Any]] = []
    claim_items = [normalize_item(item) for item in (claim.get("items") or [])] if claim else []
    if claim and not claim_items and claim.get("batch_number") is not None:
        try:
            claim_items = batch_items(int(claim["batch_number"]))
        except (TypeError, ValueError):
            claim_items = []
    by_key = manifest_items_by_key()
    md_pattern = re.compile(r"^!\[\]\((?P<url>[^)]+)\)\s*\|\s*(?P<rest>.+)$")

    for line_index, line in enumerate(lines, start=1):
        image_url = ""
        rest = line
        md = md_pattern.match(line)
        if md:
            image_url = md.group("url").strip()
            rest = md.group("rest")
        parts = [part.strip() for part in rest.split("|", 7)]
        if not md and parts and re.fullmatch(r"\d+", parts[0]):
            ordinal = int(parts[0])
            if 1 <= ordinal <= len(claim_items):
                image_url = claim_items[ordinal - 1].get("image_url", "")
            parts = parts[1:]

        row: dict[str, Any] = {
            "line": line_index,
            "raw": line,
            "image_url": image_url,
            "image_key": image_key(image_url),
            "valid": False,
            "errors": [],
        }
        if len(parts) not in {5, 7}:
            row["errors"].append("expected 7 fields after image/index; legacy 5-field rows are still readable")
            rows.append(row)
            continue

        if len(parts) == 7:
            role, anomaly_text, confidence, followup, evidence_text, quality_text, reason = parts
            evidence_tags, evidence_alias_changed = normalize_tag_aliases(
                parse_tag_list(evidence_text),
                EVIDENCE_TAG_ALIASES,
                ALLOWED_EVIDENCE_TAGS,
            )
            quality_flags, quality_alias_changed = normalize_tag_aliases(
                parse_tag_list(quality_text),
                QUALITY_FLAG_ALIASES,
                ALLOWED_QUALITY_FLAGS,
            )
            protocol_version = "v2"
        else:
            role, anomaly_text, confidence, followup, reason = parts
            evidence_tags = []
            quality_flags = []
            evidence_alias_changed = False
            quality_alias_changed = False
            protocol_version = "v1_legacy"
        original_role = str(role or "").strip()
        role = ROLE_ALIASES.get(original_role, original_role)
        row.update(
            {
                "protocol_version": protocol_version,
                "role": role,
                "anomaly_score": anomaly_text,
                "confidence": confidence,
                "needs_followup": followup,
                "evidence_tags": evidence_tags,
                "quality_flags": quality_flags,
                "reason": reason,
            }
        )
        if evidence_alias_changed:
            append_backfill_field(row, "evidence_tags_alias_normalized")
        if quality_alias_changed:
            append_backfill_field(row, "quality_flags_alias_normalized")
        if role != original_role:
            append_backfill_field(row, "role_alias_normalized")
        backfill_row_annotations(row)
        evidence_tags = row.get("evidence_tags") or []
        quality_flags = row.get("quality_flags") or []

        errors: list[str] = []
        warnings: list[str] = []
        if row["image_key"] not in by_key:
            errors.append("image is not in public manifest")
        if role not in ALLOWED_ROLES:
            warnings.append(
                "role 不在推荐选项中；建议使用 interesting / bridge / data_issue / typical / control / unsure"
            )
        anomaly_value = parse_int(anomaly_text)
        if anomaly_value is None:
            warnings.append("anomaly_score 不是整数；建议填写 0 到 5 的整数")
        else:
            if str(anomaly_text).strip() != str(anomaly_value):
                warnings.append(f"anomaly_score 已按整数记录为 {anomaly_value}；建议下次直接填写整数")
            if anomaly_value < 0 or anomaly_value > 5:
                warnings.append("anomaly_score 超出推荐范围；建议使用 0 到 5")
            row["anomaly_score"] = anomaly_value
        if confidence not in ALLOWED_CONFIDENCE:
            warnings.append("confidence 不在推荐选项中；建议使用 high / medium / low")
        if followup not in ALLOWED_FOLLOWUP:
            warnings.append("needs_followup 不在推荐选项中；建议使用 yes 或 no")
        if protocol_version == "v2":
            if not evidence_tags:
                warnings.append("evidence_tags 为空；建议选 1 到 4 个能支持判断的证据标签")
            invalid_evidence = [tag for tag in evidence_tags if tag not in ALLOWED_EVIDENCE_TAGS]
            if invalid_evidence:
                warnings.append(warning_with_suggestions("evidence_tags 不在推荐列表中", invalid_evidence, ALLOWED_EVIDENCE_TAGS))
            if not quality_flags:
                warnings.append("quality_flags 为空；建议写 good_sampling、low_snr、sparse_sampling 或 none 等质量标记")
            invalid_quality = [flag for flag in quality_flags if flag not in ALLOWED_QUALITY_FLAGS]
            if invalid_quality:
                warnings.append(warning_with_suggestions("quality_flags 不在推荐列表中", invalid_quality, ALLOWED_QUALITY_FLAGS))
        reason_ok = len(reason) >= 8 and has_visual_evidence(reason) and has_physical_interpretation(reason)
        row["reason_ok"] = reason_ok
        if not reason_ok:
            warnings.append(
                "reason 可以再补一句：图上形态说明了什么、为什么值得或不值得回看，以及后续怎么核对"
            )
        row["legacy_label_detected"] = has_legacy_class_label(reason)

        row["valid"] = not errors
        row["errors"] = errors
        row["warnings"] = warnings
        row["anomaly_decision"] = anomaly_decision(row)
        if row["image_key"] in by_key:
            item = by_key[row["image_key"]]
            row["image_key"] = item.get("image_key")
            row["source_id"] = item.get("source_id")
            row["global_index"] = item.get("global_index")
            row["gp_image_url"] = item.get("gp_image_url")
            row["scatter_image_url"] = item.get("scatter_image_url")
            row["image_mode"] = item.get("image_mode")
            row["feature_text"] = item.get("feature_text")
        rows.append(row)
    return rows


def submit(body: dict[str, Any]) -> dict[str, Any]:
    participant_id = str(body.get("participant_id") or "anonymous").strip()[:80] or "anonymous"
    claim_id = str(body.get("claim_id") or "").strip()
    text = str(body.get("text") or body.get("submission") or "")
    if not text.strip():
        return {
            "ok": False,
            "error": "submission text is empty; submit exactly 5 non-empty lines for the current claim_id",
            "valid_count": 0,
            "rows": [],
        }
    with state_lock:
        state = load_state()
        claim = state.get("claims", {}).get(claim_id) if claim_id else None
        if not claim:
            return {"ok": False, "error": "claim_id is required; claim a batch before submitting"}
        if claim.get("status") == "submitted":
            return {"ok": False, "error": "this claim has already been submitted"}
        rows = parse_submission_lines(text, claim)
        covered = state.setdefault("covered", {})
        submission_id = uuid.uuid4().hex
        total_points = 0
        valid_count = 0
        first_coverage_count = 0
        expected_lines = len(claim.get("image_keys") or []) or 5
        line_count_ok = len(rows) == expected_lines

        for row in rows:
            if not line_count_ok:
                row.setdefault("errors", []).append(f"expected exactly {expected_lines} lines")
                row["valid"] = False
            first = bool(row.get("valid") and row["image_key"] not in covered)
            points, notes = line_score(row, first)
            row["public_points"] = points
            row["public_notes"] = notes
            total_points += points
            if row.get("valid"):
                valid_count += 1
            if first:
                first_coverage_count += 1
                covered[row["image_key"]] = {
                    "submission_id": submission_id,
                    "participant_id": participant_id,
                    "source_id": row.get("source_id"),
                    "global_index": row.get("global_index"),
                    "covered_at": now_iso(),
                }

        max_points = max(expected_lines, 5) * LINE_SCORE_MAX
        score_100 = round(total_points / max_points * 100, 1) if max_points else 0
        submission = {
            "submission_id": submission_id,
            "participant_id": participant_id,
            "claim_id": claim_id or None,
            "batch_number": claim.get("batch_number") if claim else body.get("batch_number"),
            "created_at": now_iso(),
            "raw_text": text,
            "line_count": len(rows),
            "valid_count": valid_count if line_count_ok else 0,
            "first_coverage_count": first_coverage_count,
            "public_points": total_points,
            "score_100": score_100,
            "rows": rows,
        }
        state.setdefault("submissions", []).append(submission)
        if claim:
            claim["status"] = "submitted"
            claim["submitted_at"] = now_iso()
            claim["submission_id"] = submission_id
        save_state(state)
        write_results_exports(state)
        next_batch = find_next_batch(state, skip_seen_logs=False)

    return {
        "ok": line_count_ok and valid_count > 0,
        "submission_id": submission_id,
        "valid_count": valid_count if line_count_ok else 0,
        "first_coverage_count": first_coverage_count,
        "rows": [{k: v for k, v in row.items() if k not in {"public_points", "public_notes"}} for row in rows],
        "next_batch": next_batch,
        "feedback": (
            f"已记录 {(valid_count if line_count_ok else 0)}/{expected_lines} 行有效判读；"
            f"首次覆盖 {first_coverage_count} 张。"
        ),
    }


class RelayHandler(SimpleHTTPRequestHandler):
    server_version = "DataSampleRelay/1.0"

    def safe_write(self, data: bytes) -> None:
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browser screenshots and model clients may abandon image loads while
            # scrolling. The request is harmless and should not pollute logs.
            return

    def list_directory(self, path: str):  # type: ignore[override]
        self.write_json({"ok": False, "error": "directory listing is disabled"}, status=HTTPStatus.FORBIDDEN)
        return None

    def is_public_static_path(self, path: str) -> bool:
        normalized = "/" + path.lstrip("/")
        lower = normalized.lower()
        if normalized in PUBLIC_STATIC_FILES:
            return True
        if any(normalized.startswith(prefix) for prefix in PUBLIC_IMAGE_PREFIXES):
            return lower.endswith(PUBLIC_IMAGE_EXTENSIONS)
        return False

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/all_sample_review/") and parsed.path.lower().endswith(".png"):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self.is_public_static_path(parsed.path):
            self.send_response(HTTPStatus.FORBIDDEN)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        super().do_HEAD()

    def guess_type(self, path: str) -> str:
        content_type = super().guess_type(path)
        lower_path = path.lower()
        if lower_path.endswith(".md"):
            return "text/markdown; charset=utf-8"
        if lower_path.endswith((".txt", ".csv")):
            return "text/plain; charset=utf-8"
        if lower_path.endswith((".html", ".css", ".js")) and "charset=" not in content_type:
            return f"{content_type}; charset=utf-8"
        return content_type

    def end_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.write_json({"ok": True, "service": "data-sample-relay", "time": now_iso()})
            return
        if parsed.path == "/api/status":
            with state_lock:
                state = load_state()
                payload = status_payload(state)
            self.write_json(payload)
            return
        if parsed.path == "/api/submissions":
            self.write_json(
                {
                    "ok": False,
                    "error": "submissions are reviewed in TopicLab Arcade branches; this data service does not expose a public submission feed",
                },
                status=HTTPStatus.GONE,
            )
            return
        if parsed.path == "/api/leaderboard":
            self.write_json(
                {
                    "ok": False,
                    "error": "leaderboards are maintained from TopicLab Arcade review results, not from this data service",
                },
                status=HTTPStatus.GONE,
            )
            return
        if parsed.path == "/api/organizer/review":
            if not self.is_local_client():
                self.write_json({"ok": False, "error": "organizer review is local-only"}, status=HTTPStatus.FORBIDDEN)
                return
            with state_lock:
                state = load_state()
                payload = organizer_review_payload(state)
            self.write_json(payload)
            return
        if parsed.path == "/api/organizer/review.csv":
            if not self.is_local_client():
                self.write_json({"ok": False, "error": "organizer review is local-only"}, status=HTTPStatus.FORBIDDEN)
                return
            with state_lock:
                state = load_state()
                payload = organizer_review_payload(state)
            self.write_text(organizer_review_csv(payload), "text/csv; charset=utf-8")
            return
        if parsed.path == "/api/organizer/leaderboard":
            if not self.is_local_client():
                self.write_json({"ok": False, "error": "organizer leaderboard is local-only"}, status=HTTPStatus.FORBIDDEN)
                return
            with state_lock:
                state = load_state()
                payload = organizer_leaderboard_payload(state)
            self.write_json(payload)
            return
        if parsed.path.startswith("/all_sample_review/") and parsed.path.lower().endswith(".png"):
            source_id = review_source_id_from_path(parsed.path)
            try:
                data = build_review_image(source_id)
            except FileNotFoundError:
                self.write_json({"ok": False, "error": f"source image not found: {source_id}"}, status=HTTPStatus.NOT_FOUND)
                return
            except Exception as exc:
                self.write_json({"ok": False, "error": f"review image render failed: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "image/png")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.safe_write(data)
            return
        if not self.is_public_static_path(parsed.path):
            self.write_json({"ok": False, "error": "not a public resource"}, status=HTTPStatus.FORBIDDEN)
            return
        super().do_GET()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self.read_json_body()
            if parsed.path == "/api/claim":
                if (body.get("batch_number") is not None or body.get("skip_seen_logs")) and not self.is_local_client():
                    self.write_json(
                        {"ok": False, "error": "public claims are assigned by the relay; batch override is organizer-only"},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                self.write_json(make_claim(body))
                return
            if parsed.path == "/api/submit":
                self.write_json(
                    {
                        "ok": False,
                        "error": "submit in the TopicLab Arcade branch; this data service only assigns images",
                    },
                    status=HTTPStatus.GONE,
                )
                return
            self.write_json({"ok": False, "error": "unknown endpoint"}, status=HTTPStatus.NOT_FOUND)
        except Exception:  # Keep API clients from receiving Python internals.
            traceback.print_exc(file=sys.stderr)
            self.write_json(
                {"ok": False, "error": "request failed; check request format and claim state"},
                status=HTTPStatus.BAD_REQUEST,
            )

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def write_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(self.with_public_image_urls(payload), ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.safe_write(data)

    def public_base_url(self) -> str:
        explicit = os.environ.get("RELAY_PUBLIC_BASE_URL", "").strip().rstrip("/")
        if explicit:
            return explicit
        host = self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}".rstrip("/")

    def public_image_url(self, value: Any) -> Any:
        if not isinstance(value, str) or (
            "/all_sample_review/" not in value
            and "/all_sample_scatter/" not in value
            and "/all_sample_gp/" not in value
        ):
            return value
        key = image_key(value)
        return f"{self.public_base_url()}{key}"

    def with_public_image_urls(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self.public_image_url(item)
                if key in {"image_url", "review_image_url", "gp_image_url", "scatter_image_url"}
                else self.with_public_image_urls(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.with_public_image_urls(item) for item in value]
        return value

    def write_text(self, text: str, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = text.encode("utf-8-sig")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.safe_write(data)

    def is_local_client(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        return host in {"127.0.0.1", "::1", "localhost"} or host.startswith("::ffff:127.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8788")))
    args = parser.parse_args()
    os.chdir(ROOT)
    load_manifest()
    server = ThreadingHTTPServer((args.host, args.port), RelayHandler)
    print(f"Serving DATA_SAMPLE relay from {ROOT} on http://{args.host}:{args.port}", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
