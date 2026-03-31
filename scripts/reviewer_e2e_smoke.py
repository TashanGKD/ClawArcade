#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = "generated/reviewer_registry.json"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_SOURCE = "cabinets/citizen-science-harbor/102-variable-star-citizen-science"
DEFAULT_SUBMISSION_FILE = "forum_post_template.txt"
DEFAULT_EXPECTED_MIN_SCORE = 100.0


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeTopicLabServer(ThreadingHTTPServer):
    def __init__(self, server_address, request_handler_class, *, queue_items: list[dict[str, object]], expected_secret: str):
        super().__init__(server_address, request_handler_class)
        self.queue_items = queue_items
        self.expected_secret = expected_secret
        self.evaluations: list[dict[str, object]] = []


class FakeTopicLabHandler(BaseHTTPRequestHandler):
    server: FakeTopicLabServer

    def _json_response(self, payload: dict[str, object], status: int = 200) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _require_secret(self) -> bool:
        secret = self.headers.get("X-Arcade-Secret-Key", "")
        if secret != self.server.expected_secret:
            self._json_response({"error": "unauthorized"}, status=401)
            return False
        return True

    def do_GET(self) -> None:
        if not self._require_secret():
            return
        parsed = urlparse(self.path)
        if parsed.path != "/api/v1/internal/arcade/review-queue":
            self._json_response({"error": "not found"}, status=404)
            return
        params = parse_qs(parsed.query)
        limit = int(params.get("limit", ["20"])[0])
        self._json_response({"items": self.server.queue_items[:limit]})

    def do_POST(self) -> None:
        if not self._require_secret():
            return
        if not self.path.endswith("/evaluate"):
            self._json_response({"error": "not found"}, status=404)
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        payload = json.loads(body)
        self.server.evaluations.append({"path": self.path, "payload": payload})
        self._json_response({"ok": True})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def start_server(*, queue_items: list[dict[str, object]], expected_secret: str) -> tuple[FakeTopicLabServer, str]:
    server = FakeTopicLabServer(("127.0.0.1", 0), FakeTopicLabHandler, queue_items=queue_items, expected_secret=expected_secret)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def build_queue_item(*, source: str, title: str, submission_body: str) -> dict[str, object]:
    return {
        "topic": {
            "id": "smoke-topic-1",
            "title": title,
            "metadata": {
                "arcade": {
                    "validator": {
                        "config": {
                            "source": source,
                        }
                    }
                }
            },
        },
        "branch_root_post_id": "smoke-branch-root-1",
        "submission_post": {
            "id": "smoke-submission-1",
            "body": submission_body,
        },
    }


def run_reviewer_once(
    *,
    reviewer_script: Path,
    repo_root: Path,
    registry_path: Path,
    base_url: str,
    secret_key: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    return subprocess.run(
        [
            sys.executable,
            str(reviewer_script),
            "--once",
            "--repo-root",
            str(repo_root),
            "--registry-path",
            str(registry_path),
            "--base-url",
            base_url,
            "--secret-key",
            secret_key,
            "--timeout",
            str(timeout),
            "--max-concurrent",
            "1",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a fake TopicLab end-to-end smoke test for arcade_reviewer.py.")
    parser.add_argument("--repo-root", default=str(REPO_ROOT), help="ClawArcade repository root")
    parser.add_argument("--registry-path", default=DEFAULT_REGISTRY_PATH, help="Reviewer registry path, relative to repo root by default")
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="Cabinet source to submit to the fake queue")
    parser.add_argument("--submission-file", default=DEFAULT_SUBMISSION_FILE, help="Submission file inside the cabinet runtime cwd")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Reviewer timeout in seconds")
    parser.add_argument("--expected-min-score", type=float, default=DEFAULT_EXPECTED_MIN_SCORE, help="Minimum score expected in the posted evaluation result")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo_root = Path(args.repo_root).resolve()
    registry_path = Path(args.registry_path)
    if not registry_path.is_absolute():
        registry_path = repo_root / registry_path

    reviewer = load_module("clawarcade_reviewer_e2e_runtime", repo_root / "arcade_reviewer.py")
    reviewer._close_daily_log_file()
    registry = reviewer.load_reviewer_registry(registry_path)
    entry = registry.get(args.source)
    if entry is None:
        raise SystemExit(f"missing registry entry for {args.source}")

    runtime = entry.get("runtime") or {}
    cabinet_dir = repo_root / str(runtime.get("cwd") or "")
    submission_path = cabinet_dir / args.submission_file
    if not submission_path.exists():
        raise SystemExit(f"submission file not found: {submission_path}")

    submission_body = submission_path.read_text(encoding="utf-8").strip()
    title = str(entry.get("cabinet_title") or args.source)
    queue_item = build_queue_item(source=args.source, title=title, submission_body=submission_body)
    secret = "reviewer-e2e-smoke-secret"
    server, base_url = start_server(queue_items=[queue_item], expected_secret=secret)

    try:
        completed = run_reviewer_once(
            reviewer_script=repo_root / "arcade_reviewer.py",
            repo_root=repo_root,
            registry_path=registry_path,
            base_url=base_url,
            secret_key=secret,
            timeout=max(1, int(args.timeout)),
        )
        print("[reviewer-e2e] reviewer stdout:")
        print(completed.stdout.rstrip() or "-")
        if completed.stderr.strip():
            print("[reviewer-e2e] reviewer stderr:")
            print(completed.stderr.rstrip())

        if completed.returncode != 0:
            raise SystemExit(f"arcade_reviewer.py exited with code {completed.returncode}")
        if len(server.evaluations) != 1:
            raise SystemExit(f"expected exactly one evaluation post, got {len(server.evaluations)}")

        evaluation = server.evaluations[0]["payload"]
        result = evaluation.get("result") or {}
        score = result.get("score")
        if evaluation.get("for_post_id") != "smoke-submission-1":
            raise SystemExit("posted evaluation for unexpected submission id")
        if result.get("cabinet") != args.source:
            raise SystemExit(f"posted evaluation for unexpected cabinet: {result.get('cabinet')!r}")
        if not isinstance(score, (int, float)) or float(score) < float(args.expected_min_score):
            raise SystemExit(f"posted evaluation score {score!r} is below expected minimum {args.expected_min_score}")
        print(
            "[reviewer-e2e] pass "
            f"source={args.source} score={score} passed={result.get('passed')!r} "
            f"for_post_id={evaluation.get('for_post_id')}"
        )
        return 0
    finally:
        server.shutdown()
        server.server_close()
        reviewer._close_daily_log_file()


if __name__ == "__main__":
    raise SystemExit(main())
