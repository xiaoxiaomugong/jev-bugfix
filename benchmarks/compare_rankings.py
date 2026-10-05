#!/usr/bin/env python3
"""Compare frozen pools offline; C is an imported, unchanged V1 report."""
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "benchmark_v1_ranker", ROOT / "skills/jev-bugfix/scripts/rank_candidates.py")
V1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V1)
ARMS = ("A", "B", "C")
HASH_FIELDS = ("evidence_hash", "candidate_hash", "frozen_hash")
METRICS = ("hit_at_1", "hit_at_3", "mrr")
LEXICAL_RULE = "unicode-token-overlap-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def fields(value, required, optional=()):
    require(isinstance(value, dict), "record must be an object")
    require(set(required) <= set(value), "record has missing fields")
    require(not (set(value) - set(required) - set(optional)), "record has unknown fields")


def nonempty(value):
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value


def canonical_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def freeze_hashes(ranker_input):
    return {"evidence_hash": canonical_hash(ranker_input["bug"]),
            "candidate_hash": canonical_hash(ranker_input["candidates"]),
            "frozen_hash": canonical_hash(ranker_input)}


def validate_case(case):
    fields(case, ("schema_version", "case_id", "development_fixture", "pool_provenance",
                  "repo_revision", "ranker_revision", "ranker_input", "relevant_candidate_ids")
           + HASH_FIELDS, ("fixture_category",))
    require(type(case["schema_version"]) is int and case["schema_version"] == 1,
            "unsupported benchmark schema")
    require(all(nonempty(case[key]) for key in ("case_id", "repo_revision", "ranker_revision")),
            "case identity and revisions must be nonempty")
    require(type(case["development_fixture"]) is bool, "fixture flag must be boolean")
    provenance = case["pool_provenance"]
    fields(provenance, ("kind", "collector_blind_to_answer", "annotation_source"))
    require(provenance["kind"] in ("constructed", "blind_collected"), "unknown pool provenance")
    require(type(provenance["collector_blind_to_answer"]) is bool
            and nonempty(provenance["annotation_source"]), "invalid provenance")
    if provenance["kind"] == "blind_collected":
        require(provenance["collector_blind_to_answer"], "blind collection must be declared")
    else:
        require(case["development_fixture"], "constructed pools must be development fixtures")
    if "fixture_category" in case:
        require(case["development_fixture"] and nonempty(case["fixture_category"]),
                "fixture category requires a development fixture")
    try:
        V1.validate_case(case["ranker_input"])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError("ranker_input violates the V1 input contract") from None
    require(len(case["ranker_input"]["candidates"]) <= V1.MAX_CANDIDATES,
            "fixed pool exceeds the V1 candidate limit")
    for key, expected in freeze_hashes(case["ranker_input"]).items():
        require(case[key] == expected, "frozen input hash mismatch: " + key)
    identifiers = {item["id"] for item in case["ranker_input"]["candidates"]}
    relevant = case["relevant_candidate_ids"]
    require(isinstance(relevant, list) and all(isinstance(item, str) for item in relevant),
            "relevance annotation must be an ID list")
    require(len(set(relevant)) == len(relevant) and set(relevant) <= identifiers,
            "relevance annotation has duplicate or unknown IDs")


