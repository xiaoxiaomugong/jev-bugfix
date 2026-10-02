"""Offline subprocess tests for the Jev bugfix candidate ranking boundary.

Run with: python3 -m unittest discover -s tests -p 'test_rank.py' -v
No live Jev executable, API key, network, or third-party package is required.
"""

import json
import importlib.util
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills" / "jev-bugfix" / "scripts" / "rank_candidates.py"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fake_jev.py"
UNTRUSTED_ERROR = "REMOTE_RAW_ERROR_API_KEY=do-not-print-this-secret"

SPEC = importlib.util.spec_from_file_location("rank_candidates", HELPER)
RANK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RANK)


def candidate(identifier="a", **overrides):
    value = {
        "id": identifier,
        "path": "src/" + identifier + ".py",
        "start_line": 1,
        "end_line": 1,
        "snippet": "return x  # PRIVATE_SOURCE_EXCERPT_" + identifier,
        "origins": ["rg"],
    }
    value.update(overrides)
    if "end_line" not in overrides:
        value["end_line"] = value["start_line"] + max(1, len(value["snippet"].splitlines())) - 1
    return value


def case(candidates=None):
    return {
        "schema_version": 1,
        "reviewed_for_secrets": True,
        "bug": {
            "description": "A parser returns an empty result.",
            "reproduction": {
                "steps": ["Run parser on a nonempty example."],
                "expected": "A parsed item.",
                "actual": "An empty list.",
            },
            "stack_trace": ["src/a.py:1 in parse"],
        },
        "candidates": candidates if candidates is not None else [candidate()],
    }


class RankCandidatesCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="rank-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        # Spaces in the executable path expose shell-string command construction.
        self.fake = self.work / "fake jev executable.py"
        shutil.copyfile(FIXTURE, self.fake)
        self.fake.chmod(0o755)
        self.log = self.work / "calls.jsonl"

    def run_helper(self, data=None, *, execute=False, mode="success", extra=(),
                   env_extra=None, raw=None, executable=None):
        input_path = self.work / "case.json"
        input_path.write_text(raw if raw is not None else json.dumps(
            case() if data is None else data, ensure_ascii=False, allow_nan=True),
            encoding="utf-8")
        if self.log.exists():
            self.log.unlink()
        env = os.environ.copy()
        for key in list(env):
            if key.startswith("FAKE_JEV_"):
                del env[key]
        env.update({"FAKE_JEV_MODE": mode, "FAKE_JEV_LOG": str(self.log)})
        env.update(env_extra or {})
        command = [sys.executable, str(HELPER), "--input", str(input_path),
                   "--jev", str(executable or self.fake)]
        if execute:
            command.append("--execute")
        command.extend(extra)
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", env=env, cwd=self.work,
                                   timeout=8, check=False)
        calls = []
        if self.log.exists():
            calls = [json.loads(line) for line in self.log.read_text(
                encoding="utf-8").splitlines()]
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError:
            self.fail("Expected one JSON report from ranking CLI; "
                      "exit=%s stdout=%r stderr=%r" % (
                          completed.returncode, completed.stdout[:500],
                          completed.stderr[:500]))
        self.assertIsInstance(report, dict)
        self.assertEqual(report["schema_version"], 1)
        self.assertIn(report["status"], {"dry_run", "ranked", "partial", "fallback"})
        self.assertIsInstance(report["diagnostics"], list)
        self.assertEqual(set(report["usage"]), {
            "cli_invocations", "submitted_candidates", "http_attempts_upper_bound",
            "payload_bytes", "batch_timeout_seconds", "request_timeout_seconds",
        })
        for diagnostic in report["diagnostics"]:
            self.assertEqual(set(diagnostic), {"code", "message"})
        for entry in report["candidates"]:
            self.assertEqual(set(entry), {
                "id", "path", "start_line", "end_line", "origins", "must_inspect",
                "score", "label", "confidence", "status", "error",
            })
            self.assertIsInstance(entry["must_inspect"], bool)
            self.assertIn(entry["status"], {"scored", "local_only", "error", "pending"})
            if entry["score"] is not None:
                self.assertIsInstance(entry["score"], (int, float))
                self.assertTrue(math.isfinite(entry["score"]))
                self.assertGreaterEqual(entry["score"], 0)
                self.assertLessEqual(entry["score"], 1)
            if entry["error"] is not None:
                self.assertEqual(set(entry["error"]), {"code", "message"})
            if entry["status"] != "scored":
                self.assertIsNone(entry["label"])
                self.assertIsNone(entry["confidence"])
        return completed, report, calls

    def entries(self, report):
        return {row["id"]: row for row in report["candidates"]}

    def diagnostic_codes(self, report):
        return {row["code"] for row in report["diagnostics"]}

    def assert_no_calls(self, completed, report, calls, status="fallback"):
        self.assertEqual(completed.returncode, 0 if status == "dry_run" else 2)
        self.assertEqual(report["status"], status)
        self.assertEqual(calls, [])
        self.assertEqual(report["usage"]["cli_invocations"], 0)
        self.assertEqual(report["usage"]["submitted_candidates"], 0)
        self.assertEqual(report["usage"]["http_attempts_upper_bound"], 0)

    def assert_not_exposed(self, completed, *markers):
        output = completed.stdout + completed.stderr
        for marker in markers:
            self.assertNotIn(marker, output)

    def assert_metadata(self, report, data):
        rows = self.entries(report)
        self.assertEqual(list(rows), [item["id"] for item in data["candidates"]])
        for original in data["candidates"]:
            row = rows[original["id"]]
            for key in ("path", "start_line", "end_line", "origins"):
                self.assertEqual(row[key], original[key])

    def test_default_dry_run_never_launches_jev(self):
        completed, report, calls = self.run_helper()
        self.assert_no_calls(completed, report, calls, "dry_run")
        self.assertEqual(report["investigation_order"], ["a"])
        self.assertIsNone(self.entries(report)["a"]["score"])
        self.assert_not_exposed(completed, "PRIVATE_SOURCE_EXCERPT_a")

    def test_one_batch_has_bounded_argv_and_exact_jsonl_states(self):
        data = case([candidate("a"), candidate("b")])
        completed, report, calls = self.run_helper(data, execute=True, extra=(
            "--batch-timeout", "7", "--request-timeout", "3"))
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["status"], "ranked")
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        for flag, value in [("--range", "0-4"), ("--jobs", "2"),
                            ("--retries", "0"), ("--timeout", "3")]:
            if flag in argv:
                actual = argv[argv.index(flag) + 1]
            else:
                values = [arg.split("=", 1)[1] for arg in argv
                          if arg.startswith(flag + "=")]
                self.assertEqual(len(values), 1, flag + " missing from CLI argv")
                actual = values[0]
            if flag == "--timeout":
                self.assertEqual(float(actual), float(value))
            else:
                self.assertEqual(actual, value)
        self.assertIn("--lines", argv)
        self.assertIn("--json", argv)
        self.assertFalse(any(arg == "--field" or arg.startswith("--field=")
                             for arg in argv))
        self.assertEqual(calls[0]["states"], [
            {"schema_version": 1, "bug": data["bug"], "candidate": item}
            for item in data["candidates"]])
        self.assertEqual(report["usage"]["cli_invocations"], 1)
        self.assertEqual(report["usage"]["submitted_candidates"], 2)
        self.assertEqual(report["usage"]["http_attempts_upper_bound"], 4)
        self.assertEqual(report["usage"]["payload_bytes"], calls[0]["payload_bytes"])
        self.assertEqual(report["usage"]["batch_timeout_seconds"], 7)
        self.assertEqual(report["usage"]["request_timeout_seconds"], 3)
        self.assert_metadata(report, data)
        self.assert_not_exposed(completed, "PRIVATE_SOURCE_EXCERPT_a", "PRIVATE_SOURCE_EXCERPT_b")

    def test_shell_syntax_in_snippet_is_data(self):
        marker = self.work / "shell-was-run"
        snippet = "value = $(touch '" + str(marker) + "'); `touch '" + str(marker) + "'`"
        completed, report, calls = self.run_helper(
            case([candidate(snippet=snippet)]), execute=True)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["status"], "ranked")
        self.assertEqual(len(calls), 1)
        self.assertFalse(marker.exists())
        self.assertEqual(calls[0]["states"][0]["candidate"]["snippet"], snippet)

    def test_reversed_results_map_by_echoed_candidate_id(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b"), candidate("c")]),
            execute=True, mode="reverse", env_extra={
                "FAKE_JEV_SCORES": '{"a":0,"b":4,"c":2}'})
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(len(calls), 1)
        rows = self.entries(report)
        self.assertEqual([rows[key]["score"] for key in ("a", "b", "c")], [0, 1, 0.5])
        self.assertEqual(report["investigation_order"], ["b", "c", "a"])

    def test_probability_weighted_fractional_score_is_normalized(self):
        completed, report, calls = self.run_helper(execute=True, mode="fractional")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["status"], "ranked")
        self.assertAlmostEqual(self.entries(report)["a"]["score"], 0.675)
        self.assertEqual(len(calls), 1)

    def test_score_metadata_is_preserved_and_cleared_on_duplicate(self):
        _, report, _ = self.run_helper(execute=True, mode="fractional")
        entry = self.entries(report)["a"]
        self.assertEqual(entry.get("label"), "3")
        self.assertEqual(entry.get("confidence"), 0.8)
        _, report, _ = self.run_helper(execute=True, mode="duplicate")
        entry = self.entries(report)["a"]
        self.assertIsNone(entry.get("label"))
        self.assertIsNone(entry.get("confidence"))

    def test_rounded_live_response_uses_the_returned_score(self):
        completed, report, _ = self.run_helper(execute=True, mode="rounded")
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["status"], "ranked")
        entry = self.entries(report)["a"]
        self.assertAlmostEqual(entry["score"], 3.99 / 4)
        self.assertEqual(entry["label"], "4")
        self.assertEqual(entry["confidence"], 0.99)

    def test_grossly_inconsistent_probability_fields_are_rejected(self):
        for mutation in ("inconsistent_probabilities", "unnormalized_probabilities"):
            with self.subTest(mutation=mutation):
                _, report, _ = self.run_helper(execute=True, mode="invalid_answer",
                    env_extra={"FAKE_JEV_INVALID_ANSWER": mutation})
                self.assertEqual(report["status"], "fallback")
                self.assertEqual(self.entries(report)["a"]["error"]["code"], "invalid_answer")

    def test_rounding_tolerance_has_finite_boundaries(self):
        answer = {"type": "score", "score": 2.05, "value": 2.05,
                  "label": "0", "legend": {str(i): str(i) for i in range(5)},
                  "probabilities": {str(i): 0.205 for i in range(5)}, "confidence": 0.8}
        self.assertTrue(RANK.valid_answer(answer))  # Rounded sum is 1.025.
        answer.update(score=2.052, value=2.052,
                      probabilities={str(i): 0.2052 for i in range(5)})
        self.assertFalse(RANK.valid_answer(answer))  # Sum is 1.026.
        answer.update(score=3.945, value=3.945, label="4",
                      probabilities={str(i): float(i == 4) for i in range(5)})
        self.assertTrue(RANK.valid_answer(answer))
        answer.update(score=3.944, value=3.944)
        self.assertFalse(RANK.valid_answer(answer))

    def test_unpaired_surrogate_snippet_returns_safe_fallback(self):
        # Escaped surrogate is valid JSON syntax but cannot be sent as UTF-8.
        raw = json.dumps(case([candidate(snippet="\ud800")]), ensure_ascii=True)
        completed, report, calls = self.run_helper(raw=raw, execute=True)
        self.assert_no_calls(completed, report, calls)
        self.assertIn("input_invalid", self.diagnostic_codes(report))
        self.assert_not_exposed(completed, "Traceback", "UnicodeEncodeError")

    def test_oversized_response_number_is_an_item_error(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b")]), execute=True,
            mode="invalid_answer", env_extra={"FAKE_JEV_INVALID_ANSWER": "huge_score"})
        self.assertEqual(report["status"], "partial")
        self.assertEqual(self.entries(report)["a"]["status"], "scored")
        self.assertEqual(self.entries(report)["b"]["error"]["code"], "invalid_answer")
        self.assertEqual(len(calls), 1)
        self.assert_not_exposed(completed, "Traceback", "OverflowError")

    def test_stack_and_unavailable_candidates_precede_stable_score_order(self):
        data = case([
            candidate("a", origins=["stack", "rg"]), candidate("b"),
            candidate("c", origins=["call"], local_only=True), candidate("d"),
            candidate("e", origins=["call"]), candidate("f"),
            candidate("g", origins=["stack"]),
        ])
        completed, report, calls = self.run_helper(data, execute=True, mode="mixed", env_extra={
            "FAKE_JEV_ERROR_IDS": '["d"]',
            "FAKE_JEV_SCORES": '{"a":1,"b":4,"e":4,"f":2,"g":0}',
        })
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["investigation_order"], ["a", "g", "c", "d", "b", "e", "f"])
        rows = self.entries(report)
        self.assertTrue(rows["a"]["must_inspect"])
        self.assertTrue(rows["g"]["must_inspect"])
        self.assertFalse(rows["b"]["must_inspect"])
        self.assertEqual(rows["c"]["status"], "local_only")
        self.assertEqual(rows["d"]["status"], "error")
        self.assertEqual(len(calls), 1)

    def test_exit_two_retains_valid_scores_without_automatic_retry(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b")]), execute=True, mode="exit2")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "partial")
        self.assertIn("cli_failed", self.diagnostic_codes(report))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(row["status"] == "scored" for row in report["candidates"]))
        self.assert_not_exposed(completed, UNTRUSTED_ERROR)

    def test_raw_cli_error_is_sanitized_and_success_is_retained(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b")]), execute=True, mode="mixed")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "partial")
        rows = self.entries(report)
        self.assertEqual(rows["a"]["status"], "scored")
        self.assertEqual(rows["b"]["status"], "error")
        self.assertIsNone(rows["b"]["score"])
        self.assertIsNotNone(rows["b"]["error"])
        self.assertEqual(len(calls), 1)
        self.assert_not_exposed(completed, UNTRUSTED_ERROR, "PRIVATE_SOURCE_EXCERPT_b")

    def test_all_cli_errors_fall_back_without_retry(self):
        completed, report, calls = self.run_helper(execute=True, mode="all_errors")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "fallback")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.entries(report)["a"]["status"], "error")
        self.assert_not_exposed(completed, UNTRUSTED_ERROR)

    def test_fatal_missing_credentials_are_recorded_for_every_candidate(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b")]), execute=True, mode="fatal")
        self.assertEqual(report["status"], "fallback")
        self.assertIn("credentials_missing", self.diagnostic_codes(report))
        self.assertTrue(all(row["error"]["code"] == "credentials_missing"
                            for row in report["candidates"]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(report["usage"]["http_attempts_upper_bound"], 4)
        self.assert_not_exposed(completed, "未找到任何 API key")

    def test_each_remote_error_is_classified_without_losing_success(self):
        for raw, expected in [
            ("jev: 未找到 TYPESAFE_API_KEY", "credentials_missing"),
            ("HTTP 401 unauthorized", "authentication_error"),
            ("HTTP 403", "authentication_error"),
            ("HTTP 429", "rate_limited"),
            ("request timed out", "request_timeout"),
            ("connection failed", "network_error"),
        ]:
            with self.subTest(error=expected):
                completed, report, calls = self.run_helper(
                    case([candidate("a"), candidate("b")]), execute=True, mode="mixed",
                    env_extra={"FAKE_JEV_ERROR": raw})
                self.assertEqual(report["status"], "partial")
                self.assertEqual(self.entries(report)["a"]["status"], "scored")
                self.assertEqual(self.entries(report)["b"]["error"]["code"], expected)
                self.assertEqual(len(calls), 1)
                self.assert_not_exposed(completed, raw)

    def test_missing_response_is_an_error_instead_of_zero_score(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b")]), execute=True, mode="missing")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "partial")
        row = self.entries(report)["b"]
        self.assertEqual(row["status"], "error")
        self.assertIsNone(row["score"])
        self.assertEqual(row["error"]["code"], "missing_result")
        self.assertEqual(len(calls), 1)

    def test_malformed_json_line_does_not_discard_valid_result(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b")]), execute=True, mode="malformed")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(self.entries(report)["a"]["status"], "scored")
        self.assertIn("malformed_json", self.diagnostic_codes(report))
        self.assertEqual(len(calls), 1)
        self.assert_not_exposed(completed, "{not-valid-json")

    def test_unidentified_response_is_never_matched_by_position(self):
        for mode in ("unknown", "no_echo", "wrong_state"):
            with self.subTest(mode=mode):
                completed, report, calls = self.run_helper(
                    case([candidate("a"), candidate("b")]), execute=True, mode=mode)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(report["status"], "partial")
                rows = self.entries(report)
                self.assertEqual(rows["a"]["status"], "scored")
                self.assertIsNone(rows["b"]["score"])
                self.assertEqual(rows["b"]["status"], "error")
                self.assertIn("unknown_response", self.diagnostic_codes(report))
                self.assertEqual(len(calls), 1)
                self.assert_not_exposed(completed, "CHANGED_UNTRUSTED_EXCERPT")

    def test_duplicate_responses_invalidate_ambiguous_candidate(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b")]), execute=True, mode="duplicate")
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "partial")
        rows = self.entries(report)
        self.assertEqual(rows["a"]["status"], "scored")
        self.assertEqual(rows["b"]["status"], "error")
        self.assertIsNone(rows["b"]["score"])
        self.assertEqual(len(calls), 1)

    def test_invalid_score_answer_never_becomes_a_rank(self):
        for mutation in ("score_out_of_range", "fractional_score", "nonfinite_score",
                         "wrong_type", "wrong_value", "wrong_label", "missing_answer",
                         "wrong_legend", "bad_probability", "nonfinite_confidence"):
            with self.subTest(mutation=mutation):
                completed, report, calls = self.run_helper(
                    case([candidate("a"), candidate("b")]), execute=True,
                    mode="invalid_answer", env_extra={"FAKE_JEV_INVALID_ANSWER": mutation})
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(report["status"], "partial")
                rows = self.entries(report)
                self.assertEqual(rows["a"]["status"], "scored")
                self.assertIsNone(rows["b"]["score"])
                self.assertEqual(rows["b"]["status"], "error")
                self.assertEqual(len(calls), 1)

    def test_timeout_keeps_flushed_score_and_marks_pending_candidate(self):
        data = case([candidate("a"), candidate("b")])
        states = {item["id"]: {"schema_version": 1, "bug": data["bug"], "candidate": item}
                  for item in data["candidates"]}
        payload = ("".join(json.dumps(state) + "\n" for state in states.values())).encode()
        # A fresh executable copy can cost >150 ms to launch on macOS. Invoke
        # the interpreter directly to test pipe/deadline behavior independently.
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("FAKE_JEV_")}
        environment["FAKE_JEV_MODE"] = "timeout"
        started = time.monotonic()
        with patch.dict(os.environ, environment, clear=True):
            stdout, _, returncode, stop = RANK.run_batch(
                [sys.executable, str(FIXTURE)], payload, 0.5)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        self.assertNotEqual(returncode, 0)
        self.assertEqual(stop, "timeout")
        report = RANK.rank_case(data, SimpleNamespace(
            execute=False, batch_timeout=0.5, request_timeout=10))
        RANK.apply_results(report, states, stdout, stop)
        rows = self.entries(report)
        self.assertEqual(rows["a"]["status"], "scored")
        self.assertIsNone(rows["b"]["score"])
        self.assertEqual(rows["b"]["error"]["code"], "timeout")

    def test_cli_deadline_returns_fallback_or_partial_without_retry(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b")]), execute=True, mode="timeout",
            extra=("--batch-timeout", "0.5"))
        self.assertEqual(completed.returncode, 2)
        self.assertIn(report["status"], {"partial", "fallback"})
        self.assertIn("timeout", self.diagnostic_codes(report))
        self.assertEqual(self.entries(report)["b"]["error"]["code"], "timeout")
        self.assertEqual(len(calls), 1)

    def test_oversized_cli_output_is_bounded_and_sanitized(self):
        completed, report, calls = self.run_helper(
            case([candidate("a"), candidate("b")]), execute=True, mode="output_limit")
        self.assertEqual(completed.returncode, 2)
        self.assertIn(report["status"], {"partial", "fallback"})
        self.assertIn("output_limit", self.diagnostic_codes(report))
        self.assertEqual(len(calls), 1)
        self.assert_not_exposed(completed, "UNTRUSTED_OVERSIZE_RESPONSE_")
        self.assertLess(len(completed.stdout.encode("utf-8")), 10000)

    def test_missing_executable_falls_back_without_os_exception_leak(self):
        completed, report, calls = self.run_helper(
            execute=True, executable=self.work / "missing-sensitive-location")
        self.assert_no_calls(completed, report, calls)
        self.assertTrue(report["diagnostics"])
        self.assert_not_exposed(completed, "missing-sensitive-location", "Traceback",
                                "No such file or directory", "FileNotFoundError")

    def test_unreviewed_input_cannot_execute(self):
        data = case()
        data["reviewed_for_secrets"] = False
        completed, report, calls = self.run_helper(data, execute=True)
        self.assert_no_calls(completed, report, calls)
        self.assert_not_exposed(completed, "PRIVATE_SOURCE_EXCERPT_a")

    def test_more_than_twelve_candidates_fall_back_with_all_metadata(self):
        data = case([candidate("c" + str(i)) for i in range(13)])
        completed, report, calls = self.run_helper(data, execute=True)
        self.assert_no_calls(completed, report, calls)
        self.assert_metadata(report, data)
        self.assertEqual(report["investigation_order"], [item["id"] for item in data["candidates"]])

    def test_twelve_candidates_use_one_batch_and_reserve_two_attempts_each(self):
        data = case([candidate("c" + str(i)) for i in range(12)])
        completed, report, calls = self.run_helper(data, execute=True)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(report["status"], "ranked")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]["states"]), 12)
        self.assertEqual(report["usage"]["http_attempts_upper_bound"], 24)

    def test_explicit_local_only_candidate_is_not_transmitted(self):
        data = case([candidate("a", local_only=True), candidate("b")])
        completed, report, calls = self.run_helper(data, execute=True)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(self.entries(report)["a"]["status"], "local_only")
        self.assertIsNone(self.entries(report)["a"]["score"])
        self.assertEqual([state["candidate"]["id"] for state in calls[0]["states"]], ["b"])
        self.assertEqual(report["usage"]["submitted_candidates"], 1)
        self.assert_not_exposed(completed, "PRIVATE_SOURCE_EXCERPT_a")

    def test_overlong_snippets_stay_local_without_truncation(self):
        for snippet in ("x" * 2049, "界" * 683, "\n".join(["x"] * 61)):
            with self.subTest(snippet_bytes=len(snippet.encode("utf-8"))):
                data = case([candidate("a", snippet=snippet), candidate("b")])
                completed, report, calls = self.run_helper(data, execute=True)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(report["status"], "partial")
                self.assertEqual(self.entries(report)["a"]["status"], "local_only")
                self.assertEqual([state["candidate"]["id"] for state in calls[0]["states"]], ["b"])
                self.assertEqual(calls[0]["states"][0]["candidate"]["snippet"],
                                 data["candidates"][1]["snippet"])

    def test_exact_snippet_limits_are_submitted_unchanged(self):
        for snippet in ("界" * 682 + "ab", "\n".join(["x"] * 60)):
            with self.subTest(snippet_bytes=len(snippet.encode("utf-8"))):
                completed, report, calls = self.run_helper(
                    case([candidate(snippet=snippet)]), execute=True)
                self.assertEqual(completed.returncode, 0)
                self.assertEqual(self.entries(report)["a"]["status"], "scored")
                self.assertEqual(calls[0]["states"][0]["candidate"]["snippet"], snippet)

    def test_repeated_bug_payload_limit_prevents_any_partial_submission(self):
        data = case([candidate("c" + str(i), snippet="x" * 2048) for i in range(12)])
        self.assertLess(len(json.dumps(data).encode("utf-8")), 65536)
        completed, report, calls = self.run_helper(data, execute=True)
        self.assert_no_calls(completed, report, calls)
        self.assert_metadata(report, data)
        self.assert_not_exposed(completed, "x" * 200)

    def test_input_larger_than_sixty_four_kib_cannot_execute(self):
        data = case([candidate(snippet="PRIVATE_OVERSIZE_INPUT_" + "x" * 66000)])
        completed, report, calls = self.run_helper(data, execute=True)
        self.assert_no_calls(completed, report, calls)
        self.assert_not_exposed(completed, "PRIVATE_OVERSIZE_INPUT_", "x" * 200)

    def test_sensitive_candidate_paths_are_local_only(self):
        for path in (".env", "config/.env.production", ".aws/credentials",
                     "secrets/id_rsa", "secrets/server.key"):
            with self.subTest(path=path):
                data = case([candidate("a", path=path), candidate("b")])
                completed, report, calls = self.run_helper(data, execute=True)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(report["status"], "partial")
                self.assertEqual(self.entries(report)["a"]["status"], "local_only")
                self.assertEqual([state["candidate"]["id"] for state in calls[0]["states"]], ["b"])

    def test_obvious_secrets_anywhere_in_candidate_stay_local(self):
        secrets = [
            'api_key = "super-secret-key"', 'password = "super-secret-password"',
            "Authorization: Bearer super-secret-token",
            "-----BEGIN PRIVATE KEY-----\nsuper-secret-key\n-----END PRIVATE KEY-----",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJlMTIzNDU2",
            "postgresql://alice:super-secret-password@database.example/test",
        ]
        for secret in secrets:
            with self.subTest(secret_type=secret.split()[0]):
                completed, report, calls = self.run_helper(
                    case([candidate("a", snippet=secret), candidate("b")]), execute=True)
                self.assertEqual(completed.returncode, 2)
                self.assertEqual(report["status"], "partial")
                self.assertEqual(self.entries(report)["a"]["status"], "local_only")
                self.assertEqual([state["candidate"]["id"] for state in calls[0]["states"]], ["b"])
                self.assert_not_exposed(completed, secret)

    def test_secret_in_any_bug_field_blocks_the_entire_batch(self):
        field_locations = ["description", "steps", "expected", "actual", "stack_trace"]
        for location in field_locations:
            with self.subTest(location=location):
                data = case()
                secret = "Authorization: Bearer bug-secret-token"
                if location == "description":
                    data["bug"][location] = secret
                elif location == "stack_trace":
                    data["bug"][location] = [secret]
                elif location == "steps":
                    data["bug"]["reproduction"][location] = [secret]
                else:
                    data["bug"]["reproduction"][location] = secret
                completed, report, calls = self.run_helper(data, execute=True)
                self.assert_no_calls(completed, report, calls)
                self.assert_not_exposed(completed, "bug-secret-token")

    def test_unknown_fields_and_invalid_schema_fail_closed(self):
        bad_cases = []
        for location in ("root", "bug", "reproduction", "candidate"):
            data = case()
            target = {"root": data, "bug": data["bug"],
                      "reproduction": data["bug"]["reproduction"],
                      "candidate": data["candidates"][0]}[location]
            target["unknown"] = "UNTRUSTED_UNKNOWN_FIELD_VALUE"
            bad_cases.append((location, data))
        for key, value in [("schema_version", 2), ("schema_version", True),
                           ("reviewed_for_secrets", "true")]:
            data = case()
            data[key] = value
            bad_cases.append((key + repr(value), data))
        for name, data in bad_cases:
            with self.subTest(name=name):
                completed, report, calls = self.run_helper(data, execute=True)
                self.assert_no_calls(completed, report, calls)
                self.assert_not_exposed(completed, "UNTRUSTED_UNKNOWN_FIELD_VALUE")

    def test_duplicate_ids_invalid_paths_and_nonfinite_values_fail_closed(self):
        invalid = [
            case([candidate("a"), candidate("a")]),
            case([candidate(path="/absolute/source.py")]),
            case([candidate(path="../outside.py")]),
            case([candidate(path="src/../outside.py")]),
            case([candidate(path="C:\\private\\source.py")]),
            case([candidate(start_line=0)]),
            case([candidate(start_line=2, end_line=1)]),
            case([candidate(start_line=float("nan"))]),
            case([candidate(end_line=float("inf"))]),
            case([candidate(origins=["invented"])]),
            case([candidate(local_only="false")]),
        ]
        for index, data in enumerate(invalid):
            with self.subTest(index=index):
                completed, report, calls = self.run_helper(data, execute=True)
                self.assert_no_calls(completed, report, calls)
                self.assert_not_exposed(completed, "PRIVATE_SOURCE_EXCERPT_a", "Traceback")

    def test_malformed_input_json_falls_back_without_echoing_it(self):
        completed, report, calls = self.run_helper(
            execute=True, raw='{"PRIVATE_MALFORMED_INPUT": this is not JSON}')
        self.assert_no_calls(completed, report, calls)
        self.assert_not_exposed(completed, "PRIVATE_MALFORMED_INPUT", "Traceback")

    def test_cli_timeout_bounds_reject_invalid_values_before_any_call(self):
        input_path = self.work / "case.json"
        input_path.write_text(json.dumps(case()), encoding="utf-8")
        for flag, value in [("--batch-timeout", "0"), ("--batch-timeout", "45.01"),
                            ("--batch-timeout", "nan"), ("--batch-timeout", "inf"),
                            ("--request-timeout", "-1"), ("--request-timeout", "10.01"),
                            ("--request-timeout", "nan")]:
            with self.subTest(flag=flag, value=value):
                completed = subprocess.run([
                    sys.executable, str(HELPER), "--input", str(input_path),
                    "--execute", "--jev", str(self.fake), flag, value,
                ], capture_output=True, text=True, timeout=8, check=False,
                    env={**os.environ, "FAKE_JEV_LOG": str(self.log)})
                self.assertEqual(completed.returncode, 2)
                self.assertFalse(self.log.exists())
                self.assertNotIn("can't open file", completed.stderr)


if __name__ == "__main__":
    unittest.main()
