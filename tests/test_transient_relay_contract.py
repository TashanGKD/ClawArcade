from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RELAY_SERVER_PATH = (
    REPO_ROOT
    / "cabinets"
    / "citizen-science-harbor"
    / "103-data-sample-relay-review"
    / "relay_server.py"
)
EVALUATOR_PATH = RELAY_SERVER_PATH.with_name("evaluate_submission.py")


def load_relay_server():
    tmp = tempfile.mkdtemp(prefix="transient-relay-contract-")
    os.environ["RELAY_STATE_PATH"] = str(Path(tmp) / "relay-state.json")
    spec = importlib.util.spec_from_file_location("transient_relay_server_contract_test", RELAY_SERVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load relay_server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class TransientRelayContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.relay = load_relay_server()

    def test_claim_response_points_to_topiclab_openclaw_submission(self) -> None:
        payload = self.relay.make_claim({"participant_id": "contract-test"})

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["submit_to"], "TopicLab Arcade branch reply")
        self.assertEqual(payload["method"], "POST")
        self.assertIn("/api/v1/openclaw/topics/", payload["openclaw_endpoint"])
        self.assertIn(payload["openclaw_endpoint"], payload["openclaw_url"])
        self.assertIn("only assigns images", payload["note"])
        self.assertIn("TopicLab", payload["note"])

    def test_status_separates_legacy_local_submissions_from_topiclab_records(self) -> None:
        state = self.relay.load_state()
        payload = self.relay.status_payload(state)

        self.assertTrue(payload["ok"])
        self.assertIn("local_legacy_submission_count", payload)
        self.assertIn("topiclab_submission_count", payload)
        self.assertIsNone(payload["topiclab_submission_count"])
        self.assertIn("legacy", payload["submission_count_note"])
        self.assertIn("/api/v1/openclaw/topics/", payload["openclaw_endpoint"])

    def test_evaluator_reports_invalid_submission_as_json_success_protocol(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as fh:
            fh.write("bad one-line answer\n")
            submission_path = fh.name
        try:
            completed = subprocess.run(
                [sys.executable, str(EVALUATOR_PATH), "--submission", submission_path],
                cwd=str(EVALUATOR_PATH.parent),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        finally:
            Path(submission_path).unlink(missing_ok=True)

        stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
        self.assertEqual(completed.returncode, 0)
        self.assertGreaterEqual(len(stdout_lines), 2)
        self.assertEqual(stdout_lines[-1], "SUCCESS")

        payload = json.loads("\n".join(stdout_lines[:-1]))
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["line_count"], 1)
        self.assertIn("submission must contain exactly 5 non-empty lines, got 1", payload["errors"])


if __name__ == "__main__":
    unittest.main()
