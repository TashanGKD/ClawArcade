from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CabinetScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.build_module = load_module("clawarcade_build_cabinets_test", REPO_ROOT / "scripts" / "build_cabinets.py")
        self.new_module = load_module("clawarcade_new_cabinet_test", REPO_ROOT / "scripts" / "new_cabinet.py")

    def test_load_all_cabinets_finds_repo_cabinets_under_cabinets_root(self) -> None:
        cabinets = self.build_module.load_all_cabinets()
        ids = {cabinet["cabinet"]["id"] for cabinet in cabinets}
        self.assertIn("101-cifar", ids)
        self.assertIn("101-comforting-a-graduate-student", ids)
        for cabinet in cabinets:
            cabinet_dir = cabinet["_cabinet_dir"]
            self.assertIn("cabinets", cabinet_dir.parts)

    def test_render_topiclab_meta_produces_arcade_payload(self) -> None:
        cabinet = self.build_module.load_all_cabinets()[0]
        payload = json.loads(self.build_module.render_topiclab_meta(cabinet, "en"))
        self.assertEqual(payload["metadata"]["scene"], "arcade")
        self.assertIn("arcade", payload["metadata"])
        self.assertIn("prompt", payload["metadata"]["arcade"])

    def test_render_root_readme_uses_family_yaml_summary(self) -> None:
        cabinets = self.build_module.load_all_cabinets()
        family_configs = self.build_module.load_family_configs(
            {cabinet["cabinet"]["family"] for cabinet in cabinets}
        )
        readme = self.build_module.render_root_readme(cabinets, family_configs)
        self.assertIn(
            "Cabinets for empathy, social judgment, tone control, and other human-like conversational behaviors.",
            readme,
        )
        self.assertIn(
            "Cabinets for compact technical experiments, reproducible scoring, and iterative problem-solving under fixed rules.",
            readme,
        )
        self.assertNotIn("101-Comforting-a-Graduate-Student/", readme)
        self.assertNotIn("101-CIFAR/", readme)

    def test_scaffold_cabinet_writes_into_cabinets_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cabinets_root = root / "cabinets"
            template_root = root / "templates"
            schema_root = root / "schemas"
            cabinets_root.mkdir()
            template_root.mkdir()
            schema_root.mkdir()
            template_path = template_root / "cabinet.template.yaml"
            template_path.write_text(
                "schema_version: 1\ncabinet:\n  id: example-cabinet\n  family: example-family\n  title: Example Cabinet\n  summary: One-line summary for repository lists and generated docs.\n"
                "topiclab:\n  shared:\n    board: reasoning\n    difficulty: medium\n    task_type: plain_text\n    output_mode: plain_text\n    validator:\n      type: manual\n    heartbeat_interval_minutes: 60\n    visibility: public_read\n"
                "  zh:\n    title: 示例\n    body: 中文\n    tags: [示例]\n    prompt: 中文 prompt\n    rules: 中文 rules\n"
                "  en:\n    title: Example\n    body: English\n    tags: [Example]\n    prompt: English prompt\n    rules: English rules\n"
                "review:\n  mode: manual\nreadme:\n  sections:\n    - title: Problem brief\n      body: Example body\n",
                encoding="utf-8",
            )

            self.new_module.REPO_ROOT = root
            self.new_module.CABINETS_ROOT = cabinets_root
            self.new_module.TEMPLATE_PATH = template_path
            cabinet_path = self.new_module.scaffold_cabinet(
                "test-family",
                "001-demo",
                title="Demo Cabinet",
                summary="Demo summary",
            )
            self.assertEqual(cabinet_path, cabinets_root / "test-family" / "001-demo" / "cabinet.yaml")
            self.assertTrue(cabinet_path.exists())
            text = cabinet_path.read_text(encoding="utf-8")
            self.assertIn("family: test-family", text)
            self.assertIn("id: 001-demo", text)
            self.assertIn("title: Demo Cabinet", text)
            self.assertIn("summary: Demo summary", text)

    def test_build_check_detects_outdated_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "cabinets" / "demo-family" / "001-demo").mkdir(parents=True)
            (root / "templates").mkdir()
            (root / "schemas").mkdir()
            schema = json.loads((REPO_ROOT / "schemas" / "cabinet.schema.json").read_text(encoding="utf-8"))
            (root / "schemas" / "cabinet.schema.json").write_text(json.dumps(schema), encoding="utf-8")
            cabinet_yaml = (REPO_ROOT / "templates" / "cabinet.template.yaml").read_text(encoding="utf-8")
            (root / "templates" / "cabinet.template.yaml").write_text(cabinet_yaml, encoding="utf-8")
            (root / "cabinets" / "demo-family" / "family.yaml").write_text(
                "title: Demo Family\nsummary: Demo family summary.\n",
                encoding="utf-8",
            )
            (root / "cabinets" / "demo-family" / "001-demo" / "cabinet.yaml").write_text(
                """schema_version: 1
cabinet:
  id: demo-cabinet
  family: demo-family
  title: Demo Cabinet
  summary: Demo summary.
topiclab:
  shared:
    board: reasoning
    difficulty: medium
    task_type: plain_text
    output_mode: plain_text
    validator:
      type: manual
      config:
        source: demo-family/001-demo
    heartbeat_interval_minutes: 60
    visibility: public_read
  zh:
    title: 示例题
    body: 中文 body
    tags: [示例]
    prompt: 中文 prompt
    rules: 中文 rules
  en:
    title: Demo task
    body: English body
    tags: [demo]
    prompt: English prompt
    rules: English rules
review:
  mode: manual
readme:
  sections:
    - title: Problem brief
      body: Demo body
""",
                encoding="utf-8",
            )
            (root / "README.md").write_text("stale\n", encoding="utf-8")

            self.build_module.REPO_ROOT = root
            self.build_module.CABINETS_ROOT = root / "cabinets"
            self.build_module.SCHEMA_PATH = root / "schemas" / "cabinet.schema.json"

            exit_code = self.build_module.build(check=True)
            self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
