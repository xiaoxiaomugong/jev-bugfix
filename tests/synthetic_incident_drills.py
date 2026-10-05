#!/usr/bin/env python3
"""Rebuild two explicitly synthetic incident drills with temporary Git repos.

Only Python's standard library and local Git are used. The synthetic program
receives all differing runtime inputs as arguments. No live scorer is started.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "skills" / "jev-bugfix" / "scripts" / "prepare_incident.py"
RANKER = ROOT / "skills" / "jev-bugfix" / "scripts" / "rank_candidates.py"
GIT = shutil.which("git")
COMMIT_DATE = "2026-10-05T00:00:00+00:00"

RUNTIME_SOURCE = '''import argparse

def total(raw, runtime_version):
    if runtime_version == "compat-v1":
        raw = raw.replace(",", ".")
    return float(raw)

parser = argparse.ArgumentParser()
parser.add_argument("--amount", required=True)
parser.add_argument("--runtime-version", required=True)
args = parser.parse_args()
print(total(args.amount, args.runtime_version))
'''

BASELINE_SOURCE = '''import argparse

def total(raw, runtime_version):
    return float(raw.replace(",", "."))

parser = argparse.ArgumentParser()
parser.add_argument("--amount", required=True)
parser.add_argument("--runtime-version", required=True)
args = parser.parse_args()
print(total(args.amount, args.runtime_version))
'''

EVENT_SOURCE = '''import argparse

# synthetic-event-version
# The upgraded parser no longer accepts a decimal comma.
def total(raw, runtime_version):
    return float(raw)

parser = argparse.ArgumentParser()
parser.add_argument("--amount", required=True)
parser.add_argument("--runtime-version", required=True)
args = parser.parse_args()
print(total(args.amount, args.runtime_version))
'''


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def offline_environment():
    # Construct a small environment; do not read or record credentials/config.
    return {
        "PATH": os.defpath,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_DATE": COMMIT_DATE,
        "GIT_COMMITTER_DATE": COMMIT_DATE,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def git(repo, *arguments):
    if GIT is None:
        raise RuntimeError("local_git_required")
    result = subprocess.run(
        [GIT, "--no-pager", "-c", "core.fsmonitor=false", "-c",
         "user.name=Synthetic Drill", "-c", "user.email=synthetic@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
         "-C", str(repo)] + list(arguments),
        capture_output=True, text=True, encoding="utf-8", check=False,
        timeout=5, env=offline_environment(),
    )
    if result.returncode:
        raise RuntimeError("synthetic_git_command_failed")
    return result.stdout if arguments[0] == "show" else result.stdout.rstrip("\n")


def commit_source(repo, source, message):
    (repo / "app.py").write_text(source, encoding="utf-8")
    git(repo, "add", "--", "app.py")
    git(repo, "commit", "--quiet", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def run_program(repo, runtime_version):
    command = [sys.executable, "-B", str(repo / "app.py"), "--amount", "1,25",
               "--runtime-version", runtime_version]
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", check=False, timeout=5,
                            env=offline_environment())
    return {
        "command": [sys.executable, "-B", "<temporary-repository>/app.py",
                    "--amount", "1,25", "--runtime-version", runtime_version],
        "explicit_inputs": {"amount": "1,25", "runtime_version": runtime_version},
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def failing_line(observation):
    # Read the actual traceback of our fixed synthetic fixture, not a guessed line.
    matches = re.findall(r'File "[^"\n]*/app\.py", line (\d+), in total',
                         observation["stderr"])
    if len(matches) != 1:
        raise RuntimeError("synthetic_traceback_frame_missing")
    return int(matches[0])


def record_subprocess(command, stdout_path, stderr_path):
    result = subprocess.run(command, capture_output=True, text=True,
                            encoding="utf-8", check=False, timeout=25,
                            env=offline_environment())
    stdout_path.write_text(result.stdout, encoding="utf-8")
    stderr_path.write_text(result.stderr, encoding="utf-8")
    return result


def verify_handoff(repo, evidence, case, event_commit):
    provenance = {item["id"]: item for item in evidence["candidate_provenance"]}
    checks = []
    for candidate in case["candidates"]:
        source = git(repo, "show", event_commit + ":" + candidate["path"])
        event_lines = source.splitlines(keepends=True)
        start, end = candidate["start_line"], candidate["end_line"]
        snippet = "".join(event_lines[start - 1:end])
        origin = provenance[candidate["id"]]
        blob_oid = git(repo, "rev-parse", event_commit + ":" + candidate["path"])
        if (candidate["snippet"] != snippet or origin["event_commit"] != event_commit
                or origin["blob_oid"] != blob_oid
                or origin["snippet_sha256"] != hashlib.sha256(
                    snippet.encode("utf-8")).hexdigest()
                or any(origin[key] != candidate[key]
                       for key in ("path", "start_line", "end_line"))
                or origin["evidence_refs"] != [{"event_id": evidence["selected_event"],
                                               "event_ref": evidence["selected_event_ref"],
                                               "event_indices": [0], "frame_index": 0}]):
            raise RuntimeError("synthetic_candidate_provenance_mismatch")
        checkout_source = git(repo, "show", "HEAD:" + candidate["path"])
        checkout_snippet = "".join(checkout_source.splitlines(keepends=True)[start - 1:end])
        checks.append({"id": candidate["id"], "event_commit": event_commit,
                       "blob_oid": blob_oid, "start_line": start, "end_line": end,
                       "snippet_sha256": origin["snippet_sha256"],
                       "checkout_interval_matches": checkout_snippet == snippet})
    # Continue beyond ranking using the bound commit: inspect full context,
    # search the symbol and identify the fixed fixture's direct caller there.
    event_context = git(repo, "show", event_commit + ":app.py").splitlines()
    checkout_context = git(repo, "show", "HEAD:app.py").splitlines()
    def symbol_line(lines, prefix):
        return next(index + 1 for index, line in enumerate(lines) if line.startswith(prefix))
    followup = {
        "event_commit": event_commit,
        "blob_oid": git(repo, "rev-parse", event_commit + ":app.py"),
        "path": "app.py", "symbol": "total",
        "definition_line": symbol_line(event_context, "def total("),
        "direct_call_line": symbol_line(event_context, "print(total("),
        "checkout_definition_line": symbol_line(checkout_context, "def total("),
        "checkout_direct_call_line": symbol_line(checkout_context, "print(total("),
        "interpretation": "Observed fixture call in event blob; no general call graph inferred.",
    }
    return {
        "verified_against_event_blob": bool(checks),
        "checkout_would_be_wrong": any(not item["checkout_interval_matches"]
                                        for item in checks),
        "checks": checks,
        "followup_context": followup,
        "followup_rule": "Read context, symbols and calls at event_commit; choose the "
                         "modification version explicitly before mapping current source.",
    }


def run_drill(output, scenario):
    directory = output / scenario
    directory.mkdir()
    source_dir = directory / "source"
    source_dir.mkdir()
    with tempfile.TemporaryDirectory(prefix="synthetic-incident-repo-") as temporary:
        repo = Path(temporary)
        git(repo, "init", "--quiet")
        git(repo, "symbolic-ref", "HEAD", "refs/heads/synthetic")
        if scenario == "runtime_difference":
            event_commit = commit_source(repo, RUNTIME_SOURCE, "synthetic runtime fixture")
            baseline_commit = None
            local = run_program(repo, "compat-v1")
            event = run_program(repo, "compat-v2")
            observations = {"synthetic": True, "local": local, "event": event,
                            "interpretation": "One host Python interpreter simulates "
                                              "two explicit compatibility runtime inputs."}
            event_runtime = "compat-v2"
            event_source = RUNTIME_SOURCE
            runtime_difference = {"field": "runtime.version", "local": "compat-v1",
                                  "event": "compat-v2"}
            if local["exit_code"] != 0 or event["exit_code"] != 1:
                raise RuntimeError("synthetic_runtime_observation_mismatch")
        else:
            baseline_commit = commit_source(repo, BASELINE_SOURCE, "synthetic baseline parser")
            baseline = run_program(repo, "compat-v1")
            event_commit = commit_source(repo, EVENT_SOURCE, "synthetic upgraded parser")
            event = run_program(repo, "compat-v1")
            git(repo, "checkout", "--quiet", "--detach", baseline_commit)
            observations = {"synthetic": True, "baseline": baseline, "event": event,
                            "interpretation": "Same explicit inputs, baseline accepts "
                                              "decimal comma and event parser raises."}
            event_runtime = "compat-v1"
            event_source = EVENT_SOURCE
            runtime_difference = None
            (source_dir / "baseline-app.py").write_text(BASELINE_SOURCE, encoding="utf-8")
            if baseline["exit_code"] != 0 or event["exit_code"] != 1:
                raise RuntimeError("synthetic_regression_observation_mismatch")
        git(repo, "tag", "synthetic-event", event_commit)
        checkout_commit = git(repo, "rev-parse", "HEAD")
        checkout_before = git(repo, "status", "--porcelain")
        (source_dir / "event-app.py").write_text(event_source, encoding="utf-8")
        write_json(directory / "observations.json", observations)
        selected_id = "synthetic-" + scenario
        input_events = {
            "schema_version": 1,
            "events": [{
                "event_id": selected_id, "timestamp": "2026-10-05T00:00:00Z",
                "service": "synthetic-decimal-service", "environment": "synthetic-production",
                "release": "synthetic-event-release", "commit": event_commit,
                "exception": {"type": "ValueError",
                              "message": "could not convert string to float: '1,25'",
                              "frames": [{"path": "/srv/synthetic/app.py",
                                          "line": failing_line(event), "function": "total",
                                          "in_app": True}]},
                "breadcrumbs": [{"timestamp": "2026-10-05T00:00:00Z",
                                 "category": "synthetic.fixture", "level": "info",
                                 "message": "Explicit fixture input: amount=1,25"}],
                "trace": {"trace_id": "synthetic-trace-" + scenario,
                          "span_id": "synthetic-span", "parent_span_id": None,
                          "spans": []},
                "runtime": {"language": "synthetic-decimal-runtime", "version": event_runtime,
                            "os": "synthetic-os", "arch": "synthetic-arch"},
            }],
        }
        release_map = {"schema_version": 1, "entries": [{
            "service": "synthetic-decimal-service", "environment": "synthetic-production",
            "release": "synthetic-event-release", "revision": "refs/tags/synthetic-event"}]}
        write_json(directory / "input-events.json", input_events)
        write_json(directory / "release-map.json", release_map)
        incident = directory / "incident"
        command = [sys.executable, "-B", str(PREPARE), "--input",
                   str(directory / "input-events.json"), "--repo", str(repo),
                   "--release-map", str(directory / "release-map.json"),
                   "--event-id", selected_id, "--source-root", "/srv/synthetic",
                   "--output-dir", str(incident)]
        if baseline_commit is not None:
            command += ["--baseline-revision", baseline_commit]
        write_json(directory / "prepare-command.json",
                   ["<temporary-repository>" if value == str(repo) else value for value in command])
        prepare = record_subprocess(command, directory / "prepare.stdout.json",
                                    directory / "prepare.stderr.txt")
        if prepare.returncode:
            raise RuntimeError("synthetic_preparation_failed_see_saved_output")
        evidence = json.loads((incident / "evidence.json").read_text(encoding="utf-8"))
        case = json.loads((incident / "case.json").read_text(encoding="utf-8"))
        if evidence["status"] != "ready" or case["reviewed_for_secrets"] is not False:
            raise RuntimeError("synthetic_preparation_contract_mismatch")
        if evidence["version"]["resolution"]["event_commit"] != event_commit:
            raise RuntimeError("synthetic_event_commit_mismatch")
        if baseline_commit is not None and evidence["version"]["baseline"]["commit"] != baseline_commit:
            raise RuntimeError("synthetic_baseline_commit_mismatch")
        sidecar_runtime = next(item["runtime"] for item in evidence["events"]
                               if item["event_id"] == selected_id)
        if sidecar_runtime["version"] != event_runtime:
            raise RuntimeError("synthetic_runtime_evidence_missing")
        handoff = verify_handoff(repo, evidence, case, event_commit)
        ranking = record_subprocess(
            [sys.executable, "-B", str(RANKER), "--input", str(incident / "case.json")],
            directory / "ranker-dry-run.json", directory / "ranker.stderr.txt")
        if ranking.returncode:
            raise RuntimeError("synthetic_ranker_dry_run_failed")
        rank_report = json.loads(ranking.stdout)
        if (rank_report["status"] != "dry_run"
                or rank_report["usage"]["cli_invocations"] != 0
                or rank_report["cli_preflight"]["invocations"] != 0):
            raise RuntimeError("synthetic_ranker_launched_scorer")
        if git(repo, "rev-parse", "HEAD") != checkout_commit or git(
                repo, "status", "--porcelain") != checkout_before:
            raise RuntimeError("synthetic_checkout_modified")
        relative = lambda path: str(path.relative_to(output))
        artifacts = {
            "input_events": relative(directory / "input-events.json"),
            "release_map": relative(directory / "release-map.json"),
            "observations": relative(directory / "observations.json"),
            "event_source": relative(source_dir / "event-app.py"),
            "prepare_command": relative(directory / "prepare-command.json"),
            "prepare_stdout": relative(directory / "prepare.stdout.json"),
            "prepare_stderr": relative(directory / "prepare.stderr.txt"),
            "evidence": relative(incident / "evidence.json"),
            "report": relative(incident / "report.md"),
            "case": relative(incident / "case.json"),
            "ranker_dry_run": relative(directory / "ranker-dry-run.json"),
            "ranker_stderr": relative(directory / "ranker.stderr.txt"),
        }
        if baseline_commit is not None:
            artifacts["baseline_source"] = relative(source_dir / "baseline-app.py")
        return {
            "scenario": scenario, "synthetic": True,
            "commits": {"baseline": baseline_commit, "event": event_commit,
                        "checkout": checkout_commit},
            "baseline_explicitly_supplied": baseline_commit is not None,
            "runtime_difference": runtime_difference, "sidecar_runtime": sidecar_runtime,
            "candidate_handoff": handoff, "artifacts": artifacts,
            "source_repository": {
                "lifecycle": "temporary_cleaned_after_run",
                "branch": "synthetic", "author": "Synthetic Drill <synthetic@example.invalid>",
                "author_and_committer_date": COMMIT_DATE,
                "rebuild": "Run tests/synthetic_incident_drills.py in a new output directory; "
                           "fixed source, author, dates and messages reconstruct these commits.",
                "rebuild_command": ["python3", "-B", "tests/synthetic_incident_drills.py",
                                    "--scenario", scenario, "--output-dir", "<new-empty-directory>"],
            },
            "conclusions": {
                "local_reproduction": "reproduced", "root_cause": "confirmed",
                "fix_verification": "unverified",
                "regression_attribution": "confirmed" if baseline_commit else "unknown",
                "scope": "Known synthetic fixture only; adapter conclusions remain evidence "
                         "preparation states and do not prove a real production root cause.",
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--scenario", choices=("all", "runtime_difference", "regression"),
                        default="all")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        parser.error("output directory must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    scenarios = ("runtime_difference", "regression") if args.scenario == "all" else (args.scenario,)
    manifest = {
        "schema_version": 1, "synthetic": True, "status": "incomplete", "drills": [],
        "environment": {"python": platform.python_version(), "os": platform.system(),
                        "architecture": platform.machine()},
        "limits": "No credentials, environment dump, live Jev/API or true production incident; "
                  "fixtures verify the offline preparation and source handoff only.",
    }
    try:
        for scenario in scenarios:
            manifest["drills"].append(run_drill(output, scenario))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        manifest["error_type"] = type(error).__name__
        write_json(output / "manifest.json", manifest)
        print("synthetic_drill_failed_see_saved_artifacts", file=sys.stderr)
        return 2
    manifest["status"] = "complete"
    write_json(output / "manifest.json", manifest)
    print(json.dumps({"status": "complete", "synthetic": True,
                      "drills": len(manifest["drills"]), "manifest": str(output / "manifest.json")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
