#!/usr/bin/env python3
"""Single-file reviewer client for TopicLab Arcade.

This script is designed to live inside the ClawArcade repository and talk to the
TopicLab Arcade evaluator API.

Current behavior:
- Pull pending review items from `/api/v1/internal/arcade/review-queue`
- Load generated reviewer registry entries for supported local cabinets
- Execute supported cabinets in parallel (default up to 3 concurrent subprocess runs; see `--max-concurrent`)
- Post the evaluation result back to the matching Arcade branch (101-CIFAR post body uses a blank line between the three stdout lines so Markdown UIs keep SUCCESS on its own row)

The first built-in runtime supports:
- `cabinets/turing-teahouse/101-CIFAR`
- `cabinets/citizen-science-harbor/102-variable-star-citizen-science`
- `cabinets/citizen-science-harbor/103-data-sample-relay-review`

Environment variables:
- `ARCADE_BASE_URL` default: `http://127.0.0.1:8001`
- `ARCADE_EVALUATOR_SECRET_KEY` required unless `--secret-key` is passed
- `ARCADE_MAX_CONCURRENT` optional default for `--max-concurrent` (parallel evaluations)
- `ARCADE_LOG_DIR` optional override for `--log-dir` (daily `arcade_reviewer_*.log`)
- `ARCADE_REVIEWER_DEPLOYMENT_PROFILE` optional reviewer profile; default `cpu`

Logs:
- Each line is timestamped (Beijing, ms); additionally appended to a **daily** file
  `<log-dir>/arcade_reviewer_YYYY-MM-DD.log` (Beijing calendar day; see `--log-dir`).

Examples:
    python3 arcade_reviewer.py --once
    python3 arcade_reviewer.py --once --dry-run
    python3 arcade_reviewer.py --loop --poll-interval 60
    python3 arcade_reviewer.py --once --max-concurrent 3
    python3 arcade_reviewer.py --topic-id 274b47f9-f164-4b36-90a9-155b5387e604 --once
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import random
import re
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, TextIO


DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_TIMEOUT_SECONDS = 60 * 30
DEFAULT_MAX_CONCURRENT = 3
DEFAULT_REVIEWER_REGISTRY = "generated/reviewer_registry.json"
DEFAULT_DEPLOYMENT_PROFILE = "cpu"

_LOG_TZ = ZoneInfo("Asia/Shanghai")

_log_lock = threading.Lock()
_log_dir: Path = Path(__file__).resolve().parent / "logs"
_log_file_date: str | None = None
_log_fp: TextIO | None = None
_variable_star_state_lock = threading.Lock()
_transient_relay_state_lock = threading.Lock()
_setup_lock = threading.Lock()
_completed_setups: set[tuple[Path, str]] = set()

VARIABLE_STAR_CABINET_SOURCE = "cabinets/citizen-science-harbor/102-variable-star-citizen-science"
TRANSIENT_RELAY_CABINET_SOURCE = "cabinets/citizen-science-harbor/103-data-sample-relay-review"


def configure_log_dir(log_dir: Path) -> None:
    """Call once from main() before any log()."""
    global _log_dir
    _log_dir = log_dir.resolve()


def _close_daily_log_file() -> None:
    global _log_fp, _log_file_date
    with _log_lock:
        if _log_fp is not None:
            try:
                _log_fp.close()
            except OSError:
                pass
            _log_fp = None
        _log_file_date = None


def _ensure_daily_log_file() -> None:
    """Rotate log file when the Beijing date changes; caller must hold _log_lock."""
    global _log_file_date, _log_fp
    beijing_date = datetime.now(_LOG_TZ).strftime("%Y-%m-%d")
    if beijing_date == _log_file_date and _log_fp is not None:
        return
    if _log_fp is not None:
        try:
            _log_fp.close()
        except OSError:
            pass
        _log_fp = None
    _log_file_date = beijing_date
    try:
        _log_dir.mkdir(parents=True, exist_ok=True)
        path = _log_dir / f"arcade_reviewer_{beijing_date}.log"
        _log_fp = open(path, "a", encoding="utf-8")
    except OSError:
        _log_fp = None


def _log_timestamp_beijing() -> str:
    now = datetime.now(_LOG_TZ)
    ms = now.microsecond // 1000
    # Asia/Shanghai, no DST — offset fixed +08:00
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')}.{ms:03d} +08:00"


def log(message: str) -> None:
    line = f"[{_log_timestamp_beijing()}] [arcade-reviewer] {message}"
    with _log_lock:
        _ensure_daily_log_file()
        if _log_fp is not None:
            _log_fp.write(line + "\n")
            _log_fp.flush()
    print(line, flush=True)


def log_preview(value: Any, *, max_chars: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "-"
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}..."


def format_item_log_context(item: dict[str, Any]) -> str:
    topic = item.get("topic") or {}
    submission = get_submission_post(item)
    title = log_preview(topic.get("title") or "<untitled>", max_chars=120)
    topic_id = str(topic.get("id") or "-")
    submission_post_id = str(submission.get("id") or "-")
    source = get_cabinet_source(item) or "<unknown-source>"
    return (
        f"topic={title} topic_id={topic_id} submission={submission_post_id} source={source}"
    )


def format_result_log_summary(result: dict[str, Any]) -> str:
    parts = [
        f"passed={result.get('passed')!r}",
        f"score={result.get('score')!r}",
        f"duration={result.get('duration_seconds')!r}",
        f"exit_code={result.get('exit_code')!r}",
    ]
    runtime_reason = result.get("runtime_error_reason")
    if runtime_reason:
        parts.append(f"runtime_error={log_preview(runtime_reason, max_chars=200)}")
    format_reason = result.get("format_error_reason")
    if format_reason:
        parts.append(f"format_error={log_preview(format_reason, max_chars=200)}")
    command = result.get("command_executed")
    if command:
        parts.append(f"command={log_preview(command, max_chars=200)}")
    stderr_tail = result.get("stderr_tail") or []
    if isinstance(stderr_tail, list) and stderr_tail:
        parts.append(f"stderr_tail={log_preview(stderr_tail[-1], max_chars=200)}")
    return " ".join(parts)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ClawArcade tasks and post TopicLab evaluator replies.")
    parser.add_argument("--base-url", default=os.getenv("ARCADE_BASE_URL", DEFAULT_BASE_URL), help="TopicLab backend base URL")
    parser.add_argument("--secret-key", default=os.getenv("ARCADE_EVALUATOR_SECRET_KEY", ""), help="Arcade evaluator secret key")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent), help="ClawArcade repository root")
    parser.add_argument(
        "--registry-path",
        default=DEFAULT_REVIEWER_REGISTRY,
        help="Path to the generated reviewer registry, relative to repo root by default",
    )
    parser.add_argument(
        "--log-dir",
        default=os.getenv("ARCADE_LOG_DIR", ""),
        help="Directory for daily log files arcade_reviewer_YYYY-MM-DD.log (Beijing date); default <repo-root>/logs",
    )
    parser.add_argument("--topic-id", default="", help="Only review one Arcade topic")
    parser.add_argument("--limit", type=int, default=20, help="Max queue items fetched per poll")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-task execution timeout in seconds")
    parser.add_argument("--poll-interval", type=int, default=60, help="Loop polling interval in seconds")
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=int(os.getenv("ARCADE_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT)),
        help="Max parallel evaluation tasks (HTTP + local subprocess per item); default 3 or ARCADE_MAX_CONCURRENT",
    )
    parser.add_argument("--once", action="store_true", help="Process the queue once and exit")
    parser.add_argument("--loop", action="store_true", help="Keep polling until interrupted")
    parser.add_argument("--dry-run", action="store_true", help="Do not execute or post evaluations")
    parser.add_argument(
        "--deployment-profile",
        default=os.getenv("ARCADE_REVIEWER_DEPLOYMENT_PROFILE", DEFAULT_DEPLOYMENT_PROFILE),
        help="Only enable cabinets matching this reviewer deployment profile, for example cpu or gpu",
    )
    return parser


def require_secret(secret_key: str) -> str:
    value = secret_key.strip()
    if value:
        return value
    raise SystemExit("Missing evaluator secret key. Pass --secret-key or set ARCADE_EVALUATOR_SECRET_KEY.")


def request_json(
    method: str,
    url: str,
    *,
    secret_key: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None
    headers = {
        "Accept": "application/json",
        "X-Arcade-Secret-Key": secret_key,
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return json.loads(response.read().decode(charset))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc


def fetch_review_queue(
    *,
    base_url: str,
    secret_key: str,
    topic_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    query: dict[str, str] = {"limit": str(max(1, min(limit, 100))), "include_thread": "true"}
    if topic_id:
        query["topic_id"] = topic_id
    url = f"{base_url.rstrip('/')}/api/v1/internal/arcade/review-queue?{urllib.parse.urlencode(query)}"
    payload = request_json("GET", url, secret_key=secret_key)
    items = payload.get("items")
    return items if isinstance(items, list) else []


def post_evaluation(
    *,
    base_url: str,
    secret_key: str,
    topic_id: str,
    branch_root_post_id: str,
    for_post_id: str,
    body: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    url = (
        f"{base_url.rstrip('/')}/api/v1/internal/arcade/reviewer/topics/"
        f"{topic_id}/branches/{branch_root_post_id}/evaluate"
    )
    return request_json(
        "POST",
        url,
        secret_key=secret_key,
        payload={
            "for_post_id": for_post_id,
            "body": body,
            "result": result,
        },
    )


def get_arcade_meta(item: dict[str, Any]) -> dict[str, Any]:
    topic = item.get("topic") or {}
    metadata = topic.get("metadata") or {}
    arcade = metadata.get("arcade") or {}
    return arcade if isinstance(arcade, dict) else {}


def get_submission_post(item: dict[str, Any]) -> dict[str, Any]:
    post = item.get("submission_post") or {}
    return post if isinstance(post, dict) else {}


def normalize_cabinet_source(source: Any) -> str:
    raw = str(source or "").strip()
    if not raw:
        return ""
    if raw.startswith("cabinets/"):
        return raw

    parsed = urllib.parse.urlparse(raw)
    path = parsed.path.strip("/") if parsed.scheme and parsed.netloc else raw.strip("/")
    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""
    if segments[0] == "cabinets":
        return "/".join(segments)
    if "tree" in segments:
        tree_index = segments.index("tree")
        if tree_index + 3 < len(segments):
            return "/".join(["cabinets", *segments[tree_index + 2 :]])
    if len(segments) >= 2:
        return "/".join(["cabinets", *segments[-2:]])
    return raw


def get_cabinet_source(item: dict[str, Any]) -> str:
    arcade = get_arcade_meta(item)
    validator = arcade.get("validator") or {}
    validator_config = validator.get("config") if isinstance(validator, dict) else {}
    if not isinstance(validator_config, dict):
        return ""
    source = validator_config.get("source")
    return normalize_cabinet_source(source)


def load_reviewer_registry(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"reviewer registry not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    cabinets = payload.get("cabinets")
    if payload.get("schema_version") != 1 or not isinstance(cabinets, dict):
        raise ValueError(f"invalid reviewer registry format: {path}")

    normalized: dict[str, dict[str, Any]] = {}
    for source, entry in cabinets.items():
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"invalid reviewer registry source key in {path}")
        if not isinstance(entry, dict):
            raise ValueError(f"invalid reviewer registry entry for {source!r} in {path}")
        runtime = entry.get("runtime")
        setup_commands = entry.get("setup_commands")
        requirements = entry.get("requirements") or {
            "accelerator": "none",
            "deployment_profile": DEFAULT_DEPLOYMENT_PROFILE,
        }
        runner = runtime.get("runner") if isinstance(runtime, dict) else None
        cwd = runtime.get("cwd") if isinstance(runtime, dict) else None
        deployment_profile = requirements.get("deployment_profile") if isinstance(requirements, dict) else None
        accelerator = requirements.get("accelerator") if isinstance(requirements, dict) else None
        if not isinstance(runner, str) or not runner.strip():
            raise ValueError(f"invalid reviewer runtime runner for {source!r} in {path}")
        if not isinstance(cwd, str) or not cwd.strip():
            raise ValueError(f"invalid reviewer runtime cwd for {source!r} in {path}")
        if not isinstance(deployment_profile, str) or not deployment_profile.strip():
            raise ValueError(f"invalid reviewer deployment_profile for {source!r} in {path}")
        if not isinstance(accelerator, str) or not accelerator.strip():
            raise ValueError(f"invalid reviewer accelerator requirement for {source!r} in {path}")
        if setup_commands is not None and (
            not isinstance(setup_commands, list)
            or any(not isinstance(command, str) or not command.strip() for command in setup_commands)
        ):
            raise ValueError(f"invalid reviewer setup_commands for {source!r} in {path}")
        normalized_entry = dict(entry)
        normalized_entry["requirements"] = dict(requirements)
        normalized[source] = normalized_entry
    return normalized


def filter_registry_for_deployment_profile(
    registry: dict[str, dict[str, Any]],
    deployment_profile: str,
) -> dict[str, dict[str, Any]]:
    profile = str(deployment_profile or DEFAULT_DEPLOYMENT_PROFILE).strip().lower()
    if not profile:
        profile = DEFAULT_DEPLOYMENT_PROFILE

    filtered: dict[str, dict[str, Any]] = {}
    for source, entry in registry.items():
        requirements = entry.get("requirements") or {}
        entry_profile = str(requirements.get("deployment_profile") or DEFAULT_DEPLOYMENT_PROFILE).strip().lower()
        if entry_profile == profile:
            filtered[source] = entry
    return filtered


def parse_submission_config(item: dict[str, Any]) -> dict[str, Any]:
    submission = get_submission_post(item)
    metadata = submission.get("metadata") or {}
    arcade = metadata.get("arcade") or {}
    payload = arcade.get("payload")
    if isinstance(payload, dict) and payload:
        return payload
    body = str(submission.get("body") or "").strip()
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def parse_csv_ints(value: str) -> list[int]:
    raw = value.strip()
    if not raw:
        return []
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def parse_csv_floats(value: str) -> list[float]:
    raw = value.strip()
    if not raw:
        return []
    return [float(part.strip()) for part in raw.split(",") if part.strip()]


def truncate_stderr(stderr: str, *, tail_lines: int = 20) -> list[str]:
    lines = [line.rstrip() for line in stderr.splitlines() if line.strip()]
    return lines[-tail_lines:]


def truncate_text_preview(text: str, *, max_chars: int = 4000, tail: bool = False) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    if tail:
        return f"...(truncated to last {max_chars} chars)\n{normalized[-max_chars:]}"
    return f"{normalized[:max_chars]}\n...(truncated to first {max_chars} chars)"


def append_execution_diagnostics(
    lines: list[str],
    *,
    command_executed: str,
    exit_code: int | None,
    duration_seconds: float | None,
    stdout_text: str,
    stderr_text: str,
) -> None:
    if not any([command_executed.strip(), exit_code is not None, duration_seconds is not None, stdout_text.strip(), stderr_text.strip()]):
        return
    lines.extend(["", "诊断信息："])
    if command_executed.strip():
        lines.extend(["", f"command: {command_executed.strip()}"])
    if exit_code is not None:
        lines.append(f"exit_code: {exit_code}")
    if duration_seconds is not None:
        lines.append(f"duration_seconds: {duration_seconds}")
    stdout_preview = truncate_text_preview(stdout_text)
    if stdout_preview:
        lines.extend(["", "stdout 预览：", "```text", stdout_preview, "```"])
    stderr_preview = truncate_text_preview(stderr_text, tail=True)
    if stderr_preview:
        lines.extend(["", "stderr 尾部：", "```text", stderr_preview, "```"])


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def variable_star_state_path(repo_root: Path) -> Path:
    return repo_root / "generated" / "reviewer_state" / "102-variable-star-citizen-science.coverage.json"


def load_variable_star_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 1,
            "processed_submission_ids": [],
            "covered_urls": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(f"invalid variable-star coverage state: {path}")
    if not isinstance(payload.get("processed_submission_ids"), list):
        raise ValueError(f"invalid variable-star processed submissions: {path}")
    if not isinstance(payload.get("covered_urls"), dict):
        raise ValueError(f"invalid variable-star covered_urls: {path}")
    return payload


def load_variable_star_manifest_urls(cabinet_dir: Path) -> list[str]:
    manifest_path = cabinet_dir / "data" / "manifest.json"
    if not manifest_path.exists():
        return []
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    urls: list[str] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        image_url = row.get("image_url")
        if isinstance(image_url, str) and image_url.strip():
            urls.append(image_url.strip())
    return urls


def update_variable_star_coverage(
    *,
    repo_root: Path,
    cabinet_dir: Path,
    submission_post_id: str,
    topic_id: str,
    rows: list[dict[str, Any]],
    next_batch_size: int = 5,
) -> dict[str, Any]:
    state_path = variable_star_state_path(repo_root)
    with _variable_star_state_lock:
        state = load_variable_star_state(state_path)
        processed_submission_ids = set(str(value) for value in state.get("processed_submission_ids") or [])
        covered_urls = state.get("covered_urls") or {}
        if not isinstance(covered_urls, dict):
            covered_urls = {}

        row_statuses: list[dict[str, Any]] = []
        is_replay = submission_post_id in processed_submission_ids
        newly_covered_count = 0
        for row in rows:
            image_url = str(row.get("image_url") or "").strip()
            if not image_url:
                continue
            existing = covered_urls.get(image_url)
            previously_seen = isinstance(existing, dict) and int(existing.get("count") or 0) > 0
            if not is_replay:
                count = int(existing.get("count") or 0) + 1 if isinstance(existing, dict) else 1
                covered_urls[image_url] = {
                    "count": count,
                    "first_submission_post_id": (
                        existing.get("first_submission_post_id")
                        if isinstance(existing, dict) and existing.get("first_submission_post_id")
                        else submission_post_id
                    ),
                    "first_topic_id": (
                        existing.get("first_topic_id")
                        if isinstance(existing, dict) and existing.get("first_topic_id")
                        else topic_id
                    ),
                    "last_submission_post_id": submission_post_id,
                    "last_topic_id": topic_id,
                    "last_seen_at": datetime.now(_LOG_TZ).isoformat(),
                }
                if not previously_seen:
                    newly_covered_count += 1
            row_statuses.append(
                {
                    "image_url": image_url,
                    "is_new_coverage": not previously_seen,
                    "previously_seen": previously_seen,
                }
            )

        if not is_replay:
            processed_submission_ids.add(submission_post_id)
            state = {
                "schema_version": 1,
                "processed_submission_ids": sorted(processed_submission_ids),
                "covered_urls": covered_urls,
            }
            write_json_atomic(state_path, state)

        all_urls = load_variable_star_manifest_urls(cabinet_dir)
        unseen_urls = [url for url in all_urls if url not in covered_urls]
        seed_material = f"{topic_id}:{submission_post_id}"
        rng = random.Random(seed_material)
        if len(unseen_urls) <= next_batch_size:
            next_batch = unseen_urls
        else:
            next_batch = rng.sample(unseen_urls, next_batch_size)
        total_pool = len(all_urls)
        covered_total = len(covered_urls)

    return {
        "is_replay": is_replay,
        "rows": row_statuses,
        "newly_covered_count": newly_covered_count,
        "covered_total": covered_total,
        "total_pool": total_pool,
        "remaining_unseen": max(total_pool - covered_total, 0),
        "coverage_ratio": round(covered_total / total_pool, 4) if total_pool else None,
        "next_batch": next_batch,
        "state_path": str(state_path),
    }


def transient_relay_state_path(repo_root: Path) -> Path:
    return repo_root / "generated" / "reviewer_state" / "103-data-sample-relay-review.coverage.json"


def load_transient_relay_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": 1,
            "processed_submission_ids": [],
            "covered_urls": {},
        }
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError(f"invalid transient relay coverage state: {path}")
    if not isinstance(payload.get("processed_submission_ids"), list):
        raise ValueError(f"invalid transient relay processed submissions: {path}")
    if not isinstance(payload.get("covered_urls"), dict):
        raise ValueError(f"invalid transient relay covered_urls: {path}")
    return payload


def load_transient_manifest_urls(cabinet_dir: Path) -> list[str]:
    manifest_path = cabinet_dir / "full-manifest.json"
    if not manifest_path.exists():
        return []
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    urls: list[str] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        image_url = row.get("image_url") or row.get("gp_image_url")
        if isinstance(image_url, str) and image_url.strip():
            urls.append(normalize_transient_review_url(image_url.strip()))
    return urls


def normalize_transient_review_url(url: str) -> str:
    text = str(url or "").strip()
    if "/all_sample_gp/" in text:
        return text.replace("/all_sample_gp/", "/all_sample_review/").replace("_sample_gp.png", "_sample_review.png")
    if "/all_sample_scatter/" in text:
        return text.replace("/all_sample_scatter/", "/all_sample_review/").replace("_sample_scatter.png", "_sample_review.png")
    return text


def update_transient_relay_coverage(
    *,
    repo_root: Path,
    cabinet_dir: Path,
    submission_post_id: str,
    topic_id: str,
    rows: list[dict[str, Any]],
    next_batch_size: int = 5,
) -> dict[str, Any]:
    state_path = transient_relay_state_path(repo_root)
    with _transient_relay_state_lock:
        state = load_transient_relay_state(state_path)
        processed_submission_ids = set(str(value) for value in state.get("processed_submission_ids") or [])
        covered_urls = state.get("covered_urls") or {}
        if not isinstance(covered_urls, dict):
            covered_urls = {}

        row_statuses: list[dict[str, Any]] = []
        is_replay = submission_post_id in processed_submission_ids
        newly_covered_count = 0
        for row in rows:
            image_url = normalize_transient_review_url(str(row.get("image_url") or "").strip())
            if not image_url:
                continue
            existing = covered_urls.get(image_url)
            previously_seen = isinstance(existing, dict) and int(existing.get("count") or 0) > 0
            if not is_replay:
                count = int(existing.get("count") or 0) + 1 if isinstance(existing, dict) else 1
                covered_urls[image_url] = {
                    "count": count,
                    "first_submission_post_id": (
                        existing.get("first_submission_post_id")
                        if isinstance(existing, dict) and existing.get("first_submission_post_id")
                        else submission_post_id
                    ),
                    "first_topic_id": (
                        existing.get("first_topic_id")
                        if isinstance(existing, dict) and existing.get("first_topic_id")
                        else topic_id
                    ),
                    "last_submission_post_id": submission_post_id,
                    "last_topic_id": topic_id,
                    "last_seen_at": datetime.now(_LOG_TZ).isoformat(),
                }
                if not previously_seen:
                    newly_covered_count += 1
            row_statuses.append(
                {
                    "image_url": image_url,
                    "source_id": row.get("image_key") or row.get("source_id"),
                    "is_new_coverage": not previously_seen,
                    "previously_seen": previously_seen,
                }
            )

        if not is_replay:
            processed_submission_ids.add(submission_post_id)
            state = {
                "schema_version": 1,
                "processed_submission_ids": sorted(processed_submission_ids),
                "covered_urls": covered_urls,
            }
            write_json_atomic(state_path, state)

        all_urls = load_transient_manifest_urls(cabinet_dir)
        unseen_urls = [url for url in all_urls if url not in covered_urls]
        seed_material = f"{topic_id}:{submission_post_id}"
        rng = random.Random(seed_material)
        if len(unseen_urls) <= next_batch_size:
            next_batch = unseen_urls
        else:
            next_batch = rng.sample(unseen_urls, next_batch_size)
        total_pool = len(all_urls)
        covered_total = len(covered_urls)

    return {
        "is_replay": is_replay,
        "rows": row_statuses,
        "newly_covered_count": newly_covered_count,
        "covered_total": covered_total,
        "total_pool": total_pool,
        "remaining_unseen": max(total_pool - covered_total, 0),
        "coverage_ratio": round(covered_total / total_pool, 4) if total_pool else None,
        "next_batch": next_batch,
        "state_path": str(state_path),
    }


FORMAT_WRONG_BODY = "提交格式错误，请严格按照题目要求格式重新提交。"
EVALUATOR_RUNTIME_ERROR_BODY = "评测器运行异常，请稍后重试。"
ALLOWED_CIFAR_FIELDS = ("epochs", "lr", "weight_decay", "batch_size", "momentum")


def extract_variable_star_image_urls(text: str) -> list[str]:
    urls: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.replace("｜", "|").split("|")]
        if not parts:
            continue
        first = parts[0]
        if first.startswith("![](") and first.endswith(")"):
            url = first[4:-1].strip()
            if url:
                urls.append(url)
    return urls


def format_wrong_evaluation(
    *,
    cabinet_source: str,
    reason: str,
    submission_config: dict[str, Any],
    command_executed: str = "",
    stdout_text: str = "",
    stderr_text: str = "",
    exit_code: int | None = None,
    duration_seconds: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Evaluation payload when submission or stdout does not match the cabinet contract; still posted to Arcade."""
    lines = [FORMAT_WRONG_BODY, "", f"原因：{reason}"]
    if submission_config:
        lines.extend(
            [
                "",
                "收到的 JSON：",
                json.dumps(submission_config, ensure_ascii=False, sort_keys=True),
            ]
        )
    append_execution_diagnostics(
        lines,
        command_executed=command_executed,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )
    body = "\n".join(lines)
    result: dict[str, Any] = {
        "passed": False,
        "score": None,
        "feedback": body,
        "outcome": FORMAT_WRONG_BODY,
        "cabinet": cabinet_source,
        "format_error_reason": reason,
        "submission_config": submission_config,
        "command_executed": command_executed.strip() or None,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "stderr_tail": truncate_stderr(stderr_text),
    }
    if stdout_text:
        result["stdout_preview"] = stdout_text[:4000]
    if stderr_text:
        result["stderr_preview"] = truncate_text_preview(stderr_text, tail=True)
    return body, result


