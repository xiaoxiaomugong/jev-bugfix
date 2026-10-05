#!/usr/bin/env python3
"""Narrow, offline known-answer replay of boltons PR #428 / issue #452.

Only two reviewed, pinned upstream source hashes may be executed. This is a
retrospective test driver, not an event collector or a generic plugin loader.
"""

import argparse
import hashlib
from itertools import islice
import json
from pathlib import Path
import platform
import sys
import traceback
import types
from datetime import datetime, timezone


BROKEN_COMMIT = "57cb026b7f47cd2765a0d5acdc83849ed5f1f6a3"
FIXED_COMMIT = "ead236e278ca0466bf468de746b5960fb12d7e5b"
RELEASE = "oss-replay/boltons/57cb026"
SOURCE_HASHES = {
    "c5cf8da7231fcb72f14f7f5507ff7c0b8778e5c495d69c596c4eb2ef48e8cfda": BROKEN_COMMIT,
    "77ec0e8d53b060f236d06f7c3c8ed107d34818dd8cf1b2a6b986bdea1167effe": FIXED_COMMIT,
}
CASES = (
    ("constant_equal_bounds", {"start": 5, "stop": 5, "factor": 1.0}),
    ("constant_different_bounds", {"start": 1, "stop": 10, "factor": 1.0}),
    ("constant_zero_start", {"start": 0, "stop": 10, "factor": 1.0}),
    ("constant_explicit_count", {"start": 1, "stop": 10, "count": 3, "factor": 1.0}),
    ("growing_factor", {"start": 1, "stop": 8, "factor": 2.0}),
    ("constant_repeat_prefix", {"start": 1, "stop": 10, "count": "repeat", "factor": 1.0}),
)


def observe_source(source_path):
    """Execute pinned bytes and retain real result/exception/library frames."""
    path = Path(source_path).resolve()
    with path.open("rb") as handle:
        source = handle.read(2 * 1024 * 1024 + 1)
    digest = hashlib.sha256(source).hexdigest()
    if digest not in SOURCE_HASHES:
        raise ValueError("source_hash_mismatch")
    module = types.ModuleType("oss_replay_iterutils")
    module.__file__ = str(path)
    module.__package__ = ""
    # Relative typeutils import falls back to local sentinel objects; this does
    # not participate in backoff_iter. No package installation is necessary.
    exec(compile(source, str(path), "exec"), module.__dict__)
    observations = []
    for name, parameters in CASES:
        entry = {"case": name, "input": dict(parameters), "output": None, "exception": None}
        try:
            values = module.backoff_iter(**parameters)
            entry["output"] = list(islice(values, 3)) if name == "constant_repeat_prefix" else list(values)
        except Exception as error:
            frames = traceback.extract_tb(error.__traceback__)
            library_frames = [frame for frame in frames if frame.filename == str(path)]
            entry["exception"] = {
                "type": type(error).__name__, "message": str(error),
                "frames": [{"filename": "boltons/iterutils.py", "lineNo": frame.lineno,
                            "function": frame.name, "inApp": True} for frame in library_frames],
                "omitted_harness_frames": len(frames) - len(library_frames),
            }
        observations.append(entry)
    return {
        "classification": "oss_replay", "known_answer": True, "nonblind": True,
        "upstream_commit": SOURCE_HASHES[digest], "source_sha256": digest,
        "runtime": {"implementation": platform.python_implementation(), "version": platform.python_version(),
                    "os": platform.system(), "os_version": platform.release(), "arch": platform.machine()},
        "observations": observations,
    }


def reconstruct_sentry_event(observed, timestamp=None):
    """Build a labelled API-shaped local replay event; no Sentry interaction."""
    selected = observed["observations"][0]
    if observed["upstream_commit"] != BROKEN_COMMIT or not selected["exception"]:
        raise ValueError("source_did_not_reproduce")
    error = selected["exception"]
    timestamp = timestamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": "452", "eventID": "b0170000000000000000000000000452",
        "dateCreated": timestamp, "platform": "python",
        "release": {"version": RELEASE},
        "tags": [{"key": "environment", "value": "local-retrospective"},
                 {"key": "service", "value": "oss-replay-boltons"}],
        "entries": [
            {"type": "exception", "data": {"values": [{
                "type": error["type"], "value": error["message"],
                "stacktrace": {"frames": [dict(frame) for frame in error["frames"]]},
            }]}},
            {"type": "breadcrumbs", "data": {"values": [{
                "timestamp": timestamp, "category": "local.oss-replay", "level": "info",
                "message": "Retrospective local call: list(backoff_iter(5, 5, factor=1.0)); count omitted.",
            }]}},
        ],
        "contexts": {"runtime": {"name": observed["runtime"]["implementation"], "version": observed["runtime"]["version"]},
                     "os": {"name": observed["runtime"]["os"], "version": observed["runtime"]["os_version"]}},
        "extra": {"classification": "oss_replay", "reconstructed_event": True,
                  "known_answer": True, "nonblind": True, "observed_upstream_commit": BROKEN_COMMIT,
                  "omitted_harness_frames": error["omitted_harness_frames"],
                  "in_app_convention": "True means source belongs to the investigated boltons repository."},
    }


def _write_new_json(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--event-output")
    arguments = parser.parse_args()
    observed = observe_source(arguments.source)
    event = reconstruct_sentry_event(observed) if arguments.event_output else None
    _write_new_json(arguments.output, observed)
    if event is not None:
        _write_new_json(arguments.event_output, event)
    print(json.dumps({"status": "completed", "classification": "oss_replay", "observations": len(observed["observations"])}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
