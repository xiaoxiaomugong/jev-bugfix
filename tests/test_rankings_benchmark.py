"""Offline fixed-pool comparisons; expectations are hand-calculated."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "benchmarks" / "compare_rankings.py"


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def candidate(identifier, snippet, origins=None):
    return {"id": identifier, "path": "src/sample.py", "start_line": 1,
            "end_line": len(snippet.splitlines()), "snippet": snippet,
            "origins": origins or ["rg"]}


def case_and_report(candidates=None, relevant=None, order=None):
    candidates = candidates or [candidate("c1", "unrelated"),
                                 candidate("c2", "cache"),
                                 candidate("c3", "cache expired")]
    data = {"schema_version": 1, "reviewed_for_secrets": True,
            "bug": {"description": "cache expired", "reproduction": {
                "steps": ["reproduce"], "expected": "fresh", "actual": "stale"},
                "stack_trace": []}, "candidates": candidates}
    case = {"schema_version": 1, "case_id": "sample", "development_fixture": True,
            "pool_provenance": {"kind": "constructed", "collector_blind_to_answer": False,
                                "annotation_source": "offline hand annotation"},
            "repo_revision": "fixture-revision", "ranker_revision": "fixture-ranker",
            "ranker_input": data, "evidence_hash": digest(data["bug"]),
            "candidate_hash": digest(candidates), "frozen_hash": digest(data),
            "relevant_candidate_ids": ["c3"] if relevant is None else relevant}
    rows = []
    for item in candidates:
        row = {key: item[key] for key in ("id", "path", "start_line", "end_line", "origins")}
        row.update(must_inspect="stack" in item["origins"], score=0.5, label="2",
                   confidence=0.5, status="scored", error=None)
        rows.append(row)
    report = {"schema_version": 1, "status": "ranked", "candidates": rows,
              "investigation_order": order or [item["id"] for item in candidates],
              "diagnostics": [], "usage": {"cli_invocations": 1,
                  "submitted_candidates": len(candidates), "http_attempts_upper_bound": 2 * len(candidates),
                  "payload_bytes": 1, "batch_timeout_seconds": 45, "request_timeout_seconds": 10}}
    envelope = {key: case[key] for key in ("schema_version", "case_id", "repo_revision",
                "ranker_revision", "evidence_hash", "candidate_hash", "frozen_hash")}
    envelope.update(source="fake", jev_version="0.3.2", report=report)
    return case, envelope


class RankingsBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SCRIPT.is_file():
            spec = importlib.util.spec_from_file_location("rankings_benchmark", SCRIPT)
            cls.benchmark = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.benchmark)
        else:
            cls.benchmark = None

    def compare(self, cases, reports):
        self.assertIsNotNone(self.benchmark, "fixed-pool comparator is not implemented")
        return self.benchmark.compare_cases(cases, reports)

    def test_baselines_and_c_order_have_exact_simulated_utf8_bytes(self):
        case, report = case_and_report(order=["c3", "c1", "c2"])
        result = self.compare([case], [report])
        arms = result["cases"][0]["arms"]
        self.assertEqual(arms["A"]["order"], ["c1", "c2", "c3"])
        self.assertEqual(arms["B"]["order"], ["c3", "c2", "c1"])
        self.assertEqual(arms["C"]["order"], ["c3", "c1", "c2"])
        self.assertEqual(arms["A"]["simulated_bytes_to_first_relevant"], 27)
        self.assertEqual(arms["B"]["simulated_bytes_to_first_relevant"], 13)
        self.assertEqual(arms["A"]["mrr"], 1 / 3)
        self.assertEqual(arms["C"]["hit_at_1"], 1)
        self.assertTrue(result["development_fixture"])

    def test_stack_prefix_and_lexical_ties_retain_original_order(self):
        items = [candidate("c1", "cache"), candidate("c2", "unused", ["stack"]),
                 candidate("c3", "cache"), candidate("c4", "unused", ["stack"])]
        case, report = case_and_report(items, order=["c2", "c4", "c1", "c3"])
        arms = self.compare([case], [report])["cases"][0]["arms"]
        self.assertEqual(arms["A"]["order"], ["c2", "c4", "c1", "c3"])
        self.assertEqual(arms["B"]["order"], ["c2", "c4", "c1", "c3"])

    def test_missing_root_cause_counts_as_miss_overall_and_subset_is_null(self):
        case, report = case_and_report(relevant=[])
        result = self.compare([case], [report])
        self.assertEqual(result["coverage"], {"covered": 0, "total": 1, "rate": 0.0})
        self.assertEqual(result["overall"]["C"], {"hit_at_1": 0.0, "hit_at_3": 0.0, "mrr": 0.0})
        self.assertEqual(result["in_pool"]["C"], {"hit_at_1": None, "hit_at_3": None, "mrr": None})
        metric = result["cases"][0]["arms"]["C"]
        self.assertIsNone(metric["first_relevant_rank"])
        self.assertEqual(metric["simulated_bytes_to_first_relevant"], 27)

    def test_multiple_relevant_candidates_use_first_encountered_candidate(self):
        case, report = case_and_report(relevant=["c2", "c3"], order=["c3", "c2", "c1"])
        arms = self.compare([case], [report])["cases"][0]["arms"]
        self.assertEqual(arms["A"]["first_relevant_rank"], 2)
        self.assertEqual(arms["A"]["simulated_bytes_to_first_relevant"], 14)
        self.assertEqual(arms["C"]["first_relevant_rank"], 1)

    def test_utf8_bytes_are_not_character_counts(self):
        case, report = case_and_report([candidate("c1", "界"), candidate("c2", "é")],
                                      relevant=["c2"], order=["c1", "c2"])
        metric = self.compare([case], [report])["cases"][0]["arms"]["A"]
        self.assertEqual(metric["simulated_bytes_to_first_relevant"], 5)

    def test_c_order_is_consumed_directly_instead_of_recomputed_from_scores(self):
        case, report = case_and_report(order=["c3", "c2", "c1"])
        metric = self.compare([case], [report])["cases"][0]["arms"]["C"]
        self.assertEqual(metric["order"], ["c3", "c2", "c1"])

    def test_hash_content_and_revision_mismatches_are_rejected(self):
        self.assertIsNotNone(self.benchmark, "fixed-pool comparator is not implemented")
        for field in ("repo_revision", "ranker_revision", "candidate_hash", "evidence_hash", "frozen_hash"):
            with self.subTest(field=field):
                case, report = case_and_report()
                report[field] = "wrong"
                with self.assertRaises(ValueError):
                    self.benchmark.compare_cases([case], [report])
        for field in ("reviewed_for_secrets", "local_only", "snippet"):
            with self.subTest(content=field):
                case, report = case_and_report()
                if field == "reviewed_for_secrets":
                    case["ranker_input"][field] = False
                else:
                    case["ranker_input"]["candidates"][0][field] = True if field == "local_only" else "changed"
                with self.assertRaises(ValueError):
                    self.benchmark.compare_cases([case], [report])

    def test_unknown_version_only_allowed_for_real_fallback(self):
        self.assertIsNotNone(self.benchmark, "fixed-pool comparator is not implemented")
        case, report = case_and_report()
        report.update(source="jev", jev_version=None)
        with self.assertRaises(ValueError):
            self.benchmark.compare_cases([case], [report])
        report["report"]["status"] = "fallback"
        for row in report["report"]["candidates"]:
            row.update(status="error", score=None, label=None, confidence=None,
                       error={"code": "cli_version_unknown", "message": "version unverified"})
        result = self.benchmark.compare_cases([case], [report])
        self.assertEqual(result["cases"][0]["c_status"], "fallback")
        self.assertTrue(result["development_fixture"])

    def test_c_metadata_permutation_and_annotation_integrity_are_required(self):
        self.assertIsNotNone(self.benchmark, "fixed-pool comparator is not implemented")
        for mutation in ("duplicate_order", "missing_candidate", "different_lines", "wrong_stack", "unknown_label"):
            with self.subTest(mutation=mutation):
                case, report = case_and_report()
                if mutation == "duplicate_order":
                    report["report"]["investigation_order"] = ["c1", "c1", "c3"]
                elif mutation == "missing_candidate":
                    report["report"]["candidates"].pop()
                elif mutation == "different_lines":
                    report["report"]["candidates"][0]["start_line"] = 2
                elif mutation == "wrong_stack":
                    report["report"]["candidates"][0]["must_inspect"] = True
                else:
                    case["relevant_candidate_ids"] = ["outside_pool"]
                with self.assertRaises(ValueError):
                    self.benchmark.compare_cases([case], [report])

    def test_duplicate_or_unpaired_cases_and_reports_are_rejected(self):
        self.assertIsNotNone(self.benchmark, "fixed-pool comparator is not implemented")
        case, report = case_and_report()
        for cases, reports in (([case, case], [report]), ([case], [report, report]),
                               ([case], []), ([], [report])):
            with self.subTest(cases=len(cases), reports=len(reports)):
                with self.assertRaises(ValueError):
                    self.benchmark.compare_cases(cases, reports)

    def test_constructed_and_fake_sources_cannot_be_promoted_to_real_evidence(self):
        self.assertIsNotNone(self.benchmark, "fixed-pool comparator is not implemented")
        case, report = case_and_report()
        case["development_fixture"] = False
        with self.assertRaises(ValueError):
            self.benchmark.compare_cases([case], [report])

    def test_all_six_fixture_categories_match_hand_checked_expected_metrics(self):
        self.assertIsNotNone(self.benchmark, "fixed-pool comparator is not implemented")
        fixtures = ROOT / "benchmarks" / "fixtures"
        cases = self.benchmark.load_records(fixtures / "ranking-cases.jsonl")
        reports = self.benchmark.load_records(fixtures / "ranking-c-reports.jsonl")
        result = self.benchmark.compare_cases(cases, reports)
        expected = json.loads((fixtures / "ranking-expected.json").read_text(encoding="utf-8"))
        self.assertEqual({row["fixture_category"] for row in result["cases"]},
                         {"improvement", "first_root", "missing_root", "wrong_order", "fallback", "multiple_relevant"})
        for row in result["cases"]:
            for arm, wanted in expected[row["case_id"]].items():
                for metric, value in wanted.items():
                    with self.subTest(case=row["case_id"], arm=arm, metric=metric):
                        self.assertEqual(row["arms"][arm][metric], value)

    def test_cli_reads_json_and_jsonl_and_writes_json_and_markdown(self):
        self.assertIsNotNone(self.benchmark, "fixed-pool comparator is not implemented")
        case, report = case_and_report(order=["c3", "c1", "c2"])
        with tempfile.TemporaryDirectory() as temp:
            work = Path(temp)
            (work / "case.json").write_text(json.dumps(case), encoding="utf-8")
            (work / "report.jsonl").write_text(json.dumps(report) + "\n", encoding="utf-8")
            completed = subprocess.run([sys.executable, "-B", str(SCRIPT), "--cases",
                str(work / "case.json"), "--c-reports", str(work / "report.jsonl"),
                "--json-output", str(work / "out.json"), "--markdown-output", str(work / "out.md")],
                capture_output=True, text=True, check=False, timeout=8)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = json.loads((work / "out.json").read_text(encoding="utf-8"))
            self.assertEqual(output["coverage"]["total"], 1)
            rendered = (work / "out.md").read_text(encoding="utf-8")
            self.assertIn("Development fixtures", rendered)
            self.assertIn("simulated", rendered.lower())
            self.assertIn("sample", rendered)

    def test_json_duplicate_keys_and_nonfinite_numbers_are_rejected(self):
        self.assertIsNotNone(self.benchmark, "fixed-pool comparator is not implemented")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "invalid.json"
            for content in ('{"schema_version":1,"schema_version":2}', '{"score":NaN}'):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.benchmark.load_records(path)


if __name__ == "__main__":
    unittest.main()
