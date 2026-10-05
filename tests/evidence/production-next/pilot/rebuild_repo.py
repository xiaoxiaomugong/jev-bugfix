#!/usr/bin/env python3
"""Rebuild the minimal, deterministic boltons replay Git fixture offline.

The two generated commits describe fixture snapshots, not upstream history.
Only pinned upstream source bytes are stored; no downloads or package installs.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


SOURCE_HASHES = {
    "broken": "c5cf8da7231fcb72f14f7f5507ff7c0b8778e5c495d69c596c4eb2ef48e8cfda",
    "fixed": "77ec0e8d53b060f236d06f7c3c8ed107d34818dd8cf1b2a6b986bdea1167effe",
}
FIXTURE_REVISIONS = {
    "broken": "eb110f9f84ca0bdc0d9dac833d61859e15a3d527",
    "fixed": "60264f0eae8b171ac46c5ecbf487d1304c3570ab",
}


def _environment():
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Offline replay fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Offline replay fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        "LC_ALL": "C",
    })
    return environment


def _git(repo, arguments, payload=None, timestamp=None):
    environment = _environment()
    if timestamp is not None:
        environment.update({"GIT_AUTHOR_DATE": timestamp, "GIT_COMMITTER_DATE": timestamp})
    command = ["git", "-c", "protocol.allow=never", "-c", "core.hooksPath=" + os.devnull,
               "-c", "core.fsmonitor=false", "-c", "core.attributesFile=" + os.devnull,
               "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
               "-c", "credential.helper=", "-C", str(repo)] + arguments
    result = subprocess.run(command, input=payload, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=environment, timeout=10)
    if result.returncode != 0:
        raise ValueError("git_failed")
    return result.stdout.decode("ascii").strip()


def rebuild(output):
    pilot = Path(__file__).resolve().parent
    sources = {}
    for role in ("broken", "fixed"):
        raw = (pilot / "source" / role / "iterutils.py").read_bytes()
        if hashlib.sha256(raw).hexdigest() != SOURCE_HASHES[role]:
            raise ValueError("source_hash_mismatch")
        sources[role] = raw
    repo = Path(output).absolute()
    try:
        repo.mkdir()
    except FileExistsError:
        raise ValueError("output_exists")
    try:
        _git(repo, ["init", "--quiet", "--template=", "--object-format=sha1",
                    "--initial-branch=oss-pilot-fixed"])
        parent = None
        revisions = {}
        for index, role in enumerate(("broken", "fixed")):
            blob = _git(repo, ["hash-object", "-w", "--stdin"], sources[role])
            subtree = _git(repo, ["mktree"], ("100644 blob " + blob + "\titerutils.py\n").encode("ascii"))
            tree = _git(repo, ["mktree"], ("040000 tree " + subtree + "\tboltons\n").encode("ascii"))
            arguments = ["commit-tree", tree]
            if parent is not None:
                arguments.extend(["-p", parent])
            message = ("Offline boltons replay: " + role + " source\n").encode("ascii")
            revision = _git(repo, arguments, message, str(946684800 + index) + " +0000")
            if revision != FIXTURE_REVISIONS[role]:
                raise ValueError("fixture_revision_mismatch")
            _git(repo, ["update-ref", "refs/heads/oss-pilot-" + role, revision])
            parent = revision
            revisions[role] = revision
        _git(repo, ["checkout", "--quiet", "oss-pilot-fixed"])
    except Exception:
        # This directory was created exclusively by this invocation. Existing
        # output directories are refused before any modification.
        shutil.rmtree(repo)
        raise
    return {"schema_version": 1, "status": "rebuilt", "repo": str(repo),
            "classification": "oss_replay", "fixture_revisions": revisions,
            "upstream_history": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New directory; its parent must already exist.")
    arguments = parser.parse_args()
    try:
        result = rebuild(arguments.output)
    except ValueError as error:
        result = {"schema_version": 1, "status": "error", "diagnostics": [str(error)]}
    except (OSError, subprocess.SubprocessError):
        result = {"schema_version": 1, "status": "error", "diagnostics": ["rebuild_unavailable"]}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "rebuilt" else 2


if __name__ == "__main__":
    sys.exit(main())