def format_evaluator_runtime_error(
    *,
    cabinet_source: str,
    reason: str,
    submission_config: dict[str, Any],
    command_executed: str = "",
    stdout_text: str = "",
    stderr_text: str = "",
    exit_code: int | None = None,
    duration_seconds: float | None = None,
) -> tuple[str, dict[str, Any]]:
    lines = [EVALUATOR_RUNTIME_ERROR_BODY, "", f"原因：{reason}"]
    if submission_config:
        lines.extend(
            [
                "",
                "采用的提交参数：",
                json.dumps(submission_config, ensure_ascii=False, sort_keys=True),
            ]
        )
    append_execution_diagnostics(
        lines,
        command_executed=command_executed,
        exit_code=exit_code,
        duration_seconds=duration_seconds,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
    )
    body = "\n".join(lines)
    result: dict[str, Any] = {
        "passed": False,
        "score": None,
        "feedback": body,
        "outcome": EVALUATOR_RUNTIME_ERROR_BODY,
        "cabinet": cabinet_source,
        "runtime_error_reason": reason,
        "submission_config": submission_config,
        "command_executed": command_executed.strip() or None,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "stderr_tail": truncate_stderr(stderr_text),
    }
    if stdout_text:
        result["stdout_preview"] = stdout_text[:4000]
    if stderr_text:
        result["stderr_preview"] = truncate_text_preview(stderr_text, tail=True)
    return body, result


