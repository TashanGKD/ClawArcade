from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ArcadeReviewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module("clawarcade_arcade_reviewer_test", REPO_ROOT / "arcade_reviewer.py")

    def test_load_reviewer_registry_reads_known_cabinet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer_registry.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cabinets": {
                            "cabinets/turing-teahouse/101-CIFAR": {
                                "cabinet_id": "101-cifar",
                                "runtime": {
                                    "cwd": "cabinets/turing-teahouse/101-CIFAR",
                                    "runner": "builtin:101-cifar",
                                    "timeout_seconds": 1800,
                                    "max_parallel": 2,
                                    "batch_window": 10,
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            registry = self.module.load_reviewer_registry(path)
            self.assertIn("cabinets/turing-teahouse/101-CIFAR", registry)

    def test_load_reviewer_registry_rejects_malformed_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reviewer_registry.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cabinets": {
                            "cabinets/turing-teahouse/101-CIFAR": {
                                "cabinet_id": "101-cifar",
                                "runtime": {
                                    "cwd": "cabinets/turing-teahouse/101-CIFAR",
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                self.module.load_reviewer_registry(path)

    def test_evaluate_item_routes_by_validator_source(self) -> None:
        item = {
            "topic": {
                "metadata": {
                    "arcade": {
                        "validator": {
                            "config": {
                                "source": "cabinets/demo-family/001-demo",
                            }
                        }
                    }
                }
            }
        }
        registry = {
            "cabinets/demo-family/001-demo": {
                "runtime": {
                    "cwd": "cabinets/demo-family/001-demo",
                    "runner": "builtin:test-runner",
                    "timeout_seconds": 99,
                    "max_parallel": 1,
                    "batch_window": 5,
                }
            }
        }

        calls: list[tuple[Path, str, int]] = []

        def fake_runner(item, *, repo_root, registry_entry, timeout):
            calls.append((repo_root, registry_entry["source"], timeout))
            return "body", {"score": 1.0}

        with mock.patch.dict(self.module.BUILTIN_RUNNERS, {"builtin:test-runner": fake_runner}, clear=False):
            result = self.module.evaluate_item(
                item,
                repo_root=REPO_ROOT,
                registry=registry,
                timeout=123,
            )

        self.assertEqual(result, ("body", {"score": 1.0}))
        self.assertEqual(calls, [(REPO_ROOT, "cabinets/demo-family/001-demo", 99)])

    def test_evaluate_item_returns_none_for_unknown_source(self) -> None:
        item = {
            "topic": {
                "metadata": {
                    "arcade": {
                        "validator": {
                            "config": {
                                "source": "cabinets/demo-family/001-demo",
                            }
                        }
                    }
                }
            }
        }

        result = self.module.evaluate_item(
            item,
            repo_root=REPO_ROOT,
            registry={},
            timeout=123,
        )
        self.assertIsNone(result)

    def test_run_101_cifar_preserves_result_shape(self) -> None:
        item = {
            "topic": {
                "metadata": {
                    "arcade": {
                        "validator": {
                            "config": {
                                "source": "cabinets/turing-teahouse/101-CIFAR",
                            }
                        }
                    }
                }
            },
            "submission_post": {
                "body": json.dumps(
                    {
                        "epochs": 10,
                        "lr": 0.01,
                        "weight_decay": 0.0,
                        "batch_size": 128,
                        "momentum": 0.9,
                    }
                )
            },
        }
        registry_entry = {
            "source": "cabinets/turing-teahouse/101-CIFAR",
            "runtime": {
                "cwd": "cabinets/turing-teahouse/101-CIFAR",
                "runner": "builtin:101-cifar",
                "timeout_seconds": 1800,
                "max_parallel": 2,
                "batch_window": 10,
            },
        }

        completed = subprocess.CompletedProcess(
            args=["python", "train.py"],
            returncode=0,
            stdout="1,10\n0.1111,0.2222\nSUCCESS\n",
            stderr="INFO: ok\n",
        )

        with mock.patch.object(self.module.shutil, "which", return_value=None), mock.patch.object(
            self.module.subprocess,
            "run",
            return_value=completed,
        ):
            body, result = self.module.run_101_cifar(
                item,
                repo_root=REPO_ROOT,
                registry_entry=registry_entry,
                timeout=321,
            )

        self.assertEqual(body, "1,10\n\n0.1111,0.2222\n\nSUCCESS")
        self.assertEqual(result["cabinet"], "cabinets/turing-teahouse/101-CIFAR")
        self.assertEqual(result["score"], 0.2222)
        self.assertEqual(result["status_line"], "SUCCESS")

    def test_run_102_variable_star_relay_preserves_result_shape(self) -> None:
        item = {
            "topic": {
                "id": "topic-102",
                "metadata": {
                    "arcade": {
                        "validator": {
                            "config": {
                                "source": "cabinets/citizen-science-harbor/102-variable-star-citizen-science",
                            }
                        }
                    }
                }
            },
            "submission_post": {
                "body": "\n".join(
                    [
                        "![](https://example.com/a.png) | CV | 正常 | reasonable short reason",
                        "![](https://example.com/b.png) | YSO | 正常 | another acceptable reason",
                        "![](https://example.com/c.png) | SN | 异常 | transient-like one-off evolution",
                        "![](https://example.com/d.png) | WD | 正常 | compact and cleaner structure",
                        "![](https://example.com/e.png) | rare_object | 异常 | highly unusual morphology overall",
                    ]
                )
            },
        }
        registry_entry = {
            "source": "cabinets/citizen-science-harbor/102-variable-star-citizen-science",
            "runtime": {
                "cwd": "cabinets/citizen-science-harbor/102-variable-star-citizen-science",
                "runner": "builtin:102-variable-star-relay",
                "timeout_seconds": 60,
                "max_parallel": 4,
                "batch_window": 20,
            },
        }

        evaluator_stdout = json.dumps(
            {
                "raw_points": 71,
                "score_100": 94.67,
                "max_raw_points": 75,
                "rows": [
                    {"line": 1, "class_correct": True, "anomaly_correct": True, "true_class": "CV", "true_anomaly": False, "points": 15},
                    {"line": 2, "class_correct": False, "anomaly_correct": True, "true_class": "CV", "true_anomaly": False, "points": 5},
                    {"line": 3, "class_correct": True, "anomaly_correct": True, "true_class": "SN", "true_anomaly": True, "points": 15},
                    {"line": 4, "class_correct": True, "anomaly_correct": True, "true_class": "WD", "true_anomaly": False, "points": 15},
                    {"line": 5, "class_correct": True, "anomaly_correct": True, "true_class": "rare_object", "true_anomaly": True, "points": 15},
                ],
            },
            ensure_ascii=False,
        ) + "\nSUCCESS\n"

        completed = subprocess.CompletedProcess(
            args=["python", "evaluate_submission.py"],
            returncode=0,
            stdout=evaluator_stdout,
            stderr="",
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            cabinet_dir = repo_root / "cabinets" / "citizen-science-harbor" / "102-variable-star-citizen-science"
            (cabinet_dir / "data").mkdir(parents=True, exist_ok=True)
            (cabinet_dir / "data" / "manifest.json").write_text(
                json.dumps(
                    [
                        {"image_url": "https://example.com/a.png"},
                        {"image_url": "https://example.com/b.png"},
                        {"image_url": "https://example.com/c.png"},
                        {"image_url": "https://example.com/d.png"},
                        {"image_url": "https://example.com/e.png"},
                        {"image_url": "https://example.com/f.png"},
                    ]
                ),
                encoding="utf-8",
            )
            (cabinet_dir / "evaluate_submission.py").write_text("", encoding="utf-8")

            with mock.patch.object(
                self.module.subprocess,
                "run",
                return_value=completed,
            ):
                body, result = self.module.run_102_variable_star_relay(
                    item,
                    repo_root=repo_root,
                    registry_entry=registry_entry,
                    timeout=60,
                )

        self.assertIn("总分 71/75 (94.67/100)", body)
        self.assertIn("覆盖进度", body)
        self.assertIn("下一批建议样本：", body)
        self.assertEqual(result["cabinet"], "cabinets/citizen-science-harbor/102-variable-star-citizen-science")
        self.assertEqual(result["score"], 94.67)
        self.assertEqual(result["raw_points"], 71)
        self.assertEqual(result["coverage"]["newly_covered_count"], 5)


if __name__ == "__main__":
    unittest.main()
