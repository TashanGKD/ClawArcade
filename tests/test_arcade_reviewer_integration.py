from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
REVIEWER_SCRIPT = REPO_ROOT / "arcade_reviewer.py"


class FakeTopicLabServer(ThreadingHTTPServer):
    def __init__(self, server_address, request_handler_class, *, queue_items, expected_secret):
        super().__init__(server_address, request_handler_class)
        self.queue_items = queue_items
        self.expected_secret = expected_secret
        self.evaluations: list[dict[str, object]] = []
        self.requests: list[tuple[str, str]] = []


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
        self.server.requests.append(("GET", self.path))
        if not self._require_secret():
            return

        parsed = urlparse(self.path)
        if parsed.path != "/api/v1/internal/arcade/review-queue":
            self._json_response({"error": "not found"}, status=404)
            return

        params = parse_qs(parsed.query)
        limit = int(params.get("limit", ["20"])[0])
        items = self.server.queue_items[:limit]
        self._json_response({"items": items})

    def do_POST(self) -> None:
        self.server.requests.append(("POST", self.path))
        if not self._require_secret():
            return

        if not self.path.endswith("/evaluate"):
            self._json_response({"error": "not found"}, status=404)
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        payload = json.loads(body)
        self.server.evaluations.append(
            {
                "path": self.path,
                "payload": payload,
            }
        )
        self._json_response({"ok": True})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class ArcadeReviewerIntegrationTests(unittest.TestCase):
    def start_server(self, *, queue_items, expected_secret: str) -> tuple[FakeTopicLabServer, str]:
        server = FakeTopicLabServer(("127.0.0.1", 0), FakeTopicLabHandler, queue_items=queue_items, expected_secret=expected_secret)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return server, f"http://127.0.0.1:{server.server_port}"

    def write_registry(self, root: Path, cabinets: dict[str, dict[str, object]]) -> Path:
        registry_path = root / "generated" / "reviewer_registry.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            json.dumps({"schema_version": 1, "cabinets": cabinets}, indent=2),
            encoding="utf-8",
        )
        return registry_path

    def write_cifar_runner(self, root: Path) -> None:
        cabinet_dir = root / "cabinets" / "turing-teahouse" / "101-CIFAR"
        cabinet_dir.mkdir(parents=True, exist_ok=True)
        (cabinet_dir / "train.py").write_text(
            "import sys\n"
            "print('1,10')\n"
            "print('0.1111,0.2222')\n"
            "print('SUCCESS')\n"
            "print('INFO: fake run', file=sys.stderr)\n",
            encoding="utf-8",
        )

    def write_variable_star_runner(self, root: Path) -> None:
        cabinet_dir = root / "cabinets" / "citizen-science-harbor" / "102-variable-star-citizen-science"
        cabinet_dir.mkdir(parents=True, exist_ok=True)
        data_dir = cabinet_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "manifest.json").write_text(
            json.dumps(
                [
                    {"image_url": "https://example.com/a.png"},
                    {"image_url": "https://example.com/b.png"},
                    {"image_url": "https://example.com/c.png"},
                    {"image_url": "https://example.com/d.png"},
                    {"image_url": "https://example.com/e.png"},
                    {"image_url": "https://example.com/f.png"},
                    {"image_url": "https://example.com/g.png"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (cabinet_dir / "evaluate_submission.py").write_text(
            "import json\n"
            "print(json.dumps({\n"
            "  'raw_points': 75,\n"
            "  'score_100': 100.0,\n"
            "  'max_raw_points': 75,\n"
            "  'rows': [\n"
            "    {'line': 1, 'class_correct': True, 'anomaly_correct': True, 'true_class': 'CV', 'true_anomaly': False, 'points': 15},\n"
            "    {'line': 2, 'class_correct': True, 'anomaly_correct': True, 'true_class': 'YSO', 'true_anomaly': False, 'points': 15},\n"
            "    {'line': 3, 'class_correct': True, 'anomaly_correct': True, 'true_class': 'SN', 'true_anomaly': True, 'points': 15},\n"
            "    {'line': 4, 'class_correct': True, 'anomaly_correct': True, 'true_class': 'WD', 'true_anomaly': False, 'points': 15},\n"
            "    {'line': 5, 'class_correct': True, 'anomaly_correct': True, 'true_class': 'rare_object', 'true_anomaly': True, 'points': 15}\n"
            "  ]\n"
            "}, ensure_ascii=False))\n"
            "print('SUCCESS')\n",
            encoding="utf-8",
        )

    def run_reviewer_once(self, *, repo_root: Path, registry_path: Path, base_url: str, secret_key: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["PATH"] = "/usr/bin:/bin"
        return subprocess.run(
            [
                sys.executable,
                str(REVIEWER_SCRIPT),
                "--once",
                "--repo-root",
                str(repo_root),
                "--registry-path",
                str(registry_path),
                "--base-url",
                base_url,
                "--secret-key",
                secret_key,
                "--max-concurrent",
                "1",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_reviewer_once_processes_queue_and_posts_evaluation(self) -> None:
        secret = "test-secret"
        queue_item = {
            "topic": {
                "id": "topic-1",
                "title": "101-CIFAR | SmallCNN Hyperparameter Challenge",
                "metadata": {
                    "arcade": {
                        "validator": {
                            "config": {
                                "source": "cabinets/turing-teahouse/101-CIFAR",
                            }
                        }
                    }
                },
            },
            "branch_root_post_id": "branch-root-1",
            "submission_post": {
                "id": "submission-1",
                "body": json.dumps(
                    {
                        "epochs": 10,
                        "lr": 0.01,
                        "weight_decay": 0.0,
                        "batch_size": 128,
                        "momentum": 0.9,
                    }
                ),
            },
        }
        server, base_url = self.start_server(queue_items=[queue_item], expected_secret=secret)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_cifar_runner(root)
            registry_path = self.write_registry(
                root,
                {
                    "cabinets/turing-teahouse/101-CIFAR": {
                        "cabinet_id": "101-cifar",
                        "cabinet_title": "101-CIFAR",
                        "family": "turing-teahouse",
                        "review_mode": "local_subprocess",
                        "reviewer_entry": "arcade_reviewer.py",
                        "runtime": {
                            "cwd": "cabinets/turing-teahouse/101-CIFAR",
                            "runner": "builtin:101-cifar",
                            "timeout_seconds": 1800,
                            "max_parallel": 2,
                            "batch_window": 10,
                        },
                    }
                },
            )

            completed = self.run_reviewer_once(
                repo_root=root,
                registry_path=registry_path,
                base_url=base_url,
                secret_key=secret,
            )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr or completed.stdout)
        self.assertEqual(len(server.evaluations), 1)
        evaluation = server.evaluations[0]["payload"]
        self.assertEqual(evaluation["for_post_id"], "submission-1")
        self.assertEqual(evaluation["result"]["cabinet"], "cabinets/turing-teahouse/101-CIFAR")
        self.assertEqual(evaluation["result"]["score"], 0.2222)
        self.assertEqual(evaluation["result"]["status_line"], "SUCCESS")

    def test_reviewer_once_skips_unknown_cabinet_without_posting(self) -> None:
        secret = "test-secret"
        queue_item = {
            "topic": {
                "id": "topic-2",
                "title": "Unknown cabinet",
                "metadata": {
                    "arcade": {
                        "validator": {
                            "config": {
                                "source": "cabinets/demo-family/001-demo",
                            }
                        }
                    }
                },
            },
            "branch_root_post_id": "branch-root-2",
            "submission_post": {
                "id": "submission-2",
                "body": "{\"hello\": \"world\"}",
            },
        }
        server, base_url = self.start_server(queue_items=[queue_item], expected_secret=secret)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = self.write_registry(root, {})
            completed = self.run_reviewer_once(
                repo_root=root,
                registry_path=registry_path,
                base_url=base_url,
                secret_key=secret,
            )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr or completed.stdout)
        self.assertEqual(server.evaluations, [])
        self.assertIn("skip unsupported task", completed.stdout)

    def test_reviewer_once_processes_variable_star_relay(self) -> None:
        secret = "test-secret"
        queue_item = {
            "topic": {
                "id": "topic-3",
                "title": "102-Variable Star Relay Review",
                "metadata": {
                    "arcade": {
                        "validator": {
                            "config": {
                                "source": "cabinets/citizen-science-harbor/102-variable-star-citizen-science",
                            }
                        }
                    }
                },
            },
            "branch_root_post_id": "branch-root-3",
            "submission_post": {
                "id": "submission-3",
                "body": "\n".join(
                    [
                        "![](https://example.com/a.png) | CV | 正常 | reasonable short reason",
                        "![](https://example.com/b.png) | YSO | 正常 | another acceptable reason",
                        "![](https://example.com/c.png) | SN | 异常 | transient-like one-off evolution",
                        "![](https://example.com/d.png) | WD | 正常 | compact and cleaner structure",
                        "![](https://example.com/e.png) | rare_object | 异常 | highly unusual morphology overall",
                    ]
                ),
            },
        }
        server, base_url = self.start_server(queue_items=[queue_item], expected_secret=secret)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_variable_star_runner(root)
            registry_path = self.write_registry(
                root,
                {
                    "cabinets/citizen-science-harbor/102-variable-star-citizen-science": {
                        "cabinet_id": "102-variable-star-citizen-science",
                        "cabinet_title": "102-Variable-Star-Citizen-Science",
                        "family": "citizen-science-harbor",
                        "review_mode": "local_subprocess",
                        "reviewer_entry": "arcade_reviewer.py",
                        "runtime": {
                            "cwd": "cabinets/citizen-science-harbor/102-variable-star-citizen-science",
                            "runner": "builtin:102-variable-star-relay",
                            "timeout_seconds": 60,
                            "max_parallel": 4,
                            "batch_window": 20,
                        },
                    }
                },
            )

            completed = self.run_reviewer_once(
                repo_root=root,
                registry_path=registry_path,
                base_url=base_url,
                secret_key=secret,
            )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr or completed.stdout)
        self.assertEqual(len(server.evaluations), 1)
        evaluation = server.evaluations[0]["payload"]
        self.assertEqual(evaluation["for_post_id"], "submission-3")
        self.assertEqual(evaluation["result"]["cabinet"], "cabinets/citizen-science-harbor/102-variable-star-citizen-science")
        self.assertEqual(evaluation["result"]["score"], 100.0)
        self.assertEqual(evaluation["result"]["raw_points"], 75)
        self.assertEqual(evaluation["result"]["coverage"]["newly_covered_count"], 5)
        self.assertEqual(len(evaluation["result"]["coverage"]["next_batch"]), 2)
        self.assertIn("下一批建议样本：", evaluation["body"])


if __name__ == "__main__":
    unittest.main()
