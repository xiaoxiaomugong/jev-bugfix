#!/usr/bin/env python3
"""Import normalized local evidence; no agent execution, API calls or token estimates."""
import argparse
from collections import defaultdict
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import sys

PHASES = ("collection", "preflight", "scoring", "investigation", "repair", "tests")
CONFIG = ("model", "reasoning", "prompt_hash", "tools_hash", "environment_hash", "budget_hash")
COSTS = ("main_model", "jev", "review")
JEV_METRICS = {"cli_invocations": "count", "preflight_invocations": "count",
               "submitted_candidates": "count", "payload_bytes": "bytes",
               "http_attempts_upper_bound": "count"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def string(value):
    return isinstance(value, str) and bool(value.strip())


def evidence(value):
    require(isinstance(value, list) and all(string(item) for item in value), "invalid evidence paths")
    return value


def timestamp(value):
    require(string(value), "missing timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid ISO timestamp") from None
    require(result.tzinfo is not None, "timestamp needs timezone")
    return result


def validate_metric(item, unit):
    require(isinstance(item, dict), "metric must be an object")
    required = {"value", "unit", "kind", "source", "evidence", "completeness", "reason"}
    require(required <= item.keys() and item["unit"] == unit, "metric fields or unit mismatch")
    require(item["kind"] in ("measured", "derived", "estimated", "unavailable"), "invalid metric kind")
    require(item["completeness"] in ("complete", "lower_bound", "unavailable"), "invalid completeness")
    evidence(item["evidence"])
    if item["value"] is None:
        require(item["kind"] == "unavailable" and item["completeness"] == "unavailable"
                and string(item["reason"]), "missing metric needs unavailable kind and reason")
    else:
        value = item["value"]
        require(type(value) in (int, float) and value >= 0 and value <= 1e18
                and math.isfinite(value), "metric needs finite nonnegative number")
        if unit in ("count", "bytes", "tokens", "lines"):
            require(type(value) is int, "discrete metric must be integer")
        require(item["kind"] != "unavailable" and item["completeness"] != "unavailable"
                and string(item["source"]) and bool(item["evidence"]), "metric needs source and evidence")
        if item["completeness"] == "lower_bound":
            require(string(item["reason"]), "lower bound needs reason")
        if item["kind"] == "estimated":
            price = item.get("price")
            require(isinstance(price, dict) and all(string(price.get(k)) for k in
                    ("date", "source", "basis")), "estimate needs dated price source and basis")
            try:
                datetime.strptime(price["date"], "%Y-%m-%d")
            except ValueError:
                raise ValueError("invalid price date") from None
    return item


def unavailable(unit, reason):
    return {"value": None, "unit": unit, "kind": "unavailable", "source": None,
            "evidence": [], "completeness": "unavailable", "reason": reason}


def derived(value, unit, source, paths, completeness="complete", reason=None):
    return {"value": value, "unit": unit, "kind": "derived", "source": source,
            "evidence": list(dict.fromkeys(paths)), "completeness": completeness, "reason": reason}


def validate_run(record):
    require(isinstance(record, dict) and type(record.get("schema_version")) is int
            and record["schema_version"] == 1, "invalid run schema")
    for key in ("case_id", "run_id", "pair_id"):
        require(string(record.get(key)), "missing " + key)
    require(record.get("arm") in ("A", "C"), "invalid arm")
    require(type(record.get("development_fixture")) is bool, "fixture label required")
    repo, config = record.get("repository"), record.get("config")
    require(isinstance(repo, dict) and all(string(repo.get(k)) for k in ("url", "revision")), "repository required")
    require(isinstance(config, dict) and all(string(config.get(k)) for k in CONFIG), "configuration required")
    require("candidate_hash" in record and (record["candidate_hash"] is None
            or string(record["candidate_hash"])), "candidate hash required, nullable for A")
    timing = record.get("timing")
    require(isinstance(timing, dict), "timing required")
    start = timestamp(timing.get("started_at"))
    finish = timestamp(timing["finished_at"]) if timing.get("finished_at") is not None else None
    require(finish is None or finish >= start, "negative elapsed time")
    require(bool(evidence(timing.get("evidence"))), "timing evidence required")
    phases = timing.get("phases")
    require(isinstance(phases, dict) and set(PHASES) == set(phases), "all six phase metrics required")
    for key in PHASES:
        validate_metric(phases[key], "seconds")
    capture = record.get("capture")
    require(isinstance(capture, dict) and type(capture.get("complete")) is bool
            and all(string(capture.get(k)) for k in ("method", "scope")), "capture contract required")
    require(bool(evidence(capture.get("evidence"))), "capture evidence required")
    require(capture["complete"] or string(capture.get("reason")), "incomplete capture needs reason")
    usage = record.get("usage")
    require(isinstance(usage, dict) and string(usage.get("definitions")), "usage definitions required")
    for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
        item = validate_metric(usage.get(key), "tokens")
        require(item["kind"] in ("measured", "unavailable"), "tokens require actual supplier usage; otherwise unavailable")
    costs = record.get("costs")
    require(isinstance(costs, dict) and string(costs.get("currency")), "cost currency required")
    for key in COSTS:
        item = validate_metric(costs.get(key), costs["currency"])
        if item["kind"] == "derived" and item["value"] is not None:
            require(item.get("billing_basis") == "invoice_aggregation", "derived actual cost needs invoice basis")
    jev = record.get("jev")
    require(isinstance(jev, dict) and string(jev.get("ranker_revision")), "Jev revision required")
    require("version" in jev and (jev["version"] is None or string(jev["version"])), "Jev version required (nullable)")
    require(jev.get("status") in ("not_used", "ranked", "partial", "fallback", "unavailable"), "invalid Jev status")
    for key, unit in JEV_METRICS.items():
        validate_metric(jev.get(key), unit)
    bound = jev["http_attempts_upper_bound"]["value"]
    submitted = jev["submitted_candidates"]["value"]
    invocations = jev["cli_invocations"]["value"]
    require(bound is None or bound == 0 or jev["version"] == "0.3.2", "unknown CLI cannot claim positive HTTP bound")
    require(bound is None or submitted is None or bound <= 2 * submitted, "invalid HTTP bound")
    if bound is not None and invocations is not None:
        require((bound == 0) == (invocations == 0), "HTTP bound must reflect scoring startup")
        if invocations > 0 and submitted is not None:
            require(bound == 2 * submitted, "invalid HTTP bound for started scoring")
    require(isinstance(jev.get("failure_types"), list) and all(string(x) for x in jev["failure_types"]), "failure types required")
    if record["arm"] == "A":
        require(jev["status"] == "not_used" and all(jev[k]["value"] in (0, None) for k in JEV_METRICS), "A cannot use Jev")
    require(isinstance(record.get("protocol_deviations"), list)
            and all(string(x) for x in record["protocol_deviations"]), "protocol deviations required")
    limits = {"cli_invocations": 1, "preflight_invocations": 1,
              "submitted_candidates": 12, "payload_bytes": 24576, "http_attempts_upper_bound": 24}
    require(record["protocol_deviations"] or all(jev[key]["value"] is None or jev[key]["value"] <= limit
            for key, limit in limits.items()), "budget deviation must be declared and retained")
    evidence(record.get("evidence"))
    outcome = record.get("outcome")
    if outcome is not None:
        require(isinstance(outcome, dict) and outcome.get("status") in
                ("success", "failure", "timeout", "environment_failure", "unavailable"), "invalid outcome")
        checks = outcome.get("validations")
        require(isinstance(checks, list), "validation records required")
        roles = defaultdict(list)
        for check in checks:
            require(isinstance(check, dict) and check.get("role") in
                    ("reproduction_before", "reproduction_after", "independent", "regression")
                    and string(check.get("command")) and type(check.get("exit_code")) is int
                    and bool(evidence(check.get("evidence"))), "invalid validation evidence")
            roles[check["role"]].append(check["exit_code"])
        if outcome["status"] == "success":
            require(outcome.get("tests_unmodified") is True
                    and any(code != 0 for code in roles["reproduction_before"])
                    and all(roles[k] and all(code == 0 for code in roles[k]) for k in
                            ("reproduction_after", "independent", "regression")), "success evidence incomplete or invalid")
    return start, finish


def reading_metrics(record, events):
    reads = [event for event in events if event["type"] == "source_read"]
    groups = defaultdict(list)
    for event in reads:
        groups[(event["path"], event["file_version"])].append((event["start_line"], event["end_line"]))
    intervals = []
    for (path, version), ranges in sorted(groups.items()):
        merged = []
        for start, end in sorted(ranges):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        intervals.append({"path": path, "file_version": version, "ranges": merged})
    capture = record["capture"]
    completeness = "complete" if capture["complete"] else "lower_bound"
    paths = capture["evidence"] + [p for event in reads for p in event["evidence"]]
    return {"cumulative_bytes": derived(sum(event["bytes"] for event in reads), "bytes",
                                        "normalized source_read events", paths, completeness, capture.get("reason")),
            "unique_lines": derived(sum(end - start + 1 for item in intervals for start, end in item["ranges"]),
                                    "lines", "union by path and file_version", paths, completeness, capture.get("reason")),
            "intervals": intervals, "capture": capture}


def cost_total(record, actual):
    items = [record["costs"][k] for k in COSTS]
    acceptable = ("measured", "derived") if actual else ("measured", "derived", "estimated")
    if any(item["value"] is None or item["kind"] not in acceptable
           or item["completeness"] != "complete" for item in items):
        return unavailable(record["costs"]["currency"], "some components unavailable, incomplete or estimated")
    total = derived(sum(item["value"] for item in items), record["costs"]["currency"],
                    "sum main_model + jev + attributable review", [p for item in items for p in item["evidence"]])
    if not actual and any(item["kind"] == "estimated" for item in items):
        total["kind"] = "estimated"
        total["price_bases"] = [item["price"] for item in items if item["kind"] == "estimated"]
    return total


def normalize_run(record, events, start, finish):
    roots = [event for event in events if event["type"] == "root_cause" and event["verified"]]
    root = min(roots, key=lambda e: timestamp(e["timestamp"])) if roots else None
    return {**record, "outcome": record.get("outcome") or {"status": "unavailable", "reason": "no result record"},
            "elapsed_seconds": derived((finish - start).total_seconds(), "seconds", "task receipt to final verification",
                                       record["timing"]["evidence"]) if finish else unavailable("seconds", "final timing boundary missing"),
            "first_correct_root_seconds": derived((timestamp(root["timestamp"]) - start).total_seconds(), "seconds",
                                                  "evaluator-verified root_cause event", root["evidence"])
            if root else unavailable("seconds", "no evaluator-verified root event"),
            "reading": reading_metrics(record, events), "total_real_cost": cost_total(record, True),
            "total_estimated_cost": cost_total(record, False)}


def validate_events(events, records, times):
    grouped, seen = defaultdict(list), set()
    for event in events:
        require(isinstance(event, dict) and type(event.get("schema_version")) is int
                and event["schema_version"] == 1, "invalid event schema")
        identifier, run_id = event.get("event_id"), event.get("run_id")
        require(string(identifier) and string(run_id) and run_id in records, "event has unknown run or ID")
        require((run_id, identifier) not in seen, "duplicate event identity")
        seen.add((run_id, identifier))
        moment = timestamp(event.get("timestamp"))
        start, finish = times[run_id]
        require(moment >= start and (finish is None or moment <= finish), "event outside timing boundaries")
        require(string(event.get("source")) and bool(evidence(event.get("evidence"))), "event needs source and evidence")
        if event.get("type") == "source_read":
            require(string(event.get("path")) and string(event.get("file_version")), "source event needs path/version")
            first, last, size = event.get("start_line"), event.get("end_line"), event.get("bytes")
            require(type(first) is int and type(last) is int and 1 <= first <= last
                    and type(size) is int and 0 <= size <= 1e18, "invalid source range/bytes")
        elif event.get("type") == "root_cause":
            require(type(event.get("verified")) is bool and string(event.get("claim")), "root event needs evaluator verdict")
        else:
            raise ValueError("unsupported normalized event type")
        grouped[run_id].append(event)
    return grouped


def pair_runs(pair_id, group):
    arm = {record["arm"]: record for record in group}
    result = {"pair_id": pair_id, "case_id": group[0]["case_id"], "run_ids": {key: r["run_id"] for key, r in arm.items()},
              "both_successful": False, "elapsed_seconds_delta": None, "reading_bytes_delta": None,
              "reading_reduction": None, "real_cost_delta": None, "excluded_reasons": []}
    if set(arm) != {"A", "C"}:
        result["excluded_reasons"].append("unpaired run")
        return result
    a, c = arm["A"], arm["C"]
    require(all(a[k] == c[k] for k in ("case_id", "repository", "config", "development_fixture"))
            and a["costs"]["currency"] == c["costs"]["currency"], "pair mismatch: case/repository/configuration/currency")
    result["both_successful"] = all(r["outcome"]["status"] == "success" for r in (a, c))
    if not result["both_successful"]:
        result["excluded_reasons"].append("not both successful; retained in arm totals")
        return result
    if a["protocol_deviations"] or c["protocol_deviations"]:
        result["excluded_reasons"].append("protocol deviation")
        return result
    for metric, target in (("elapsed_seconds", "elapsed_seconds_delta"), ("total_real_cost", "real_cost_delta")):
        av, cv = a[metric]["value"], c[metric]["value"]
        if av is not None and cv is not None:
            result[target] = cv - av
        else:
            result["excluded_reasons"].append("missing " + metric)
    ac, cc = a["capture"], c["capture"]
    if ac["complete"] and cc["complete"] and all(ac[k] == cc[k] for k in ("method", "scope")):
        av, cv = a["reading"]["cumulative_bytes"]["value"], c["reading"]["cumulative_bytes"]["value"]
        result["reading_bytes_delta"] = cv - av
        result["reading_reduction"] = (av - cv) / av if av else None
        if not av:
            result["excluded_reasons"].append("zero A exposure; percentage undefined")
    else:
        result["excluded_reasons"].append("incomplete or incomparable source capture")
    return result


def descriptive(values):
    return {"n_tasks": len(values), "median": statistics.median(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None,
            "wins": sum(value < 0 for value in values), "ties": sum(value == 0 for value in values),
            "losses": sum(value > 0 for value in values)}


def arm_totals(normalized):
    arms = {}
    for arm in ("A", "C"):
        selected = [r for r in normalized if r["arm"] == arm]
        successes = sum(r["outcome"]["status"] == "success" for r in selected)
        totals = [r["total_real_cost"]["value"] for r in selected]
        known = sum(item["value"] for r in selected for key in COSTS
                    for item in [r["costs"][key]] if item["value"] is not None
                    and item["kind"] in ("measured", "derived") and item["completeness"] == "complete")
        total = sum(totals) if selected and all(v is not None for v in totals) else None
        arms[arm] = {"total_runs": len(selected), "successes": successes,
                     "success_rate": successes / len(selected) if selected else None,
                     "total_real_cost": total, "known_cost_subtotal": known,
                     "cost_per_success": total / successes if total is not None and successes else None,
                     "cost_per_success_reason": None if total is not None and successes else
                     "zero successes or incomplete actual cost",
                     "jev_status_counts": {status: sum(r["jev"]["status"] == status for r in selected)
                                           for status in ("not_used", "ranked", "partial", "fallback", "unavailable")}}
    return arms


def summarize(runs, events):
    require(isinstance(runs, list) and isinstance(events, list), "runs/events must be lists")
    records, times, pairs = {}, {}, defaultdict(list)
    for record in runs:
        boundary = validate_run(record)
        identifier = record["run_id"]
        require(identifier not in records, "duplicate run ID")
        require(not any(r["arm"] == record["arm"] for r in pairs[record["pair_id"]]), "duplicate arm in pair")
        records[identifier], times[identifier] = record, boundary
        pairs[record["pair_id"]].append(record)
    require(len({r["costs"]["currency"] for r in runs}) <= 1, "cannot aggregate different currencies")
    # A task ID always identifies one frozen starting repository across repetitions.
    cases = {}
    for record in runs:
        identity = (record["repository"], record["development_fixture"])
        require(record["case_id"] not in cases or cases[record["case_id"]] == identity, "pair mismatch: case ID reused across versions")
        cases[record["case_id"]] = identity
    by_run = validate_events(events, records, times)
    normalized = [normalize_run(record, by_run[record["run_id"]], *times[record["run_id"]]) for record in runs]
    normalized_pairs = defaultdict(list)
    for record in normalized:
        normalized_pairs[record["pair_id"]].append(record)
    comparisons = [pair_runs(key, group) for key, group in normalized_pairs.items()]
    tasks = []
    for case_id in sorted(cases):
        selected = [p for p in comparisons if p["case_id"] == case_id]
        task = {"case_id": case_id, "pairs": len(selected), "development_fixture": cases[case_id][1]}
        for key in ("elapsed_seconds_delta", "reading_bytes_delta", "reading_reduction", "real_cost_delta"):
            values = [p[key] for p in selected if p[key] is not None]
            task[key] = statistics.median(values) if values else None
        joint = [p for p in selected if p["reading_reduction"] is not None and p["elapsed_seconds_delta"] is not None]
        task["joint_comparable_pairs"] = [p["pair_id"] for p in joint]
        task["joint_reading_reduction"] = statistics.median(p["reading_reduction"] for p in joint) if joint else None
        task["joint_elapsed_seconds_delta"] = statistics.median(p["elapsed_seconds_delta"] for p in joint) if joint else None
        tasks.append(task)
    arms = arm_totals(normalized)
    actual = [r for r in normalized if not r["development_fixture"]]
    real_arms = arm_totals(actual)
    eligible_tasks = [t for t in tasks if not t["development_fixture"] and t["joint_comparable_pairs"]]
    real_pairs = [p for p in comparisons if not cases[p["case_id"]][1]]
    balanced = all(set(p["run_ids"]) == {"A", "C"} for p in real_pairs)
    completed = all(r["outcome"]["status"] != "unavailable"
                    and r["elapsed_seconds"]["value"] is not None for r in actual)
    benefit = {"status": "insufficient_evidence", "reason": "fewer than six complete comparable real tasks",
               "eligible_real_tasks": len(eligible_tasks), "cost_target": "unavailable",
               "scope": "exploratory targets only; no general savings or quality noninferiority claim"}
    new_failures = [p["case_id"] for p in comparisons if set(p["run_ids"]) == {"A", "C"}
                    and records[p["run_ids"]["A"]].get("outcome")
                    and records[p["run_ids"]["A"]]["outcome"]["status"] == "success"
                    and records[p["run_ids"]["C"]].get("outcome")
                    and records[p["run_ids"]["C"]]["outcome"]["status"] in ("failure", "timeout", "environment_failure")
                    and not records[p["run_ids"]["A"]]["development_fixture"]]
    if new_failures:
        benefit.update(status="pause_for_quality_failure", reason="C failed where A succeeded", cases=new_failures)
    elif not completed:
        benefit["reason"] = "unfinished real runs; complete the frozen run set before evaluating targets"
    elif len(eligible_tasks) >= 6 and balanced and len(eligible_tasks) == len([t for t in tasks if not t["development_fixture"]]):
        reading = statistics.median(t["joint_reading_reduction"] for t in eligible_tasks)
        elapsed = statistics.median(t["joint_elapsed_seconds_delta"] for t in eligible_tasks)
        cost_known = all(r["total_real_cost"]["value"] is not None for r in actual)
        cost_deltas = []
        if cost_known:
            for task in eligible_tasks:
                totals = {arm: sum(r["total_real_cost"]["value"] for r in actual
                                   if r["case_id"] == task["case_id"] and r["arm"] == arm) for arm in ("A", "C")}
                cost_deltas.append(totals["C"] - totals["A"])
        cost_pass = cost_known and statistics.median(cost_deltas) <= 0 and (
            real_arms["C"]["total_real_cost"] <= real_arms["A"]["total_real_cost"]
            and real_arms["C"]["cost_per_success"] <= real_arms["A"]["cost_per_success"])
        benefit.update(status="exploratory_target_met" if reading >= 0.2 and elapsed <= 0
                       and (not cost_known or cost_pass) else "exploratory_target_not_met",
                       reason="predeclared median reading/time targets; actual cost evaluated only if complete",
                       median_reading_reduction=reading, median_elapsed_delta=elapsed,
                       cost_target=("met" if cost_pass else "not_met") if cost_known else "unavailable",
                       cost_basis="all real runs, including failed repeats; task medians and aggregate cost/success")
    elif not balanced:
        benefit["reason"] = "unpaired real runs; complete the frozen A/C run set before evaluating targets"
    return {"schema_version": 1, "development_fixture": any(r["development_fixture"] for r in runs),
            "real_experiment_runs": len(actual), "independent_tasks": len(cases),
            "runs": normalized, "pairs": comparisons, "tasks": tasks, "arms": arms, "real_arms": real_arms,
            "task_statistics": {key: descriptive([t[key] for t in tasks if t[key] is not None])
                                for key in ("elapsed_seconds_delta", "reading_bytes_delta", "real_cost_delta")},
            "benefit_evidence": benefit,
            "limitations": ["Normalized sources/completeness are declarations requiring evaluator audit.",
                            "Development fixtures do not establish real benefits.",
                            "Reading bytes include repeated exposure; unique lines use path plus file version.",
                            "Estimated prices, HTTP upper bounds and subscription percentages are not bills."]}


def markdown(report):
    def cell(value):
        if value is None:
            return "unavailable"
        return str(round(value, 6)) if isinstance(value, float) else str(value).replace("|", "\\|").replace("\n", " ")
    lines = ["# Run evidence summary", "", "DEVELOPMENT FIXTURE — synthetic data; no real benefit evidence."
             if report["development_fixture"] else "Real run records; evidence declarations require audit.", "",
             "Real runs: %s. Independent tasks: %s." % (report["real_experiment_runs"], report["independent_tasks"]), "",
             "Benefit status: `%s`. %s" % (report["benefit_evidence"]["status"], report["benefit_evidence"]["reason"]), "",
             "| Arm | Runs | Successes | Success rate | Actual total cost | Known subtotal | Cost/success |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for arm, data in report["arms"].items():
        lines.append("| " + " | ".join(cell(v) for v in [arm, data["total_runs"], data["successes"], data["success_rate"],
                                                      data["total_real_cost"], data["known_cost_subtotal"], data["cost_per_success"]]) + " |")
    lines += ["", "Deltas are C minus A; comparisons use both successful, protocol-compliant runs.", "",
              "| Pair | Task | Seconds delta | Observed bytes delta | Reading reduction | Actual cost delta | Exclusions |",
              "| --- | --- | ---: | ---: | ---: | ---: | --- |"]
    for pair in report["pairs"]:
        lines.append("| " + " | ".join(cell(v) for v in [pair["pair_id"], pair["case_id"], pair["elapsed_seconds_delta"],
                     pair["reading_bytes_delta"], pair["reading_reduction"], pair["real_cost_delta"],
                     "; ".join(pair["excluded_reasons"])]) + " |")
    lines += ["", "Evidence index (all runs, including failures):", ""]
    for record in report["runs"]:
        lines.append("- `%s`: %s; elapsed %s s; exposed %s bytes (%s); evidence %s" %
                     (cell(record["run_id"]), record["outcome"]["status"], cell(record["elapsed_seconds"]["value"]),
                      cell(record["reading"]["cumulative_bytes"]["value"]), record["reading"]["cumulative_bytes"]["completeness"],
                      ", ".join(cell(p) for p in record["evidence"])))
    lines += ["", "Limitations:", ""] + ["- " + value for value in report["limitations"]]
    return "\n".join(lines) + "\n"


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    def constant(value):
        raise ValueError("nonfinite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def load_records(path):
    raw = Path(path).read_text(encoding="utf-8")
    if not raw.strip():
        return []
    if Path(path).suffix == ".jsonl":
        return [strict_json(line) for line in raw.splitlines() if line.strip()]
    value = strict_json(raw)
    require(isinstance(value, list), "JSON input must be a record array; use .jsonl for individual records")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--events", help="Normalized source_read/root_cause JSON or JSONL")
    parser.add_argument("--json", help="Output JSON path; stdout by default")
    parser.add_argument("--markdown", help="Output Markdown path")
    args = parser.parse_args(argv)
    try:
        report = summarize(load_records(args.runs), load_records(args.events) if args.events else [])
        encoded = json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
        if args.json:
            Path(args.json).write_text(encoded, encoding="utf-8")
        else:
            sys.stdout.write(encoded)
        if args.markdown:
            Path(args.markdown).write_text(markdown(report), encoding="utf-8")
    except (ValueError, TypeError, OSError, UnicodeError, RecursionError) as error:
        # Input snippets, raw tool output and filesystem exceptions are never printed.
        message = str(error) if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError) else "invalid or unreadable local evidence"
        sys.stderr.write("benchmark input rejected: " + message + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