def validate_c_report(case, envelope):
    fields(envelope, ("schema_version", "case_id", "repo_revision", "ranker_revision",
                      "source", "jev_version", "report") + HASH_FIELDS)
    for key in ("schema_version", "case_id", "repo_revision", "ranker_revision") + HASH_FIELDS:
        require(type(envelope[key]) is type(case[key]) and envelope[key] == case[key],
                "C report does not match frozen case: " + key)
    require(envelope["source"] in ("fake", "jev"), "unknown C source")
    require(envelope["jev_version"] is None or nonempty(envelope["jev_version"]),
            "invalid Jev version")
    require(envelope["source"] != "fake" or case["development_fixture"],
            "fake C output must remain a development fixture")
    report = envelope["report"]
    # Accept compatible V1 extensions without copying or reconstructing its ranking.
    require(isinstance(report, dict) and type(report.get("schema_version")) is int
            and report["schema_version"] == 1, "C report must use V1 schema")
    require(report.get("status") in ("ranked", "partial", "fallback", "dry_run"), "invalid C status")
    if envelope["source"] == "jev" and report["status"] in ("ranked", "partial"):
        require(envelope["jev_version"] == "0.3.2", "real scored C report requires verified Jev 0.3.2")
    require(envelope["source"] != "jev" or report["status"] != "dry_run",
            "dry-run output cannot be labeled as a real Jev report")
    require(isinstance(report.get("diagnostics"), list) and isinstance(report.get("usage"), dict),
            "C report lacks diagnostics or usage")
    candidates = case["ranker_input"]["candidates"]
    rows = report.get("candidates")
    require(isinstance(rows, list) and len(rows) == len(candidates), "C candidate pool differs")
    for candidate, row in zip(candidates, rows):
        require(isinstance(row, dict), "invalid C candidate row")
        for key in ("id", "path", "start_line", "end_line", "origins"):
            require(type(row.get(key)) is type(candidate[key]) and row[key] == candidate[key],
                    "C candidate metadata differs: " + key)
        require(type(row.get("must_inspect")) is bool
                and row["must_inspect"] == ("stack" in candidate["origins"]), "C stack marker differs")
        require(row.get("status") in ("pending", "scored", "local_only", "error"), "invalid C row status")
        score = row.get("score")
        if row["status"] == "scored":
            require(type(score) in (int, float) and 0 <= score <= 1 and math.isfinite(score),
                    "invalid C score")
            require(row.get("label") in ("0", "1", "2", "3", "4"), "invalid C label")
            confidence = row.get("confidence")
            require(type(confidence) in (int, float) and 0 <= confidence <= 1
                    and math.isfinite(confidence) and row.get("error") is None, "invalid C confidence")
        else:
            require(all(key in row and row[key] is None for key in ("score", "label", "confidence")),
                    "unscored C candidate has a score")
    scored = sum(row["status"] == "scored" for row in rows)
    require(report["status"] != "ranked" or scored == len(rows), "ranked C report lacks scores")
    require(report["status"] != "partial" or scored > 0, "partial C report lacks scores")
    require(report["status"] not in ("fallback", "dry_run") or scored == 0,
            "fallback/dry-run C report has scores")
    order = report.get("investigation_order")
    identifiers = [item["id"] for item in candidates]
    require(isinstance(order, list) and all(isinstance(item, str) for item in order)
            and len(order) == len(identifiers) and set(order) == set(identifiers),
            "C investigation_order must be an exact pool permutation")


def tokens(value):
    # Split ASCII camelCase before case folding; underscore/punctuation split below.
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return set(re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE))


def baseline_orders(ranker_input):
    bug = ranker_input["bug"]
    reproduction = bug["reproduction"]
    query = tokens("\n".join([bug["description"], reproduction["expected"],
        reproduction["actual"]] + reproduction["steps"] + bug["stack_trace"]))
    candidates = ranker_input["candidates"]
    stack = [item["id"] for item in candidates if "stack" in item["origins"]]
    other = [item for item in candidates if "stack" not in item["origins"]]
    lexical = sorted(other, key=lambda item: -len(query & tokens(item["path"] + "\n" + item["snippet"])))
    return {"A": stack + [item["id"] for item in other],
            "B": stack + [item["id"] for item in lexical]}


def ranking_metrics(order, candidates, relevant):
    snippets = {item["id"]: len(item["snippet"].encode("utf-8")) for item in candidates}
    first, read_bytes = None, 0
    for rank, identifier in enumerate(order, 1):
        read_bytes += snippets[identifier]
        if identifier in relevant:
            first = rank
            break
    return {"order": list(order), "first_relevant_rank": first,
            "hit_at_1": int(first is not None and first <= 1),
            "hit_at_3": int(first is not None and first <= 3),
            "mrr": 1 / first if first is not None else 0.0,
            "simulated_bytes_to_first_relevant": read_bytes,
            "read_stopped_at_relevant": first is not None}


def compare_case(case, envelope):
    validate_case(case)
    validate_c_report(case, envelope)
    orders = baseline_orders(case["ranker_input"])
    orders["C"] = envelope["report"]["investigation_order"]
    relevant = set(case["relevant_candidate_ids"])
    arms = {arm: ranking_metrics(order, case["ranker_input"]["candidates"], relevant)
            for arm, order in orders.items()}
    return {"case_id": case["case_id"], "development_fixture": case["development_fixture"],
            "fixture_category": case.get("fixture_category"), "covered": bool(relevant),
            "repo_revision": case["repo_revision"], "ranker_revision": case["ranker_revision"],
            "pool_provenance": case["pool_provenance"], "c_source": envelope["source"],
            "jev_version": envelope["jev_version"], "c_status": envelope["report"]["status"],
            "relevant_candidate_ids": case["relevant_candidate_ids"],
            "hashes": {key: case[key] for key in HASH_FIELDS}, "arms": arms,
            "order_changed": {arm: orders[arm] != orders["A"] for arm in ("B", "C")},
            "improvement_opportunity": bool(relevant) and arms["A"]["first_relevant_rank"] > 1}


