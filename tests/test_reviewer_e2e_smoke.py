from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "reviewer_e2e_smoke.py"


class ReviewerE2ESmokeScriptTests(unittest.TestCase):
    def write_registry(self, root: Path, cabinets: dict[str, dict[str, object]]) -> Path:
        registry_path = root / "generated" / "reviewer_registry.json"
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            json.dumps({"schema_version": 1, "cabinets": cabinets}, indent=2),
            encoding="utf-8",
        )
        return registry_path

    def write_variable_star_cabinet(self, root: Path) -> None:
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
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (cabinet_dir / "forum_post_template.txt").write_text(
            "\n".join(
                [
                    "![](https://example.com/a.png) | CV | 正常 | reasonable short reason",
                    "![](https://example.com/b.png) | YSO | 正常 | another acceptable reason",
                    "![](https://example.com/c.png) | SN | 异常 | transient-like one-off evolution",
                    "![](https://example.com/d.png) | WD | 正常 | compact and cleaner structure",
                    "![](https://example.com/e.png) | rare_object | 异常 | highly unusual morphology overall",
                ]
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

    def test_reviewer_e2e_smoke_runs_fake_queue_and_posts_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir(parents=True, exist_ok=True)
            (root / "arcade_reviewer.py").write_text((REPO_ROOT / "arcade_reviewer.py").read_text(encoding="utf-8"), encoding="utf-8")
            self.write_variable_star_cabinet(root)
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

            completed = subprocess.run(
                [
                    sys.executable,
                    str(SMOKE_SCRIPT),
                    "--repo-root",
                    str(root),
                    "--registry-path",
                    str(registry_path),
                    "--timeout",
                    "60",
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, msg=completed.stderr or completed.stdout)
        self.assertIn("[reviewer-e2e] pass", completed.stdout)
        self.assertIn("score=100.0", completed.stdout)

