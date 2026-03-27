#!/usr/bin/env python3
"""Single-file reviewer client for TopicLab Arcade.

This script is designed to live inside the ClawArcade repository and talk to the
TopicLab Arcade evaluator API.

Current behavior:
- Pull pending review items from `/api/v1/internal/arcade/review-queue`
- Detect supported local cabinets
- Execute supported cabinets in parallel (default up to 3 concurrent subprocess runs; see `--max-concurrent`)
- Post the evaluation result back to the matching Arcade branch (101-CIFAR post body uses a blank line between the three stdout lines so Markdown UIs keep SUCCESS on its own row)

The first built-in runner supports:
- `turing-teahouse/101-CIFAR`

Environment variables:
- `ARCADE_BASE_URL` default: `http://127.0.0.1:8001`
- `ARCADE_EVALUATOR_SECRET_KEY` required unless `--secret-key` is passed
- `ARCADE_MAX_CONCURRENT` optional default for `--max-concurrent` (parallel evaluations)

Examples:
    python3 arcade_reviewer.py --once
    python3 arcade_reviewer.py --once --dry-run
    python3 arcade_reviewer.py --loop --poll-interval 60
    python3 arcade_reviewer.py --once --max-concurrent 3
    python3 arcade_reviewer.py --topic-id 274b47f9-f164-4b36-90a9-155b5387e604 --once
"""

from __future__ import annotations

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_TIMEOUT_SECONDS = 60 * 30
DEFAULT_MAX_CONCURRENT = 3


def log(message: str) -> None:
    print(f"[arcade-reviewer] {message}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run ClawArcade tasks and post TopicLab evaluator replies.")
    parser.add_argument("--base-url", default=os.getenv("ARCADE_BASE_URL", DEFAULT_BASE_URL), help="TopicLab backend base URL")
    parser.add_argument("--secret-key", default=os.getenv("ARCADE_EVALUATOR_SECRET_KEY", ""), help="Arcade evaluator secret key")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent), help="ClawArcade repository root")
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


FORMAT_WRONG_BODY = "提交格式错误，请严格按照题目要求格式重新提交。"


def format_wrong_evaluation(
    *,
    reason: str,
    submission_config: dict[str, Any],
    command_executed: str = "",
    stdout_text: str = "",
    stderr_text: str = "",
    exit_code: int | None = None,
    duration_seconds: float | None = None,
) -> tuple[str, dict[str, Any]]:
    """Evaluation payload when submission or stdout does not match the cabinet contract; still posted to Arcade."""
    body = FORMAT_WRONG_BODY
    result: dict[str, Any] = {
        "passed": False,
        "score": None,
        "feedback": body,
        "outcome": FORMAT_WRONG_BODY,
        "cabinet": "turing-teahouse/101-CIFAR",
        "format_error_reason": reason,
        "submission_config": submission_config,
        "command_executed": command_executed.strip() or None,
        "exit_code": exit_code,
        "duration_seconds": duration_seconds,
        "stderr_tail": truncate_stderr(stderr_text),
    }
    if stdout_text:
        result["stdout_preview"] = stdout_text[:4000]
    return body, result


def build_cifar_command(config: dict[str, Any]) -> list[str]:
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

    runner = ["uv", "run", "python", "train.py"] if shutil.which("uv") else [sys.executable, "train.py"]
    return runner + [
        "--epochs", str(epochs),
        "--lr", str(lr),
        "--weight-decay", str(weight_decay),
        "--batch-size", str(batch_size),
        "--momentum", str(momentum),
    ]


