#!/usr/bin/env python3
"""Explicit live integration check; never imported by offline test discovery."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jev", help="Installed Jev executable; defaults to PATH")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable,
               str(root / "skills/jev-bugfix/scripts/rank_candidates.py"),
               "--input", str(root / "tests/fixtures/smoke_case.json"), "--execute"]
    if args.jev:
        command.extend(["--jev", args.jev])
    result = subprocess.run(command, capture_output=True, timeout=50, check=False)
    try:
        report = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        print("Live smoke did not return a JSON report; raw output suppressed.")
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if result.returncode or report.get("status") != "ranked":
        print("Live integration unverified; see recorded fallback above.", file=sys.stderr)
        return 2
    print("Live CLI contract verified for one synthetic candidate.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
