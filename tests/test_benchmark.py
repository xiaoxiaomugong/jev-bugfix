"""Offline behavioral checks for normalized run evidence, never benefit samples."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "benchmarks" / "summarize_runs.py"


def metric(value, unit, kind="measured", completeness="complete"):
    return {"value": value, "unit": unit, "kind": kind,
            "source": "synthetic_fixture", "evidence": ["synthetic.log"],
            "completeness": completeness, "reason": None}


def missing(unit, reason="runner has no export"):
    return {"value": None, "unit": unit, "kind": "unavailable", "source": None,
            "evidence": [], "completeness": "unavailable", "reason": reason}


def run(arm="A", status="success", run_id=None, pair_id="p1", case_id="case1"):
    identifier = run_id or arm + "1"
    checks = [{"role": role, "command": "python3 check.py", "exit_code": code,
               "evidence": ["synthetic-tests.log"]}
              for role, code in (("reproduction_before", 1), ("reproduction_after", 0),
                                 ("independent", 0), ("regression", 0))]
    return {"schema_version": 1, "case_id": case_id, "run_id": identifier,
            "pair_id": pair_id, "arm": arm, "development_fixture": True,
            "repository": {"url": "synthetic://fixture", "revision": "buggy-v1"},
            "config": {"model": "gpt-6.1-sol", "reasoning": "fixed",
                       "prompt_hash": "p" * 64, "tools_hash": "t" * 64,
                       "environment_hash": "e" * 64, "budget_hash": "b" * 64},
            "candidate_hash": None if arm == "A" else "c" * 64,
            "timing": {"started_at": "2026-10-02T00:00:00Z",
                       "finished_at": "2026-10-02T00:00:10Z",
                       "evidence": ["synthetic-clock.log"],
                       "phases": {key: missing("seconds") for key in
                                  ("collection", "preflight", "scoring", "investigation", "repair", "tests")}},
            "capture": {"complete": True, "method": "explicit-source-events-v1",
                        "scope": "all model-visible source", "reason": None,
                        "evidence": ["synthetic.log"]},
            "outcome": {"status": status, "tests_unmodified": True,
                        "validations": checks, "reason": None},
            "usage": {"input_tokens": missing("tokens"), "cached_input_tokens": missing("tokens"),
                      "output_tokens": missing("tokens"), "definitions": "not exported"},
            "costs": {"currency": "USD", "main_model": metric(0.5, "USD"),
                      "jev": metric(0 if arm == "A" else 0.1, "USD"),
                      "review": metric(0, "USD")},
            "jev": {"version": None if arm == "A" else "0.3.2", "ranker_revision": "fixed-v2",
                    "status": "not_used" if arm == "A" else "ranked",
                    "failure_types": [],
                    **{key: metric(value, unit) for key, value, unit in
                       (("cli_invocations", 0 if arm == "A" else 1, "count"),
                        ("preflight_invocations", 0 if arm == "A" else 1, "count"),
                        ("submitted_candidates", 0 if arm == "A" else 2, "count"),
                        ("payload_bytes", 0 if arm == "A" else 120, "bytes"),
                        ("http_attempts_upper_bound", 0 if arm == "A" else 4, "count"))}},
            "protocol_deviations": [], "evidence": ["synthetic.log"]}


def read_event(run_id, event_id, start=1, end=3, size=30, version="f1"):
    return {"schema_version": 1, "run_id": run_id, "event_id": event_id,
            "type": "source_read", "timestamp": "2026-10-02T00:00:01Z",
            "path": "src/example.py", "file_version": version,
            "start_line": start, "end_line": end, "bytes": size,
            "source": "synthetic_tool_output", "evidence": ["synthetic.log"]}


class RunBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SCRIPT.exists():
            return
        spec = importlib.util.spec_from_file_location("run_benchmark", SCRIPT)
        cls.benchmark = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.benchmark)

    def summarize(self, runs, events=()):
        self.assertTrue(SCRIPT.exists(), "normalized run summarizer is not implemented")
        return self.benchmark.summarize(runs, list(events))

    def test_overlap_counts_exposure_twice_but_merges_lines_per_version(self):
        events = [read_event("A1", "1"), read_event("A1", "2", 2, 4, 20),
                  read_event("A1", "3", 1, 2, 10, "f2")]
        report = self.summarize([run()], events)
        observed = report["runs"][0]["reading"]
        self.assertEqual(observed["cumulative_bytes"]["value"], 60)
        self.assertEqual(observed["unique_lines"]["value"], 6)
        self.assertEqual(observed["intervals"][0]["ranges"], [[1, 4]])

    def test_duplicate_events_rejected_instead_of_double_counted(self):
        event = read_event("A1", "1")
        with self.assertRaisesRegex(ValueError, "duplicate event"):
            self.summarize([run()], [event, copy.deepcopy(event)])

    def test_missing_cost_is_null_and_known_subtotal_is_preserved(self):
        r = run("C")
        r["costs"]["main_model"] = missing("USD")
        report = self.summarize([r])
        self.assertIsNone(report["runs"][0]["total_real_cost"]["value"])
        self.assertAlmostEqual(report["arms"]["C"]["known_cost_subtotal"], 0.1)
        self.assertIsNone(report["arms"]["C"]["cost_per_success"])

    def test_estimated_cost_never_becomes_actual_cost(self):
        r = run("C")
        r["costs"]["main_model"] = metric(0.2, "USD", "estimated")
        r["costs"]["main_model"]["price"] = {
            "date": "2026-10-02", "source": "synthetic-price", "basis": "hypothetical"}
        report = self.summarize([r])
        self.assertIsNone(report["runs"][0]["total_real_cost"]["value"])
        self.assertAlmostEqual(report["runs"][0]["total_estimated_cost"]["value"], 0.3)

    def test_failure_costs_and_zero_success_are_retained(self):
        report = self.summarize([run("C", "failure"),
                                 run("C", "timeout", "C2", "p2", "case2")])
        self.assertEqual(report["arms"]["C"]["total_runs"], 2)
        self.assertEqual(report["arms"]["C"]["successes"], 0)
        self.assertAlmostEqual(report["arms"]["C"]["total_real_cost"], 1.2)
        self.assertIsNone(report["arms"]["C"]["cost_per_success"])

    def test_pair_repository_and_configuration_mismatch_rejected(self):
        for field in ("revision", "prompt_hash", "model", "reasoning", "tools_hash",
                      "environment_hash", "budget_hash"):
            a, c = run(), run("C")
            target = c["repository"] if field == "revision" else c["config"]
            target[field] = "different"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "pair mismatch"):
                self.summarize([a, c])

    def test_incomplete_capture_shows_lower_bound_but_no_reading_comparison(self):
        a, c = run(), run("C")
        c["capture"].update(complete=False, reason="one tool is unavailable")
        report = self.summarize([a, c], [read_event("A1", "a", size=100),
                                       read_event("C1", "c", size=10)])
        self.assertEqual(report["runs"][1]["reading"]["cumulative_bytes"]["completeness"], "lower_bound")
        self.assertIsNone(report["pairs"][0]["reading_bytes_delta"])
        self.assertEqual(report["benefit_evidence"]["status"], "insufficient_evidence")

    def test_different_capture_method_or_scope_blocks_reading_comparison(self):
        for field in ("method", "scope"):
            a, c = run(), run("C")
            c["capture"][field] = "different"
            report = self.summarize([a, c])
            self.assertIsNone(report["pairs"][0]["reading_bytes_delta"])

    def test_repetition_does_not_increase_independent_task_count(self):
        records = [run(), run("C"), run("A", run_id="A2", pair_id="p2"),
                   run("C", run_id="C2", pair_id="p2")]
        report = self.summarize(records)
        self.assertEqual(report["independent_tasks"], 1)
        self.assertEqual(len(report["pairs"]), 2)
        self.assertEqual(len(report["tasks"]), 1)

    def test_missing_outcome_is_unavailable_not_success(self):
        r = run()
        r["outcome"] = None
        report = self.summarize([r])
        self.assertEqual(report["arms"]["A"]["successes"], 0)
        self.assertEqual(report["runs"][0]["outcome"]["status"], "unavailable")

    def test_success_requires_unchanged_tests_and_all_validation_roles(self):
        for mutation in ("remove", "failed", "modified"):
            r = run()
            if mutation == "remove":
                r["outcome"]["validations"].pop()
            elif mutation == "failed":
                r["outcome"]["validations"][-1]["exit_code"] = 1
            else:
                r["outcome"]["tests_unmodified"] = False
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "success evidence"):
                self.summarize([r])

    def test_duplicate_runs_and_duplicate_arm_in_pair_rejected(self):
        for records in ([run(), run()], [run(), run(run_id="A2")]):
            with self.assertRaisesRegex(ValueError, "duplicate"):
                self.summarize(records)

    def test_verified_root_event_records_first_correct_root_time(self):
        root = {"schema_version": 1, "run_id": "A1", "event_id": "root",
                "type": "root_cause", "timestamp": "2026-10-02T00:00:04Z",
                "claim": "fixture explanation", "verified": True,
                "source": "fixture evaluator", "evidence": ["synthetic.log"]}
        report = self.summarize([run()], [root])
        self.assertEqual(report["runs"][0]["first_correct_root_seconds"]["value"], 4)
        self.assertIsNone(self.summarize([run()])["runs"][0]["first_correct_root_seconds"]["value"])

    def test_fixture_savings_never_claim_real_benefits(self):
        report = self.summarize([run(), run("C")],
                                [read_event("A1", "a", size=100), read_event("C1", "c", size=10)])
        self.assertEqual(report["pairs"][0]["reading_bytes_delta"], -90)
        self.assertEqual(report["benefit_evidence"]["status"], "insufficient_evidence")
        self.assertEqual(report["real_experiment_runs"], 0)

    def test_metrics_need_evidence_and_missing_values_need_reason(self):
        for mutation in ("no_source", "no_reason", "negative", "boolean"):
            r = run()
            m = r["costs"]["main_model"]
            if mutation == "no_source":
                m["evidence"] = []
            elif mutation == "no_reason":
                m.update(value=None, kind="unavailable", reason=None)
            elif mutation == "negative":
                m["value"] = -1
            else:
                m["value"] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.summarize([r])

    def test_event_unknown_run_and_outside_timing_rejected(self):
        for event in (read_event("unknown", "a"), read_event("A1", "b")):
            if event["run_id"] == "A1":
                event["timestamp"] = "2026-10-02T00:00:11Z"
            with self.assertRaises(ValueError):
                self.summarize([run()], [event])

    def test_real_quality_failure_pauses_but_missing_outcome_is_insufficient(self):
        for status, expected in (("failure", "pause_for_quality_failure"),
                                 ("timeout", "pause_for_quality_failure"),
                                 (None, "insufficient_evidence")):
            a, c = run(), run("C", status or "success")
            a["development_fixture"] = c["development_fixture"] = False
            if status is None:
                c["outcome"] = None
            report = self.summarize([a, c])
            self.assertEqual(report["benefit_evidence"]["status"], expected)

    def test_unpaired_wrong_case_and_unknown_version_bound_rejected(self):
        a, c = run(), run("C", case_id="different")
        with self.assertRaisesRegex(ValueError, "pair mismatch"):
            self.summarize([a, c])
        c = run("C")
        c["jev"]["version"] = "future"
        with self.assertRaisesRegex(ValueError, "unknown CLI"):
            self.summarize([c])

    def test_model_token_estimates_are_not_accepted_as_usage(self):
        r = run()
        r["usage"]["input_tokens"] = metric(100, "tokens", "estimated")
        r["usage"]["input_tokens"]["price"] = {
            "date": "2026-10-02", "source": "guess", "basis": "character heuristic"}
        with self.assertRaisesRegex(ValueError, "supplier usage"):
            self.summarize([r])

    def test_c_budget_deviation_cannot_be_silently_called_compliant(self):
        for key, value in (("cli_invocations", 2), ("submitted_candidates", 13),
                           ("payload_bytes", 24577), ("preflight_invocations", 2)):
            c = run("C")
            c["jev"][key]["value"] = value
            if key == "submitted_candidates":
                c["jev"]["http_attempts_upper_bound"]["value"] = 26
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "budget deviation"):
                self.summarize([c])
            c["protocol_deviations"] = ["declared extra run/scoring budget"]
            self.assertEqual(self.summarize([c])["arms"]["C"]["total_runs"], 1)

    def test_started_scoring_requires_exact_reserved_http_bound(self):
        for bound in (0, 1, 3, 5):
            c = run("C")
            c["jev"]["http_attempts_upper_bound"]["value"] = bound
            with self.subTest(bound=bound), self.assertRaisesRegex(ValueError, "HTTP bound"):
                self.summarize([c])
        for key in ("http_attempts_upper_bound", "submitted_candidates", "cli_invocations"):
            c = run("C")
            c["jev"][key] = missing("count")
            with self.subTest(unavailable=key):
                report = self.summarize([c])
                self.assertIsNone(report["runs"][0]["jev"][key]["value"])

    def test_no_scoring_start_cannot_claim_reserved_http_attempts(self):
        c = run("C")
        c["jev"]["cli_invocations"]["value"] = 0
        with self.assertRaisesRegex(ValueError, "HTTP bound"):
            self.summarize([c])

    def real_pilot(self):
        records, events = [], []
        for index in range(6):
            for arm, size in (("A", 100), ("C", 10)):
                r = run(arm, run_id=arm + str(index), pair_id="p" + str(index), case_id="case" + str(index))
                r["development_fixture"] = False
                r["costs"]["main_model"]["value"] = 0.5 if arm == "A" else 0.4
                records.append(r)
                events.append(read_event(r["run_id"], "source", size=size))
        return records, events

    def test_disjoint_repeat_metrics_cannot_be_joined_into_benefit_evidence(self):
        records, events = self.real_pilot()
        extra = []
        for original in records:
            original["timing"]["finished_at"] = None
            r = copy.deepcopy(original)
            r["run_id"] += "-time"
            r["pair_id"] += "-time"
            r["timing"]["finished_at"] = "2026-10-02T00:00:10Z"
            r["capture"].update(complete=False, reason="reading not captured")
            extra.append(r)
        report = self.summarize(records + extra, events)
        self.assertEqual(report["benefit_evidence"]["status"], "insufficient_evidence")
        self.assertEqual(report["benefit_evidence"]["eligible_real_tasks"], 0)

    def test_unfinished_real_repeats_block_benefit_targets(self):
        records, events = self.real_pilot()
        self.assertEqual(self.summarize(records, events)["benefit_evidence"]["status"],
                         "exploratory_target_met")
        for arm in ("A", "C"):
            for gap in ("null_outcome", "unavailable_outcome", "unfinished_timing"):
                repeated = [copy.deepcopy(record) for record in records[:2]]
                for record in repeated:
                    record["run_id"] += "-unfinished"
                    record["pair_id"] += "-unfinished"
                unfinished = next(record for record in repeated if record["arm"] == arm)
                unfinished["costs"]["main_model"] = missing("USD", "run is unfinished")
                if gap == "null_outcome":
                    unfinished["outcome"] = None
                elif gap == "unavailable_outcome":
                    unfinished["outcome"]["status"] = "unavailable"
                else:
                    unfinished["timing"]["finished_at"] = None
                with self.subTest(arm=arm, gap=gap):
                    report = self.summarize(records + repeated, events)
                    self.assertEqual(report["benefit_evidence"]["status"], "insufficient_evidence")
                    self.assertEqual(report["real_experiment_runs"], 14)
                    self.assertEqual(report["arms"][arm]["total_runs"], 7)
                    observed = next(record for record in report["runs"]
                                    if record["run_id"] == unfinished["run_id"])
                    self.assertIsNone(observed["total_real_cost"]["value"])
                    if gap == "unfinished_timing":
                        self.assertIsNone(observed["elapsed_seconds"]["value"])
                    else:
                        self.assertEqual(observed["outcome"]["status"], "unavailable")
                        self.assertEqual(report["arms"][arm]["successes"], 6)
        unfinished_repeat = [copy.deepcopy(record) for record in records[:2]]
        for record in unfinished_repeat:
            record["run_id"] += "-pending"
            record["pair_id"] += "-pending"
        unfinished_repeat[1]["outcome"] = None
        unfinished_repeat[1]["timing"]["finished_at"] = None
        quality_failure = [copy.deepcopy(record) for record in records[:2]]
        for record in quality_failure:
            record["run_id"] += "-quality-failure"
            record["pair_id"] += "-quality-failure"
        quality_failure[1]["outcome"]["status"] = "failure"
        report = self.summarize(records + unfinished_repeat + quality_failure, events)
        self.assertEqual(report["benefit_evidence"]["status"], "pause_for_quality_failure")
        self.assertEqual(report["benefit_evidence"]["cases"], ["case0"])
        self.assertEqual(report["real_experiment_runs"], 16)

    def test_failure_repeat_costs_enter_real_cost_target(self):
        records, events = self.real_pilot()
        extra = []
        for original in records:
            r = copy.deepcopy(original)
            r["run_id"] += "-failed"
            r["pair_id"] += "-failed"
            r["outcome"]["status"] = "failure"
            r["costs"]["main_model"]["value"] = 0.5 if r["arm"] == "A" else 1.4
            extra.append(r)
        report = self.summarize(records + extra, events)
        self.assertEqual(report["benefit_evidence"]["cost_target"], "not_met")
        self.assertEqual(report["benefit_evidence"]["status"], "exploratory_target_not_met")
        self.assertAlmostEqual(report["arms"]["C"]["cost_per_success"], 2)

    def test_missing_jev_version_cli_rejects_without_traceback(self):
        self.assertTrue(SCRIPT.exists())
        r = run("C")
        del r["jev"]["version"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.json"
            path.write_text(json.dumps([r]), encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--runs", str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)

    def test_unpaired_real_runs_block_benefit_target(self):
        records, events = self.real_pilot()
        extra = run("C", "failure", "unpaired", "unpaired", "case0")
        extra["development_fixture"] = False
        report = self.summarize(records + [extra], events)
        self.assertEqual(report["benefit_evidence"]["status"], "insufficient_evidence")

    def test_jsonl_cli_and_markdown_report_are_reproducible(self):
        self.assertTrue(SCRIPT.exists(), "normalized run summarizer is not implemented")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            records, events = base / "runs.jsonl", base / "events.jsonl"
            records.write_text(json.dumps(run()) + "\n", encoding="utf-8")
            events.write_text(json.dumps(read_event("A1", "a")) + "\n", encoding="utf-8")
            result = subprocess.run([sys.executable, "-B", str(SCRIPT), "--runs", str(records),
                                     "--events", str(events), "--json", str(base / "out.json"),
                                     "--markdown", str(base / "out.md")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((base / "out.json").read_text())
            self.assertEqual(report["independent_tasks"], 1)
            self.assertIn("DEVELOPMENT FIXTURE", (base / "out.md").read_text())


if __name__ == "__main__":
    unittest.main()