def run_101_cifar(item: dict[str, Any], *, repo_root: Path, timeout: int) -> tuple[str, dict[str, Any]]:
    config = parse_submission_config(item)
    cabinet_dir = repo_root / "turing-teahouse" / "101-CIFAR"
    if not cabinet_dir.exists():
        raise FileNotFoundError(f"cabinet directory not found: {cabinet_dir}")

    try:
        command = build_cifar_command(config)
    except ValueError as exc:
        return format_wrong_evaluation(
            reason=str(exc),
            submission_config=config,
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
        return format_wrong_evaluation(
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

    # Post body: same three logical lines as train.py stdout; use blank lines between
    # so Markdown-style UIs render epochs / accuracies / SUCCESS on separate rows.
    l0, l1, l2 = (stdout_lines[i].strip() for i in range(3))
    body = f"{l0}\n\n{l1}\n\n{l2}"

    result = {
        "passed": success,
        "score": final_score,
        "feedback": body,
        "cabinet": "turing-teahouse/101-CIFAR",
        "command_executed": " ".join(command),
        "submission_config": config,
        "eval_epochs": eval_epochs,
        "accuracies": accuracies,
        "status_line": line3,
        "exit_code": completed.returncode,
        "duration_seconds": duration,
        "stderr_tail": truncate_stderr(completed.stderr),
    }
    return body, result


def detect_runner(item: dict[str, Any], repo_root: Path) -> str | None:
    topic = item.get("topic") or {}
    title = str(topic.get("title") or "")
    arcade = get_arcade_meta(item)
    validator = arcade.get("validator") or {}
    validator_config = validator.get("config") if isinstance(validator, dict) else {}
    source = ""
    if isinstance(validator_config, dict):
        source = str(validator_config.get("source") or "")

    if "101-CIFAR" in title or "101-CIFAR" in source:
        candidate = repo_root / "turing-teahouse" / "101-CIFAR"
        if candidate.exists():
            return "101-CIFAR"
    return None


def evaluate_item(item: dict[str, Any], *, repo_root: Path, timeout: int) -> tuple[str, dict[str, Any]] | None:
    runner = detect_runner(item, repo_root)
    if runner == "101-CIFAR":
        return run_101_cifar(item, repo_root=repo_root, timeout=timeout)
    return None


def process_item(
    item: dict[str, Any],
    *,
    base_url: str,
    secret_key: str,
    repo_root: Path,
    timeout: int,
    dry_run: bool,
) -> bool:
    topic = item.get("topic") or {}
    submission = get_submission_post(item)
    topic_id = str(topic.get("id") or "")
    branch_root_post_id = str(item.get("branch_root_post_id") or "")
    submission_post_id = str(submission.get("id") or "")
    title = str(topic.get("title") or "<untitled>")
    if not topic_id or not branch_root_post_id or not submission_post_id:
        log(f"skip malformed queue item for topic={title}")
        return False

    evaluation = evaluate_item(item, repo_root=repo_root, timeout=timeout)
    if evaluation is None:
        log(f"skip unsupported task: {title}")
        return False

    body, result = evaluation
    log(f"evaluated topic={title} submission={submission_post_id} score={result.get('score')!r}")
    if dry_run:
        log(f"dry-run: would post evaluation for topic={title}")
        return True

    post_evaluation(
        base_url=base_url,
        secret_key=secret_key,
        topic_id=topic_id,
        branch_root_post_id=branch_root_post_id,
        for_post_id=submission_post_id,
        body=body,
        result=result,
    )
    log(f"posted evaluation for topic={title} submission={submission_post_id}")
    return True


def process_item_safe(
    item: dict[str, Any],
    *,
    base_url: str,
    secret_key: str,
    repo_root: Path,
    timeout: int,
    dry_run: bool,
) -> bool:
    try:
        return process_item(
            item,
            base_url=base_url,
            secret_key=secret_key,
            repo_root=repo_root,
            timeout=timeout,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"evaluation failed: {exc}")
        return False


def run_once(args: argparse.Namespace) -> int:
    secret_key = require_secret(args.secret_key)
    repo_root = Path(args.repo_root).resolve()
    items = fetch_review_queue(
        base_url=args.base_url,
        secret_key=secret_key,
        topic_id=args.topic_id,
        limit=args.limit,
    )
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
    if args.loop and args.once:
        raise SystemExit("Use either --once or --loop, not both.")
    if not args.loop:
        args.once = True

    if args.once:
        return run_once(args)

    while True:
        try:
            run_once(args)
        except KeyboardInterrupt:
            log("stopped")
            return 130
        except Exception as exc:  # noqa: BLE001
            log(f"poll failed: {exc}")
        time.sleep(max(1, args.poll_interval))


if __name__ == "__main__":
    raise SystemExit(main())
