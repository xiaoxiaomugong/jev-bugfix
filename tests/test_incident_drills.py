"""Synthetic offline integration drills; no credentials, Jev, or live API.

The tests catch mixing the checkout with event source, losing explicit runtime
evidence, or silently starting a scorer while preparing a V1 case.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "synthetic_incident_drills.py"


class SyntheticIncidentDrills(unittest.TestCase):
    def run_drill(self, scenario):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-drill-test-")
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "artifacts"
        completed = subprocess.run(
            [sys.executable, "-B", str(DRIVER), "--scenario", scenario,
             "--output-dir", str(output)],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            check=False,
        )
        saved_errors = "\n".join(path.read_text(encoding="utf-8")
                                 for path in output.rglob("prepare.stderr.txt"))
        self.assertEqual(completed.returncode, 0, completed.stderr + saved_errors)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["synthetic"])
        self.assertEqual(manifest["status"], "complete")
        self.assertEqual(len(manifest["drills"]), 1)
        self.assertFalse(any(path.name == ".git" for path in output.rglob("*")))
        drill = manifest["drills"][0]
        evidence = json.loads((output / drill["artifacts"]["evidence"]).read_text(
            encoding="utf-8"))
        case = json.loads((output / drill["artifacts"]["case"]).read_text(
            encoding="utf-8"))
        ranking = json.loads((output / drill["artifacts"]["ranker_dry_run"]).read_text(
            encoding="utf-8"))
        self.assertFalse(case["reviewed_for_secrets"])
        self.assertEqual(ranking["status"], "dry_run")
        self.assertEqual(ranking["usage"]["cli_invocations"], 0)
        self.assertEqual(ranking["cli_preflight"]["invocations"], 0)
        self.assertEqual(ranking["usage"]["http_attempts_upper_bound"], 0)
        self.assertEqual(evidence["version"]["resolution"]["event_commit"],
                         drill["commits"]["event"])
        self.assertEqual(len(case["candidates"]), 1)
        provenance = evidence["candidate_provenance"][0]
        candidate = case["candidates"][0]
        self.assertEqual(provenance["id"], candidate["id"])
        self.assertEqual(provenance["event_commit"], drill["commits"]["event"])
        self.assertEqual(provenance["snippet_sha256"], hashlib.sha256(
            candidate["snippet"].encode("utf-8")).hexdigest())
        self.assertTrue(provenance["evidence_refs"])
        return output, drill, evidence, case

    def test_same_commit_preserves_explicit_runtime_difference(self):
        output, drill, evidence, case = self.run_drill("runtime_difference")
        observations = json.loads((output / drill["artifacts"]["observations"]).read_text(
            encoding="utf-8"))
        self.assertEqual(drill["commits"]["event"], drill["commits"]["checkout"])
        self.assertEqual(observations["local"]["exit_code"], 0)
        self.assertEqual(observations["event"]["exit_code"], 1)
        self.assertEqual(observations["local"]["explicit_inputs"]["amount"], "1,25")
        self.assertEqual(observations["event"]["explicit_inputs"]["amount"], "1,25")
        self.assertEqual(observations["local"]["explicit_inputs"]["runtime_version"],
                         "compat-v1")
        self.assertEqual(observations["event"]["explicit_inputs"]["runtime_version"],
                         "compat-v2")
        self.assertEqual(drill["runtime_difference"], {
            "field": "runtime.version", "local": "compat-v1", "event": "compat-v2"})
        self.assertEqual(drill["sidecar_runtime"]["version"], "compat-v2")
        self.assertIn("return float(raw)", case["candidates"][0]["snippet"])

    def test_explicit_baseline_preserves_event_lines_with_baseline_checkout(self):
        output, drill, evidence, case = self.run_drill("regression")
        observations = json.loads((output / drill["artifacts"]["observations"]).read_text(
            encoding="utf-8"))
        self.assertEqual(observations["baseline"]["exit_code"], 0)
        self.assertEqual(observations["event"]["exit_code"], 1)
        self.assertEqual(observations["baseline"]["explicit_inputs"],
                         observations["event"]["explicit_inputs"])
        self.assertEqual(drill["commits"]["checkout"], drill["commits"]["baseline"])
        self.assertNotEqual(drill["commits"]["event"], drill["commits"]["checkout"])
        self.assertTrue(drill["baseline_explicitly_supplied"])
        candidate = case["candidates"][0]
        event_source = (output / drill["artifacts"]["event_source"]).read_text(
            encoding="utf-8").splitlines(keepends=True)
        baseline_source = (output / drill["artifacts"]["baseline_source"]).read_text(
            encoding="utf-8").splitlines(keepends=True)
        self.assertEqual(candidate["snippet"], "".join(
            event_source[candidate["start_line"] - 1:candidate["end_line"]]))
        self.assertNotEqual(candidate["snippet"], "".join(
            baseline_source[candidate["start_line"] - 1:candidate["end_line"]]))
        self.assertIn("# synthetic-event-version", candidate["snippet"])
        self.assertNotIn('raw.replace(",", ".")', candidate["snippet"])
        self.assertTrue(drill["candidate_handoff"]["verified_against_event_blob"])
        self.assertTrue(drill["candidate_handoff"]["checkout_would_be_wrong"])
        self.assertIn("followup_context", drill["candidate_handoff"])
        followup = drill["candidate_handoff"]["followup_context"]
        self.assertEqual(followup["event_commit"], drill["commits"]["event"])
        self.assertEqual(followup["definition_line"], 5)
        self.assertEqual(followup["direct_call_line"], 12)
        self.assertEqual(followup["checkout_definition_line"], 3)
        self.assertEqual(followup["checkout_direct_call_line"], 10)


if __name__ == "__main__":
    unittest.main()