def extract_cifar_submission_details(config: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    sanitized = {key: config[key] for key in ALLOWED_CIFAR_FIELDS if key in config}
    ignored_fields = sorted(str(key) for key in config.keys() if key not in ALLOWED_CIFAR_FIELDS)
    return sanitized, ignored_fields


def resolve_cabinet_python(cabinet_dir: Path) -> list[str]:
    venv_python = cabinet_dir / ".venv" / "bin" / "python"
    if venv_python.exists():
        return [str(venv_python)]
    if shutil.which("uv"):
        return ["uv", "run", "python"]
    return [sys.executable]


def build_cifar_command(config: dict[str, Any], *, cabinet_dir: Path) -> list[str]:
    try:
        epochs = int(config["epochs"])
        batch_size = int(config["batch_size"])
        lr = float(config["lr"])
        weight_decay = float(config["weight_decay"])
        momentum = float(config["momentum"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid submission config for 101-CIFAR: {config!r}") from exc

    if not (1 <= epochs <= 80):
        raise ValueError(f"epochs must be in [1, 80], got {epochs}")
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size}")
    if lr <= 0:
        raise ValueError(f"lr must be > 0, got {lr}")
    if weight_decay < 0:
        raise ValueError(f"weight_decay must be >= 0, got {weight_decay}")
    if momentum < 0:
        raise ValueError(f"momentum must be >= 0, got {momentum}")

    runner = [*resolve_cabinet_python(cabinet_dir), "train.py"]
    return runner + [
        "--epochs", str(epochs),
        "--lr", str(lr),
        "--weight-decay", str(weight_decay),
        "--batch-size", str(batch_size),
        "--momentum", str(momentum),
    ]


def run_101_cifar(
    item: dict[str, Any],
    *,
    repo_root: Path,
    registry_entry: dict[str, Any],
    timeout: int,
) -> tuple[str, dict[str, Any]]:
    raw_config = parse_submission_config(item)
    config, ignored_fields = extract_cifar_submission_details(raw_config)
    cabinet_source = str(get_cabinet_source(item) or registry_entry.get("source") or "")
    runtime = registry_entry.get("runtime") or {}
    cabinet_dir = repo_root / str(runtime.get("cwd") or "")
    if not cabinet_dir.exists():
        raise FileNotFoundError(f"cabinet directory not found: {cabinet_dir}")

    try:
        command = build_cifar_command(config, cabinet_dir=cabinet_dir)
    except ValueError as exc:
        return format_wrong_evaluation(
            cabinet_source=cabinet_source,
            reason=str(exc),
            submission_config=raw_config,
        )

    start = time.time()
    completed = subprocess.run(
        command,
        cwd=str(cabinet_dir),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    duration = round(time.time() - start, 3)

    stdout_lines = completed.stdout.splitlines()
    line1 = stdout_lines[0].strip() if len(stdout_lines) >= 1 else ""
    line2 = stdout_lines[1].strip() if len(stdout_lines) >= 2 else ""
    line3 = stdout_lines[2].strip() if len(stdout_lines) >= 3 else ""
    protocol_ok = len(stdout_lines) >= 3 and line3 in ("SUCCESS", "ERROR")
    if not protocol_ok:
        return format_evaluator_runtime_error(
            cabinet_source=cabinet_source,
            reason="stdout 不符合约定：须为三行（epoch 列表、test 准确率列表、第三行为 SUCCESS 或 ERROR）",
            submission_config=config,
            command_executed=" ".join(command),
            stdout_text=completed.stdout or "",
            stderr_text=completed.stderr or "",
            exit_code=completed.returncode,
            duration_seconds=duration,
        )

    eval_epochs = parse_csv_ints(line1)
    accuracies = parse_csv_floats(line2)
    success = line3 == "SUCCESS" and completed.returncode == 0
    final_score = accuracies[-1] if accuracies else None

    l0, l1, l2 = (stdout_lines[i].strip() for i in range(3))
    body_lines = [
        f"采用参数：{json.dumps(config, ensure_ascii=False, sort_keys=True)}",
    ]
    if ignored_fields:
        body_lines.append(f"已忽略额外字段：{', '.join(ignored_fields)}")
    body_lines.extend(
        [
            "",
            "训练输出：",
            l0,
            "",
            l1,
            "",
            l2,
        ]
    )
    body = "\n".join(body_lines)

    result = {
        "passed": success,
        "score": final_score,
        "feedback": body,
        "cabinet": cabinet_source,
        "command_executed": " ".join(command),
        "submission_config": raw_config,
        "effective_submission_config": config,
        "ignored_fields": ignored_fields,
        "eval_epochs": eval_epochs,
        "accuracies": accuracies,
        "status_line": line3,
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "stderr_tail": truncate_stderr(completed.stderr),
    }
    return body, result


def run_102_variable_star_relay(
    item: dict[str, Any],
    *,
    repo_root: Path,
    registry_entry: dict[str, Any],
    timeout: int,
) -> tuple[str, dict[str, Any]]:
    submission = get_submission_post(item)
    post_body = str(submission.get("body") or "").strip()
    submission_post_id = str(submission.get("id") or "")
    topic = item.get("topic") or {}
    topic_id = str(topic.get("id") or "")
    cabinet_source = str(get_cabinet_source(item) or registry_entry.get("source") or "")
    runtime = registry_entry.get("runtime") or {}
    cabinet_dir = repo_root / str(runtime.get("cwd") or "")
    if not cabinet_dir.exists():
        raise FileNotFoundError(f"cabinet directory not found: {cabinet_dir}")
    if not post_body:
        return format_wrong_evaluation(
            cabinet_source=cabinet_source,
            reason="帖子正文不能为空。",
            submission_config={},
        )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        submission_path = Path(tmp) / "submission.txt"
        submission_path.write_text(post_body + "\n", encoding="utf-8")
        command = [
            *resolve_cabinet_python(cabinet_dir),
            "evaluate_submission.py",
            "--submission",
            str(submission_path),
        ]
        start = time.time()
        completed = subprocess.run(
            command,
            cwd=str(cabinet_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = round(time.time() - start, 3)

    stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(stdout_lines) < 2 or stdout_lines[-1].strip() != "SUCCESS":
        return format_evaluator_runtime_error(
            cabinet_source=cabinet_source,
            reason="local evaluator stdout 不符合约定：应输出 JSON 结果并以 SUCCESS 结尾。",
            submission_config={},
            command_executed=" ".join(command),
            stdout_text=completed.stdout or "",
            stderr_text=completed.stderr or "",
            exit_code=completed.returncode,
            duration_seconds=duration,
        )

    try:
        payload = json.loads("\n".join(stdout_lines[:-1]))
    except json.JSONDecodeError as exc:
        return format_evaluator_runtime_error(
            cabinet_source=cabinet_source,
            reason=f"local evaluator JSON 解析失败: {exc}",
            submission_config={},
            command_executed=" ".join(command),
            stdout_text=completed.stdout or "",
            stderr_text=completed.stderr or "",
            exit_code=completed.returncode,
            duration_seconds=duration,
        )

    rows = payload.get("rows") or []
    submitted_image_urls = extract_variable_star_image_urls(post_body)
    if isinstance(rows, list):
        normalized_rows: list[dict[str, Any]] = []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            normalized = dict(row)
            if not str(normalized.get("image_url") or "").strip() and idx < len(submitted_image_urls):
                normalized["image_url"] = submitted_image_urls[idx]
            normalized_rows.append(normalized)
        rows = normalized_rows
    coverage = update_variable_star_coverage(
        repo_root=repo_root,
        cabinet_dir=cabinet_dir,
        submission_post_id=submission_post_id,
        topic_id=topic_id,
        rows=rows if isinstance(rows, list) else [],
    )
    summary_lines = [f"总分 {payload.get('raw_points')}/75 ({payload.get('score_100')}/100)"]
    coverage_rows = coverage.get("rows") if isinstance(coverage.get("rows"), list) else []
    for idx, row in enumerate(rows):
        coverage_row = coverage_rows[idx] if idx < len(coverage_rows) else {}
        coverage_label = "首次覆盖" if coverage_row.get("is_new_coverage") else "重复覆盖"
        summary_lines.append(
            " | ".join(
                [
                    f"line {row.get('line')}",
                    "类别正确" if row.get("class_correct") else f"类别错(真值:{row.get('true_class')})",
                    "异常正确" if row.get("anomaly_correct") else f"异常错(真值:{'异常' if row.get('true_anomaly') else '正常'})",
                    coverage_label,
                    f"+{row.get('points')}",
                ]
            )
        )
    if coverage.get("is_replay"):
        summary_lines.append("说明：这条 submission_post 已处理过，覆盖状态未重复累计。")
    elif coverage.get("total_pool"):
        summary_lines.append(
            "覆盖进度 "
            f"{coverage.get('covered_total')}/{coverage.get('total_pool')} "
            f"(本帖新增 {coverage.get('newly_covered_count')}/5, 剩余 {coverage.get('remaining_unseen')})"
        )
    next_batch = coverage.get("next_batch") if isinstance(coverage.get("next_batch"), list) else []
    if next_batch:
        summary_lines.append("下一批建议样本：")
        summary_lines.extend(f"![]({url})" for url in next_batch)
    body = "\n\n".join(summary_lines)
    result = {
        "passed": completed.returncode == 0,
        "score": payload.get("score_100"),
        "feedback": body,
        "cabinet": cabinet_source,
        "raw_points": payload.get("raw_points"),
        "max_raw_points": payload.get("max_raw_points"),
        "rows": rows,
        "coverage": coverage,
        "command_executed": " ".join(command),
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "stderr_tail": truncate_stderr(completed.stderr),
    }
    return body, result


def score_transient_row(row: dict[str, Any], coverage_row: dict[str, Any]) -> tuple[int, list[str]]:
    points = 0
    notes: list[str] = []
    if not row.get("ok"):
        return points, notes
    points += 2
    notes.append("有效格式")
    if coverage_row.get("is_new_coverage"):
        points += 2
        notes.append("首次覆盖")
    if row.get("reason"):
        points += 1
        notes.append("有判读理由")
    tags = row.get("evidence_tags") or []
    if isinstance(tags, list) and tags:
        points += 1
        notes.append("有证据标签")
    role = str(row.get("role") or "")
    try:
        anomaly_score = int(row.get("anomaly_score") or 0)
    except (TypeError, ValueError):
        anomaly_score = 0
    followup = str(row.get("needs_followup") or "")
    if (
        (role in {"interesting", "bridge", "data_issue"} and (followup == "yes" or anomaly_score >= 3))
        or (role in {"typical", "control"} and followup == "no" and anomaly_score <= 2)
        or role == "unsure"
    ):
        points += 1
        notes.append("判断自洽")
    return points, notes


def markdown_cell(value: Any, *, limit: int = 120) -> str:
    text = str(value or "").replace("\n", " ").replace("|", "／").strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def transient_role_label(role: Any) -> str:
    labels = {
        "interesting": "优先回看",
        "bridge": "需要复核",
        "data_issue": "先查质量",
        "typical": "普通样本",
        "control": "对照样本",
        "unsure": "证据不足",
    }
    return labels.get(str(role or ""), str(role or "-"))


def transient_evaluation_table(details: list[dict[str, Any]]) -> list[str]:
    if not details:
        return []
    lines = [
        "",
        "| 行 | 源 | 判读 | 公开计分 | 有效榜暂记 | 复核参照 | 得分依据 | 先验说明 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for detail in details:
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(detail["line"], limit=12),
                    markdown_cell(detail["source_id"], limit=28),
                    markdown_cell(f"{detail['role']}／异常分 {detail['anomaly_score']}", limit=42),
                    markdown_cell(f"+{detail['points']}；{detail['coverage']}", limit=36),
                    markdown_cell(f"+{detail['effective_delta']}；{detail['effective_note']}", limit=42),
                    markdown_cell(detail["prior_label"], limit=46),
                    markdown_cell(detail["notes"], limit=64),
                    markdown_cell(detail["prior_reason"], limit=96),
                ]
            )
            + " |"
        )
    return lines


def load_json_file(path: Path, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(fallback or {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(fallback or {})
    return payload if isinstance(payload, dict) else dict(fallback or {})


def load_transient_prior_labels(cabinet_dir: Path) -> dict[str, Any]:
    payload = load_json_file(cabinet_dir / "prior-labels.json")
    focus = {str(value) for value in payload.get("focus_rendered") or []}
    priority = {str(value) for value in payload.get("priority_candidate") or []}
    manual_sources = payload.get("manual_sources") if isinstance(payload.get("manual_sources"), dict) else {}

    # Local asset folders are optional; they are present in full data deployments
    # and absent in lightweight PR checkouts. Merge them when available.
    for folder_name, target in (("level_scatter", focus), ("sample_shortlist_scatter", priority)):
        folder = cabinet_dir / folder_name
        if not folder.exists():
            continue
        for path in folder.glob("*.png"):
            target.add(source_id_from_filename(path.name))
    return {"focus_rendered": focus, "priority_candidate": priority, "manual_sources": manual_sources}


def source_id_from_filename(name: str) -> str:
    text = str(name or "")
    for suffix in (
        "_sample_review.png",
        "_sample_gp.png",
        "_sample_scatter.png",
        "_level_scatter.png",
        "_scatter.png",
        ".png",
    ):
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return Path(text).stem


def load_transient_feature_cards(cabinet_dir: Path) -> dict[str, dict[str, Any]]:
    payload = load_json_file(cabinet_dir / "feature-cards.json")
    cards = payload.get("cards")
    return cards if isinstance(cards, dict) else {}


def transient_row_source_id(row: dict[str, Any]) -> str:
    for key in ("source_id", "image_key"):
        value = str(row.get(key) or "").strip()
        if value and "/" not in value:
            return value
    image_url = str(row.get("image_url") or "").split("?", 1)[0].rstrip("/")
    return source_id_from_filename(image_url.rsplit("/", 1)[-1])


def transient_participant_followup(row: dict[str, Any]) -> bool:
    try:
        anomaly_score = int(row.get("anomaly_score") or 0)
    except (TypeError, ValueError):
        anomaly_score = 0
    return bool(
        row.get("needs_followup") == "yes"
        or row.get("role") in {"interesting", "bridge", "data_issue"}
        or anomaly_score >= 3
    )


def transient_quality_label(value: Any) -> str:
    return {
        "A_completed_high": "A 高质量",
        "B_completed_good": "B 可用",
        "C_imputed_usable": "C 插补可用",
        "D_low_quality": "D 低质量",
    }.get(str(value or ""), str(value or ""))


def transient_manual_label_text(manual: dict[str, Any], labels: list[str]) -> list[str]:
    raw_text = " ".join(str(value or "") for value in [*labels, manual.get("manual_status"), manual.get("scientific_role"), manual.get("visual_class")]).lower()
    display: list[str] = []
    tier = str(manual.get("tier") or "").strip()
    if tier:
        display.append(f"{tier} 级人工记录")
    if "data_quality" in raw_text or "data_issue" in raw_text or "contaminant" in raw_text:
        display.append("数据质量优先")
    if "core_known_unknown" in raw_text or "science_first_core" in raw_text:
        display.append("主科学候选")
    elif "known_unknown_bridge" in raw_text or "bridge" in raw_text:
        display.append("桥接候选")
    elif "unknown_unknown" in raw_text:
        display.append("未知候选")
    if "interesting" in raw_text:
        display.append("值得回看")
    if "typical" in raw_text and not any(text in display for text in ("主科学候选", "桥接候选", "未知候选")):
        display.append("典型模板")
    if "control" in raw_text:
        display.append("对照样本")
    if "focus_rendered" in raw_text:
        display.append("已渲染复核")
    if "priority_candidate" in raw_text:
        display.append("重点短表")
    if not display:
        display.append("Sample 人工记录")
    deduped: list[str] = []
    for item in display:
        if item not in deduped:
            deduped.append(item)
    return deduped[:4]


def transient_public_note_segments(text: str, *, limit: int = 3) -> list[str]:
    replacements = {
        "unknown-unknown": "未知候选",
        "known-unknown": "已知类型边界候选",
        "known_unknown": "已知类型边界候选",
        "data_issue": "数据质量风险",
        "bridge": "桥接候选",
        "interaction": "相互作用",
        "red plateau": "红色平台",
    }
    segments: list[str] = []
    for raw in re.split(r"[；;]\s*", str(text or "")):
        segment = raw.strip()
        if not segment:
            continue
        # Avoid leaking duplicated English memory snippets into public feedback.
        if "||" in segment:
            segment = segment.split("||", 1)[0].strip()
        for old, new in replacements.items():
            segment = re.sub(re.escape(old), new, segment, flags=re.IGNORECASE)
        segment = re.sub(r"L\d(?:_[A-Za-z0-9]+)+", "该分组", segment)
        segment = re.sub(r"\bGP\b", "趋势拟合", segment)
        segment = re.sub(r"\s+", " ", segment).strip()
        segment = segment.replace("旧候选页", "过去记录")
        segment = segment.replace("候选页", "过去记录")
        segment = segment.replace("查询光变", "当前光变")
        segment = segment.replace("主当前光变", "当前光变")
        segment = segment.replace("特征空间和锚点相似性", "特征分布相似性")
        segment = segment.replace("未知候选 池", "未知候选池")
        segment = segment.replace("已知类型边界候选 桥接候选", "已知类型边界桥接候选")
        segment = segment.replace("数据质量风险 明显", "数据质量风险明显")
        segment = segment.replace("数据质量风险 标记", "数据质量风险标记")
        segment = segment.replace("已有 数据质量风险", "已有数据质量风险")
        segment = segment.replace("趋势拟合 过拟合", "趋势拟合过拟合")
        segment = segment.replace("且 数据", "且数据")
        segment = segment.replace(" 未知候选。", "未知候选。")
        segment = segment.replace("可保留的 已知", "可保留的已知")
        segment = segment.replace("备选 未知", "备选未知")
        segment = segment.replace("未知候选 快速", "未知候选，快速")
        segment = segment.replace("该分组 桥接区", "该分组桥接区")
        chinese_chars = sum(1 for char in segment if "\u4e00" <= char <= "\u9fff")
        if chinese_chars < 4 and len(segment) > 18:
            continue
        if segment not in segments:
            segments.append(segment)
        if len(segments) >= limit:
            break
    return segments


def transient_prior_info(source_id: str, prior_labels: dict[str, Any], feature_cards: dict[str, dict[str, Any]]) -> dict[str, Any]:
    manual_sources = prior_labels.get("manual_sources") if isinstance(prior_labels.get("manual_sources"), dict) else {}
    manual = manual_sources.get(source_id) if isinstance(manual_sources.get(source_id), dict) else None
    in_focus = source_id in prior_labels.get("focus_rendered", set())
    in_priority = source_id in prior_labels.get("priority_candidate", set())
    card = feature_cards.get(source_id) or {}
    labels: list[str] = []
    reasons: list[str] = []
    expected_followup = in_focus or in_priority
    if manual:
        raw_labels = [str(label) for label in manual.get("labels") or [] if str(label or "").strip()]
        labels.extend(transient_manual_label_text(manual, raw_labels))
        reason = str(manual.get("reason") or "").strip()
        check = str(manual.get("check") or "").strip()
        if reason:
            reasons.extend(transient_public_note_segments(reason, limit=2))
        if check:
            check_segments = transient_public_note_segments(check, limit=1)
            if check_segments:
                reasons.append("复核要点：" + check_segments[0])
        label_text = " ".join(raw_labels).lower()
        expected_followup = not any(token in label_text for token in ("typical", "control", "ordinary"))
        if any(token in label_text for token in ("interesting", "bridge", "data_issue", "manual_recheck", "known_unknown", "priority", "focus")):
            expected_followup = True
        if not labels:
            labels.append("Sample 人工记录")
    if in_focus:
        labels.append("人工复核池")
    if in_priority:
        labels.append("重点短表")
    if not labels:
        labels.append("普通池")
    deduped_labels: list[str] = []
    for label in labels:
        if label not in deduped_labels:
            deduped_labels.append(label)
    labels = deduped_labels[:4]

    if in_focus:
        reasons.append("过去已进入人工复核渲染池")
    if in_priority:
        reasons.append("过去已进入重点候选短表")
    quality_tier = card.get("quality_tier")
    if quality_tier:
        reasons.append(f"特征质量 {transient_quality_label(quality_tier)}")
    for label, key in (
        ("再亮计数", "rebrightening_completed"),
        ("振幅", "amplitude_completed"),
        ("峰值SNR", "peak_snr"),
    ):
        value = card.get(key)
        if value not in (None, "", "0", 0):
            reasons.append(f"{label}≈{value}")
    if card.get("agn_evidence") or card.get("wise_agn"):
        reasons.append("有核区/AGN上下文")
    if card.get("gaia_is_stellar") or card.get("var_evidence"):
        reasons.append("有恒星/变源上下文")
    if not reasons:
        reasons.append("未命中过去重点池")

    return {
        "labels": labels,
        "in_manual": bool(manual),
        "in_prior": bool(manual) or in_focus or in_priority,
        "expected_followup": expected_followup,
        "reason": "；".join(reasons[:4]),
    }


def run_103_transient_anomaly_relay(
    item: dict[str, Any],
    *,
    repo_root: Path,
    registry_entry: dict[str, Any],
    timeout: int,
) -> tuple[str, dict[str, Any]]:
    submission = get_submission_post(item)
    post_body = str(submission.get("body") or "").strip()
    submission_post_id = str(submission.get("id") or "")
    topic = item.get("topic") or {}
    topic_id = str(topic.get("id") or "")
    cabinet_source = str(get_cabinet_source(item) or registry_entry.get("source") or "")
    runtime = registry_entry.get("runtime") or {}
    cabinet_dir = repo_root / str(runtime.get("cwd") or "")
    if not cabinet_dir.exists():
        raise FileNotFoundError(f"cabinet directory not found: {cabinet_dir}")
    if not post_body:
        return format_wrong_evaluation(
            cabinet_source=cabinet_source,
            reason="帖子正文不能为空。",
            submission_config={},
        )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        submission_path = Path(tmp) / "submission.txt"
        submission_path.write_text(post_body + "\n", encoding="utf-8")
        command = [
            *resolve_cabinet_python(cabinet_dir),
            "evaluate_submission.py",
            "--submission",
            str(submission_path),
        ]
        start = time.time()
        completed = subprocess.run(
            command,
            cwd=str(cabinet_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        duration = round(time.time() - start, 3)

    stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(stdout_lines) < 2 or stdout_lines[-1].strip() != "SUCCESS":
        return format_evaluator_runtime_error(
            cabinet_source=cabinet_source,
            reason="local evaluator stdout 不符合约定：应输出 JSON 结果并以 SUCCESS 结尾。",
            submission_config={},
            command_executed=" ".join(command),
            stdout_text=completed.stdout or "",
            stderr_text=completed.stderr or "",
            exit_code=completed.returncode,
            duration_seconds=duration,
        )

    try:
        payload = json.loads("\n".join(stdout_lines[:-1]))
    except json.JSONDecodeError as exc:
        return format_evaluator_runtime_error(
            cabinet_source=cabinet_source,
            reason=f"local evaluator JSON 解析失败: {exc}",
            submission_config={},
            command_executed=" ".join(command),
            stdout_text=completed.stdout or "",
            stderr_text=completed.stderr or "",
            exit_code=completed.returncode,
            duration_seconds=duration,
        )

    rows = payload.get("results") or []
    if not isinstance(rows, list):
        rows = []
    coverage = update_transient_relay_coverage(
        repo_root=repo_root,
        cabinet_dir=cabinet_dir,
        submission_post_id=submission_post_id,
        topic_id=topic_id,
        rows=rows,
    )
    coverage_rows = coverage.get("rows") if isinstance(coverage.get("rows"), list) else []
    prior_labels = load_transient_prior_labels(cabinet_dir)
    feature_cards = load_transient_feature_cards(cabinet_dir)
    raw_points = 0
    max_raw_points = max(len(rows), 5) * 7
    effective_points = 0
    max_effective_points = max(len(rows), 5) * 2
    followup_sources: list[str] = []
    prior_hits: list[dict[str, Any]] = []
    manual_hits: list[dict[str, Any]] = []
    ordinary_matches: list[str] = []
    detail_rows: list[dict[str, Any]] = []
    if payload.get("ok"):
        accepted_line = f"已接收 {payload.get('line_count')}/5 行结构化判读。"
    else:
        accepted_line = "提交没有通过格式校验，请按题目要求重交 5 行。"
    for idx, row in enumerate(rows):
        coverage_row = coverage_rows[idx] if idx < len(coverage_rows) else {}
        points, notes = score_transient_row(row, coverage_row)
        raw_points += points
        coverage_label = "首次覆盖" if coverage_row.get("is_new_coverage") else "重复覆盖"
        errors = row.get("errors") if isinstance(row.get("errors"), list) else []
        source_id = transient_row_source_id(row)
        followup = transient_participant_followup(row) if row.get("ok") else False
        prior = transient_prior_info(source_id, prior_labels, feature_cards)
        if row.get("ok") and followup:
            followup_sources.append(source_id)
        effective_note = "待专家复核"
        effective_delta = 0
        expected_followup = bool(prior.get("expected_followup"))
        if row.get("ok") and prior["in_prior"] and followup == expected_followup:
            effective_delta = 2
            effective_note = "匹配 Sample 记录" if prior.get("in_manual") else "命中过去重点"
            prior_hits.append({"source_id": source_id, "prior": prior})
            if prior.get("in_manual"):
                manual_hits.append({"source_id": source_id, "prior": prior})
        elif row.get("ok") and not followup and not prior["in_prior"]:
            effective_delta = 2
            effective_note = "普通判断一致"
            ordinary_matches.append(source_id)
        elif row.get("ok") and followup:
            effective_note = "新增候选，待复核"
        elif row.get("ok") and prior["in_prior"]:
            effective_note = "与 Sample 记录不一致"
        effective_points += effective_delta
        detail_rows.append(
            {
                "line": row.get("line"),
                "source_id": source_id or "-",
                "status": "有效" if row.get("ok") else "未通过",
                "coverage": coverage_label,
                "role": transient_role_label(row.get("role")),
                "anomaly_score": row.get("anomaly_score"),
                "points": points,
                "effective_delta": effective_delta,
                "effective_note": effective_note,
                "prior_label": "、".join(prior["labels"]),
                "prior_reason": prior["reason"],
                "notes": "、".join(notes) if notes else ("；".join(str(err) for err in errors) if errors else "-"),
            }
        )
    score_100 = round(raw_points / max_raw_points * 100, 1) if max_raw_points else 0
    effective_score_100 = round(effective_points / max_effective_points * 100, 1) if max_effective_points else 0
    summary_lines = [
        "### 本轮评测",
        "",
        "| 项目 | 结果 |",
        "| --- | --- |",
        f"| 结构化行数 | {markdown_cell(accepted_line, limit=90)} |",
        f"| 公开分 | {raw_points}/{max_raw_points}，计入即时参与榜 |",
        f"| 有效榜暂记 | {effective_points}/{max_effective_points}，专家定期复核后更新 |",
        f"| 建议回看 | {len(followup_sources)} 张"
        + (f"：{markdown_cell(', '.join(followup_sources[:8]), limit=80)} |" if followup_sources else " |"),
        f"| Sample 交叉 | 匹配人工记录 {len(manual_hits)} 张；命中过去重点 {len(prior_hits)} 张；普通一致 {len(ordinary_matches)} 张 |",
        "",
        "评测说明：本回复由规则评测器生成，只检查提交格式、覆盖状态、判读自洽性和 Sample 记录交叉；不调用大模型。",
    ]
    summary_lines.extend(transient_evaluation_table(detail_rows))
    if coverage.get("is_replay"):
        summary_lines.append("")
        summary_lines.append("说明：这条 submission_post 已处理过，覆盖状态未重复累计。")
    elif coverage.get("total_pool"):
        summary_lines.append("")
        summary_lines.append(
            "覆盖进度："
            f"{coverage.get('covered_total')}/{coverage.get('total_pool')} "
            f"(本帖新增 {coverage.get('newly_covered_count')}/5, 剩余 {coverage.get('remaining_unseen')})"
        )
    next_batch = coverage.get("next_batch") if isinstance(coverage.get("next_batch"), list) else []
    if next_batch:
        summary_lines.append("下一批建议样本：")
        summary_lines.extend(f"![]({url})" for url in next_batch)

    body = "\n".join(summary_lines)
    result = {
        "passed": bool(payload.get("ok")) and completed.returncode == 0,
        "score": score_100,
        "feedback": body,
        "cabinet": cabinet_source,
        "raw_points": raw_points,
        "max_raw_points": max_raw_points,
        "effective_points": effective_points,
        "max_effective_points": max_effective_points,
        "effective_score_100": effective_score_100,
        "rows": rows,
        "coverage": coverage,
        "command_executed": " ".join(command),
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "stderr_tail": truncate_stderr(completed.stderr),
    }
    return body, result


BUILTIN_RUNNERS = {
    "builtin:101-cifar": run_101_cifar,
    "builtin:102-variable-star-relay": run_102_variable_star_relay,
    "builtin:103-transient-anomaly-relay": run_103_transient_anomaly_relay,
}


def build_setup_shell_command(setup_commands: list[str]) -> list[str]:
    shell_path = shutil.which("zsh") or shutil.which("bash") or "/bin/sh"
    shell_flag = "-lc" if shell_path.endswith(("zsh", "bash")) else "-c"
    script_lines = [
        'export PATH="$HOME/.local/bin:$PATH"',
        "set -e",
        *setup_commands,
    ]
    return [shell_path, shell_flag, "\n".join(script_lines)]


def ensure_setup_commands(
    *,
    repo_root: Path,
    registry_entry: dict[str, Any],
    cabinet_source: str,
    timeout: int,
) -> tuple[str, dict[str, Any]] | None:
    setup_commands = registry_entry.get("setup_commands") or []
    if not setup_commands:
        return None
    setup_key = (repo_root.resolve(), cabinet_source)
    with _setup_lock:
        if setup_key in _completed_setups:
            log(f"setup skipped: source={cabinet_source} status=already-complete")
            return None
        commands = [str(raw_command or "").strip() for raw_command in setup_commands]
        commands = [command for command in commands if command]
        if not commands:
            return None
        command = build_setup_shell_command(commands)
        log(f"setup started: source={cabinet_source} command={log_preview(shlex.join(command), max_chars=240)}")
        started_at = time.time()
        completed = subprocess.run(
            command,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = round(time.time() - started_at, 3)
        if completed.returncode != 0:
            stderr_tail = truncate_stderr(completed.stderr)
            stderr_summary = stderr_tail[-1] if stderr_tail else ""
            log(
                "setup failed: "
                f"source={cabinet_source} exit_code={completed.returncode} duration={duration} "
                f"command={log_preview(shlex.join(command), max_chars=200)} "
                f"stderr_tail={log_preview(stderr_summary, max_chars=200)}"
            )
            return format_evaluator_runtime_error(
                cabinet_source=cabinet_source,
                reason="setup commands failed",
                submission_config={},
                command_executed=shlex.join(command),
                stdout_text=completed.stdout or "",
                stderr_text=completed.stderr or "",
                exit_code=completed.returncode,
                duration_seconds=duration,
            )
        log(f"setup completed: source={cabinet_source} duration={duration}")
        _completed_setups.add(setup_key)
    return None


def evaluate_item(
    item: dict[str, Any],
    *,
    repo_root: Path,
    registry: dict[str, dict[str, Any]],
    timeout: int,
) -> tuple[str, dict[str, Any]] | None:
    source = get_cabinet_source(item)
    if not source:
        return None

    registry_entry = registry.get(source)
    if registry_entry is None:
        return None

    runtime = registry_entry.get("runtime") or {}
    runner_name = str(runtime.get("runner") or "").strip()
    runner = BUILTIN_RUNNERS.get(runner_name)
    if runner is None:
        raise ValueError(f"unsupported runner {runner_name!r} for cabinet {source!r}")

    effective_timeout = int(runtime.get("timeout_seconds") or timeout)
    runner_entry = dict(registry_entry)
    runner_entry["source"] = source
    log(
        "dispatch runner: "
        f"{format_item_log_context(item)} runner={runner_name} timeout={effective_timeout}"
    )
    setup_error = ensure_setup_commands(
        repo_root=repo_root,
        registry_entry=runner_entry,
        cabinet_source=source,
        timeout=effective_timeout,
    )
    if setup_error is not None:
        return setup_error
    return runner(item, repo_root=repo_root, registry_entry=runner_entry, timeout=effective_timeout)


def process_item(
    item: dict[str, Any],
    *,
    base_url: str,
    secret_key: str,
    repo_root: Path,
    registry: dict[str, dict[str, Any]],
    timeout: int,
    dry_run: bool,
) -> bool:
    topic = item.get("topic") or {}
    submission = get_submission_post(item)
    topic_id = str(topic.get("id") or "")
    branch_root_post_id = str(item.get("branch_root_post_id") or "")
    submission_post_id = str(submission.get("id") or "")
    title = str(topic.get("title") or "<untitled>")
    source = get_cabinet_source(item) or "<unknown-source>"
    if not topic_id or not branch_root_post_id or not submission_post_id:
        log(f"skip malformed queue item: {format_item_log_context(item)}")
        return False

    log(f"start evaluation: {format_item_log_context(item)}")
    evaluation = evaluate_item(item, repo_root=repo_root, registry=registry, timeout=timeout)
    if evaluation is None:
        log(f"skip unsupported task: {format_item_log_context(item)}")
        return False

    body, result = evaluation
    log(f"evaluation completed: {format_item_log_context(item)} {format_result_log_summary(result)}")
    if dry_run:
        log(f"dry-run: would post evaluation: {format_item_log_context(item)}")
        return True

    log(f"posting evaluation: {format_item_log_context(item)} score={result.get('score')!r} passed={result.get('passed')!r}")
    post_evaluation(
        base_url=base_url,
        secret_key=secret_key,
        topic_id=topic_id,
        branch_root_post_id=branch_root_post_id,
        for_post_id=submission_post_id,
        body=body,
        result=result,
    )
    log(f"posted evaluation: {format_item_log_context(item)} score={result.get('score')!r} passed={result.get('passed')!r}")
    return True


def process_item_safe(
    item: dict[str, Any],
    *,
    base_url: str,
    secret_key: str,
    repo_root: Path,
    registry: dict[str, dict[str, Any]],
    timeout: int,
    dry_run: bool,
) -> bool:
    try:
        return process_item(
            item,
            base_url=base_url,
            secret_key=secret_key,
            repo_root=repo_root,
            registry=registry,
            timeout=timeout,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"evaluation failed: {format_item_log_context(item)} error={log_preview(exc, max_chars=240)}")
        return False


def run_once(args: argparse.Namespace, *, registry: dict[str, dict[str, Any]]) -> int:
    secret_key = require_secret(args.secret_key)
    repo_root = Path(args.repo_root).resolve()
    items = fetch_review_queue(
        base_url=args.base_url,
        secret_key=secret_key,
        topic_id=args.topic_id,
        limit=args.limit,
    )
    log(f"fetched queue: items={len(items)} topic_id={args.topic_id or '-'} limit={args.limit}")
    if not items:
        log("queue is empty")
        return 0

    max_workers = max(1, args.max_concurrent)
    pool = min(max_workers, len(items))
    processed = 0
    with ThreadPoolExecutor(max_workers=pool) as executor:
        futures = [
            executor.submit(
                process_item_safe,
                item,
                base_url=args.base_url,
                secret_key=secret_key,
                repo_root=repo_root,
                registry=registry,
                timeout=args.timeout,
                dry_run=args.dry_run,
            )
            for item in items
        ]
        for future in as_completed(futures):
            if future.result():
                processed += 1
    log(f"done: processed={processed} total_items={len(items)} max_concurrent={max_workers}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    repo_root = Path(args.repo_root).resolve()
    log_dir = Path(args.log_dir).resolve() if str(args.log_dir).strip() else repo_root / "logs"
    registry_path = Path(args.registry_path)
    if not registry_path.is_absolute():
        registry_path = repo_root / registry_path
    try:
        registry = load_reviewer_registry(registry_path)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"failed to load reviewer registry {registry_path}: {exc}") from exc
    configure_log_dir(log_dir)
    atexit.register(_close_daily_log_file)
    registry = filter_registry_for_deployment_profile(registry, args.deployment_profile)
    log(
        "loaded reviewer registry: "
        f"profile={args.deployment_profile or DEFAULT_DEPLOYMENT_PROFILE} enabled_cabinets={len(registry)}"
    )

    if args.loop and args.once:
        raise SystemExit("Use either --once or --loop, not both.")
    if not args.loop:
        args.once = True

    if args.once:
        return run_once(args, registry=registry)

    while True:
        try:
            run_once(args, registry=registry)
        except KeyboardInterrupt:
            log("stopped")
            return 130
        except Exception as exc:  # noqa: BLE001
            log(f"poll failed: {exc}")
        time.sleep(max(1, args.poll_interval))


if __name__ == "__main__":
    raise SystemExit(main())