def average_metrics(rows):
    return {arm: {key: sum(row["arms"][arm][key] for row in rows) / len(rows) if rows else None
                  for key in METRICS} for arm in ARMS}


def index_records(records):
    require(isinstance(records, list), "records must be a list")
    indexed = {}
    for record in records:
        require(isinstance(record, dict) and nonempty(record.get("case_id")), "missing case ID")
        require(record["case_id"] not in indexed, "duplicate case ID")
        indexed[record["case_id"]] = record
    return indexed


def compare_cases(cases, c_reports):
    case_index, report_index = index_records(cases), index_records(c_reports)
    require(case_index and set(case_index) == set(report_index), "cases and C reports must pair exactly")
    rows = [compare_case(case, report_index[identifier]) for identifier, case in case_index.items()]
    covered = [row for row in rows if row["covered"]]
    return {"schema_version": 1, "report_kind": "fixed_pool_rankings",
            "development_fixture": any(row["development_fixture"] for row in rows),
            "fixture_cases": sum(row["development_fixture"] for row in rows),
            "lexical_rule": LEXICAL_RULE,
            "coverage": {"covered": len(covered), "total": len(rows), "rate": len(covered) / len(rows)},
            "overall": average_metrics(rows), "in_pool": average_metrics(covered),
            "improvement_opportunity_cases": sum(row["improvement_opportunity"] for row in rows),
            "order_changed_cases": {arm: sum(row["order_changed"][arm] for row in rows) for arm in ("B", "C")},
            "cases": rows,
            "interpretation": "Fixed-pool ranking and simulated snippet bytes only; no measured repair, reading, time or cost benefit."}


def load_records(path):
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [V1.strict_json(line) for line in raw.splitlines() if line.strip()]
    value = V1.strict_json(raw)
    require(isinstance(value, (dict, list)), "JSON input must be an object or array")
    return value if isinstance(value, list) else [value]


def markdown_report(report):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

    def decimal(value):
        return "unavailable" if value is None else "%.4f" % value

    lines = ["# Fixed-pool ranking comparison", ""]
    if report["development_fixture"]:
        lines += ["**Development fixtures included: %s/%s cases. These are tool tests, not real benefit evidence.**" %
                  (report["fixture_cases"], report["coverage"]["total"]), ""]
    lines += [report["interpretation"], "", "Candidate coverage: %s/%s. Improvement opportunity: %s cases." %
              (report["coverage"]["covered"], report["coverage"]["total"], report["improvement_opportunity_cases"]),
              "", "| Population | Arm | Hit@1 | Hit@3 | MRR |", "| --- | --- | ---: | ---: | ---: |"]
    for population in ("overall", "in_pool"):
        for arm in ARMS:
            metrics = report[population][arm]
            lines.append("| %s | %s | %s | %s | %s |" %
                         (population, arm, *(decimal(metrics[key]) for key in METRICS)))
    lines += ["", "| Case | Fixture | C source/status | Arm | Order | First relevant rank | Simulated bytes |",
              "| --- | --- | --- | --- | --- | ---: | ---: |"]
    for row in report["cases"]:
        for arm in ARMS:
            metrics = row["arms"][arm]
            lines.append("| %s | %s | %s/%s | %s | %s | %s | %s |" %
                (cell(row["case_id"]), row["development_fixture"], row["c_source"], row["c_status"], arm,
                 cell(", ".join(metrics["order"])), metrics["first_relevant_rank"] if metrics["first_relevant_rank"] is not None else "miss",
                 metrics["simulated_bytes_to_first_relevant"]))
    lines += ["", "Misses read the whole pool in this simulation. Multiple labels mark relevant evidence; reaching one does not prove a complete diagnosis.", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, help="Case JSON object/array or .jsonl")
    parser.add_argument("--c-reports", required=True, help="Bound C report JSON object/array or .jsonl")
    parser.add_argument("--json-output", help="Write JSON here; otherwise stdout")
    parser.add_argument("--markdown-output", help="Optional Markdown report path")
    args = parser.parse_args()
    try:
        report = compare_cases(load_records(args.cases), load_records(args.c_reports))
        output = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.json_output:
            Path(args.json_output).write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        if args.markdown_output:
            Path(args.markdown_output).write_text(markdown_report(report), encoding="utf-8")
    except (ValueError, OSError, UnicodeError, TypeError, RecursionError) as error:
        # Raw evidence and filesystem errors are never echoed by this CLI.
        print("Benchmark input/output rejected (%s)." % type(error).__name__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
