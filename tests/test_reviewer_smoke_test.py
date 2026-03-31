from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import types
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


class ReviewerSmokeTestScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module("clawarcade_reviewer_smoke_test_script", REPO_ROOT / "scripts" / "reviewer_smoke_test.py")

    def test_default_probes_cover_representative_cabinets(self) -> None:
        probes = {probe.name: probe for probe in self.module.get_default_probes()}

        self.assertIn("101-cifar-env", probes)
        self.assertIn("102-variable-star-evaluator", probes)
        self.assertEqual(probes["101-cifar-env"].source, "cabinets/turing-teahouse/101-CIFAR")
        self.assertEqual(
            probes["102-variable-star-evaluator"].source,
            "cabinets/citizen-science-harbor/102-variable-star-citizen-science",
        )

    def test_run_probe_returns_setup_failure_details(self) -> None:
        probe = self.module.get_default_probes()[0]
        reviewer = types.SimpleNamespace(
            ensure_setup_commands=lambda **_: (
                "body",
                {
                    "runtime_error_reason": "setup commands failed",
                    "command_executed": "uv sync",
                    "duration_seconds": 1.25,
                    "exit_code": 1,
                    "stderr_tail": ["uv: command not found"],
                },
            ),
            truncate_stderr=lambda text: [text] if text else [],
        )

        result = self.module.run_probe(
            probe,
            reviewer=reviewer,
            repo_root=REPO_ROOT,
            registry={probe.source: {"runtime": {"cwd": "cabinets/turing-teahouse/101-CIFAR"}}},
            timeout=30,
        )

        self.assertFalse(result["passed"])
        self.assertEqual(result["stage"], "setup")
        self.assertEqual(result["reason"], "setup commands failed")
        self.assertEqual(result["command"], "uv sync")

    def test_run_probe_executes_probe_command_after_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            cabinet_dir = repo_root / "cabinets" / "demo-family" / "001-demo"
            cabinet_dir.mkdir(parents=True)
            reviewer = types.SimpleNamespace(
                ensure_setup_commands=lambda **_: None,
                resolve_cabinet_python=lambda _: ["python3"],
                truncate_stderr=lambda text: [text] if text else [],
            )
            probe = self.module.SmokeProbe(
                name="demo-probe",
                source="cabinets/demo-family/001-demo",
                description="demo",
                command_builder=lambda reviewer_module, current_cabinet_dir: [*reviewer_module.resolve_cabinet_python(current_cabinet_dir), "demo.py"],
                validator=lambda completed: (completed.returncode == 0, None),
            )

            completed = subprocess.CompletedProcess(args=["python3", "demo.py"], returncode=0, stdout="ok\n", stderr="")
            with mock.patch.object(self.module.subprocess, "run", return_value=completed) as run_mock:
                result = self.module.run_probe(
                    probe,
                    reviewer=reviewer,
                    repo_root=repo_root,
                    registry={probe.source: {"runtime": {"cwd": "cabinets/demo-family/001-demo"}}},
                    timeout=30,
                )

        self.assertTrue(result["passed"])
        self.assertEqual(result["stage"], "probe")
        self.assertEqual(run_mock.call_args.kwargs["cwd"], str(cabinet_dir))
        self.assertEqual(run_mock.call_args.args[0], ["python3", "demo.py"])

