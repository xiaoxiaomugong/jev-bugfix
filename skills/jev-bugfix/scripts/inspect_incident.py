#!/usr/bin/env python3
"""Verify a local incident bundle and optionally export frozen source context."""
import argparse
import json
import sys

from incident_bundle import read_candidate_context, verify_bundle


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'invalid_arguments\n')


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--candidate-id')
    parser.add_argument('--context-lines', type=int)
    parser.add_argument('--source-output')
    args = parser.parse_args(argv)
    if ((args.candidate_id is not None) != (args.source_output is not None) or
            args.candidate_id is not None and (not args.candidate_id or not args.source_output) or
            args.context_lines is not None and (not args.candidate_id or not 0 <= args.context_lines <= 60)):
        parser.error('invalid arguments')
    verified = verify_bundle(args.bundle, args.repo)
    report = dict(verified)
    exit_code = 0 if report['status'] == 'verified' else 2
    if args.candidate_id and report['status'] == 'verified':
        report['context'] = read_candidate_context(verified, args.candidate_id,
                                                   20 if args.context_lines is None else args.context_lines,
                                                   args.source_output)
        if report['context']['status'] != 'written':
            exit_code = 2
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
