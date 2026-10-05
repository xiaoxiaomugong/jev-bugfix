"""Narrow stdlib regression checks for the upstream constant-backoff fix."""

import argparse
import importlib.util
from pathlib import Path
import unittest


DRIVER_PATH = Path(__file__).resolve().parents[3] / "oss_incident_replay.py"
SPEC = importlib.util.spec_from_file_location("oss_incident_replay", DRIVER_PATH)
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


class BackoffRegression(unittest.TestCase):
    source = None

    @classmethod
    def setUpClass(cls):
        observed = DRIVER.observe_source(cls.source)
        cls.observations = {entry["case"]: entry for entry in observed["observations"]}

    def test_equal_bounds_yield_one_value(self):
        self.assertEqual(self.observations["constant_equal_bounds"]["output"], [5.0])
        self.assertIsNone(self.observations["constant_equal_bounds"]["exception"])

    def test_different_bounds_reject_uninferable_count(self):
        self.assertEqual(self.observations["constant_different_bounds"]["exception"]["type"], "ValueError")

    def test_zero_start_rejects_uninferable_count(self):
        self.assertEqual(self.observations["constant_zero_start"]["exception"]["type"], "ValueError")

    def test_explicit_count_keeps_constant_sequence(self):
        self.assertEqual(self.observations["constant_explicit_count"]["output"], [1.0, 1.0, 1.0])

    def test_growing_factor_keeps_exponential_sequence(self):
        self.assertEqual(self.observations["growing_factor"]["output"], [1.0, 2.0, 4.0, 8.0])

    def test_repeat_keeps_constant_prefix(self):
        self.assertEqual(self.observations["constant_repeat_prefix"]["output"], [1.0, 1.0, 1.0])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    arguments = parser.parse_args()
    BackoffRegression.source = arguments.source
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BackoffRegression))
    raise SystemExit(0 if result.wasSuccessful() else 1)
