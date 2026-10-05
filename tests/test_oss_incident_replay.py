"""Offline retrospective replay against pinned, licensed upstream sources."""

import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DRIVER_PATH = ROOT / "tests" / "oss_incident_replay.py"
PILOT = ROOT / "tests" / "evidence" / "production-next" / "pilot"
DRIVER = None
if DRIVER_PATH.exists():
    SPEC = importlib.util.spec_from_file_location("oss_incident_replay", DRIVER_PATH)
    DRIVER = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(DRIVER)


class OSSReplayTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(DRIVER, "pinned upstream replay driver is not implemented")

    def observe(self, version):
        return DRIVER.observe_source(PILOT / "source" / version / "iterutils.py")

    def test_real_error_version_reproduces_zero_division_at_actual_library_line(self):
        result = self.observe("broken")
        observed = result["observations"][0]
        self.assertEqual(observed["case"], "constant_equal_bounds")
        self.assertIsNone(observed["output"])
        self.assertEqual(observed["exception"]["type"], "ZeroDivisionError")
        # CPython's math.log message differs across supported Python versions.
        self.assertIn("division by zero", observed["exception"]["message"])
        self.assertEqual(observed["exception"]["frames"], [{"filename": "boltons/iterutils.py", "lineNo": 651, "function": "backoff_iter", "inApp": True}])

    def test_upstream_fixed_version_returns_value_for_same_input(self):
        result = self.observe("fixed")
        observed = result["observations"][0]
        self.assertEqual(observed["output"], [5.0])
        self.assertIsNone(observed["exception"])
        self.assertEqual(result["classification"], "oss_replay")
        self.assertTrue(result["known_answer"])

    def test_explicit_count_and_normal_growth_are_counterexamples_to_broad_rejection(self):
        for version in ("broken", "fixed"):
            observations = {value["case"]: value for value in self.observe(version)["observations"]}
            self.assertEqual(observations["constant_explicit_count"]["output"], [1.0, 1.0, 1.0])
            self.assertEqual(observations["growing_factor"]["output"], [1.0, 2.0, 4.0, 8.0])
            self.assertEqual(observations["constant_repeat_prefix"]["output"], [1.0, 1.0, 1.0])

    def test_unreachable_inferred_stop_gets_clear_value_error_after_fix(self):
        for case in ("constant_different_bounds", "constant_zero_start"):
            broken = {value["case"]: value for value in self.observe("broken")["observations"]}[case]
            fixed = {value["case"]: value for value in self.observe("fixed")["observations"]}[case]
            self.assertEqual(broken["exception"]["type"], "ZeroDivisionError")
            self.assertEqual(fixed["exception"]["type"], "ValueError")

    def test_reconstructed_event_preserves_observed_frames_without_inventing_trace(self):
        observed = self.observe("broken")
        original = copy.deepcopy(observed)
        event = DRIVER.reconstruct_sentry_event(observed, timestamp="2026-10-05T08:00:00Z")
        self.assertEqual(event["entries"][0]["data"]["values"][0]["stacktrace"]["frames"][0]["lineNo"], 651)
        self.assertEqual(event["eventID"], "b0170000000000000000000000000452")
        self.assertNotEqual(event["id"], event["eventID"])
        self.assertNotIn("trace", event.get("contexts", {}))
        self.assertNotIn("commit", event)
        self.assertTrue(event["extra"]["reconstructed_event"])
        self.assertEqual(observed, original)

    def test_unknown_source_is_rejected_before_loading_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "marker"
            source = Path(temporary) / "untrusted.py"
            source.write_text("from pathlib import Path\nPath(%r).touch()\n" % str(marker), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source_hash_mismatch"):
                DRIVER.observe_source(source)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
