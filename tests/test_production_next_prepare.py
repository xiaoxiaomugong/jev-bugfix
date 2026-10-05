"""Offline new preparation entry points without changing the V1 scorer."""
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from incident_test_support import GitRepository

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/jev-bugfix/scripts'
sys.path.insert(0, str(SCRIPTS))


class NextPreparationTests(unittest.TestCase):
    def setUp(self):
        self.repository = GitRepository()
        self.addCleanup(self.repository.close)
        self.repo = self.repository.path
        self.repository.write('app.py', 'def fail(value):\n    return 1 / value\n')
        self.commit = self.repository.commit()
        self.tmp = tempfile.TemporaryDirectory(prefix='next-prepare-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.count = 0
        self.events = {'schema_version': 1, 'events': [{'event_id': 'next-event',
            'service': 'next-service', 'environment': 'production', 'release': 'deployment-next',
            'commit': self.commit, 'exception': {'type': 'ZeroDivisionError', 'message': 'synthetic divide',
            'frames': [{'path': 'app.py', 'line': 2, 'function': 'fail', 'in_app': True}]}}]}

    def prepare(self, data=None, extra=()):
        self.count += 1
        path = self.work / ('input-%d.json' % self.count)
        path.write_text(json.dumps(self.events if data is None else data))
        output = self.work / ('output-%d' % self.count)
        result = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'prepare_incident.py'),
                                 '--input', str(path), '--repo', str(self.repo), '--output-dir', str(output),
                                 *extra], capture_output=True, text=True, timeout=30)
        self.assertNotIn('Traceback', result.stderr)
        report = json.loads(result.stdout)
        return result, report, output

    def test_case_is_transactionally_bound_and_can_be_verified(self):
        result, report, output = self.prepare()
        self.assertEqual(result.returncode, 0)
        self.assertIn('bundle.json', report['artifacts'])
        verified = importlib.import_module('incident_bundle').verify_bundle(output / 'bundle.json', self.repo)
        self.assertEqual(verified['status'], 'verified')
        self.assertEqual(verified['checked_candidates'], 1)

    def test_bug_context_reprepares_new_bundle_and_preserves_original(self):
        _, _, original = self.prepare()
        old_case = (original / 'case.json').read_bytes()
        context = self.work / 'context.json'
        context.write_text(json.dumps({'description': 'User supplied focused observation',
            'reproduction': {'steps': ['Run fail(0) locally'], 'expected': 'Needs business confirmation',
                            'actual': 'User says it was reproduced; claim unverified'}}))
        _, report, output = self.prepare(extra=('--bug-context', str(context)))
        self.assertEqual(report['status'], 'ready')
        case = json.loads((output / 'case.json').read_text())
        evidence = json.loads((output / 'evidence.json').read_text())
        self.assertFalse(case['reviewed_for_secrets'])
        self.assertEqual(case['bug']['description'], 'User supplied focused observation')
        self.assertEqual(case['bug']['reproduction']['steps'], ['Run fail(0) locally'])
        self.assertEqual(evidence['conclusions']['local_reproduction'], 'not_attempted')
        self.assertIs(evidence['bug_context']['user_supplied_unverified'], True)
        self.assertEqual(evidence['bug_context']['file_sha256'],
                         hashlib.sha256(context.read_bytes()).hexdigest())
        self.assertEqual(evidence['bug_context']['fields'], ['description', 'reproduction'])
        self.assertNotIn('User supplied focused observation', json.dumps(evidence['bug_context']))
        self.assertEqual((original / 'case.json').read_bytes(), old_case)
        self.assertEqual(importlib.import_module('incident_bundle').verify_bundle(
            output / 'bundle.json', self.repo)['status'], 'verified')

    def test_bug_context_cannot_override_stack_candidates_or_conclusions(self):
        context = self.work / 'invalid-context.json'
        for value in ({'stack_trace': []}, {'reviewed_for_secrets': True}, {},
                      {'conclusions': {'root_cause': 'confirmed'}}, {'reproduction': {'steps': ['a']}}):
            with self.subTest(value=value):
                context.write_text(json.dumps(value))
                _, report, output = self.prepare(extra=('--bug-context', str(context)))
                self.assertEqual(report['status'], 'error')
                self.assertFalse((output / 'case.json').exists())
                self.assertFalse((output / 'bundle.json').exists())

    def test_bug_context_sensitive_shared_text_is_rejected_without_leak(self):
        context = self.work / 'sensitive-context.json'
        context.write_text(json.dumps({'description': 'api_key=DO_NOT_PRINT_NEXT_SECRET'}))
        result, report, output = self.prepare(extra=('--bug-context', str(context)))
        self.assertEqual(report['status'], 'error')
        all_output = result.stdout + result.stderr + (output / 'report.md').read_text() + (output / 'evidence.json').read_text()
        self.assertNotIn('DO_NOT_PRINT_NEXT_SECRET', all_output)
        self.assertFalse((output / 'case.json').exists())

    def test_bug_context_file_and_depth_limits_precede_git(self):
        context = self.work / 'over-context.json'
        module = importlib.import_module('prepare_incident')
        for raw in (b' ' * 8193, b'[' * 9 + b'0' + b']' * 9,
                    b'{"description":"ok","description":"duplicate"}',
                    b'{"description":"\\ud800"}', b'{"description":NaN}'):
            context.write_bytes(raw)
            args = type('Args', (), {'input': str(self.work / 'direct-events.json'),
                'repo': str(self.repo), 'event_id': None, 'release_map': None,
                'baseline_revision': None, 'source_root': None, 'bug_context': str(context),
                'input_format': 'production-events/v1', 'exception_index': None, 'service': None})()
            Path(args.input).write_text(json.dumps(self.events))
            with patch.object(module, 'GitSession', side_effect=AssertionError('Git must not start')):
                evidence, case = module.build_evidence(args)
            self.assertEqual(evidence['status'], 'error')
            self.assertIsNone(case)

    def test_evidence_output_limit_cannot_create_unverifiable_success(self):
        module = importlib.import_module('prepare_incident')
        args = type('Args', (), {'input': str(self.work / 'direct-events.json'),
            'repo': str(self.repo), 'output_dir': str(self.work / 'limit-output'), 'event_id': None,
            'release_map': None, 'baseline_revision': None, 'source_root': None,
            'bug_context': None, 'input_format': 'production-events/v1', 'exception_index': None,
            'service': None})()
        Path(args.input).write_text(json.dumps(self.events))
        with patch.object(module, 'EVIDENCE_OUTPUT_LIMIT', 10):
            report = module.prepare_incident(args)
        self.assertEqual(report['status'], 'error')
        self.assertIsNone(report['artifacts'])
        self.assertFalse((Path(args.output_dir) / 'case.json').exists())
        self.assertFalse((Path(args.output_dir) / 'bundle.json').exists())

    def test_sentry_only_flags_rejected_with_old_input_format(self):
        for extra in (('--service', 'service'), ('--exception-index', '0')):
            result, report, output = self.prepare(extra=extra)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(report['status'], 'error')
            self.assertIn('input_format_arguments_invalid', report['diagnostics'])
            self.assertFalse((output / 'case.json').exists())

    def sentry_event(self):
        return {'id': 'different-api-row-id', 'eventID': 'next-sentry-event', 'platform': 'python',
                'dateCreated': '2026-10-05T00:00:00Z', 'release': {'version': 'deployment-next'},
                'tags': [{'key': 'service', 'value': 'next-service'},
                         {'key': 'environment', 'value': 'production'}],
                'entries': [{'type': 'exception', 'data': {'values': [{'type': 'ZeroDivisionError',
                    'value': 'synthetic divide', 'stacktrace': {'frames': [
                        {'filename': 'app.py', 'lineNo': 2, 'function': 'fail', 'inApp': True}]}}]}}],
                'contexts': {'trace': {'trace_id': 'next-trace', 'span_id': 'next-span'}}}

    def mapping(self):
        path = self.work / 'release-map.json'
        path.write_text(json.dumps({'schema_version': 1, 'entries': [{'service': 'next-service',
            'environment': 'production', 'release': 'deployment-next', 'revision': self.commit}]}))
        return path

    def test_sentry_api_profile_projects_ignored_fields_and_generates_verified_bundle(self):
        event = self.sentry_event()
        event['request'] = {'headers': {'Authorization': 'SECRET_NEXT_IGNORED'}, 'data': 'x' * 16000}
        event['SECRET_UNKNOWN_KEY'] = 'SECRET_NEXT_IGNORED'
        event['entries'][0]['data']['values'][0]['stacktrace']['frames'][0]['vars'] = {'api_key': 'SECRET_NEXT_IGNORED'}
        _, report, output = self.prepare(event, ('--input-format', 'sentry-api-event/v1',
            '--release-map', str(self.mapping())))
        self.assertEqual(report['status'], 'ready')
        evidence = json.loads((output / 'evidence.json').read_text())
        self.assertEqual(evidence['events'][0]['event_id'], 'next-sentry-event')
        self.assertIsNone(evidence['events'][0]['commit'])
        self.assertIn('import_provenance', evidence)
        for path in output.iterdir():
            self.assertNotIn('SECRET_NEXT_IGNORED', path.read_text())
            self.assertNotIn('SECRET_UNKNOWN_KEY', path.read_text())
        self.assertEqual(importlib.import_module('incident_bundle').verify_bundle(
            output / 'bundle.json', self.repo)['status'], 'verified')

    def test_sentry_unselected_exception_chain_stays_partial(self):
        event = self.sentry_event()
        event['entries'][0]['data']['values'].append({'type': 'WrapperError', 'value': 'wrapper synthetic',
            'stacktrace': {'frames': [{'filename': 'app.py', 'lineNo': 1, 'inApp': True}]}})
        _, report, output = self.prepare(event, ('--input-format', 'sentry-api-event/v1',
            '--release-map', str(self.mapping())))
        self.assertEqual(report['status'], 'needs_input')
        self.assertFalse((output / 'bundle.json').exists())
        _, report, output = self.prepare(event, ('--input-format', 'sentry-api-event/v1',
            '--exception-index', '0', '--release-map', str(self.mapping())))
        self.assertEqual(report['status'], 'partial')
        self.assertTrue((output / 'case.json').exists())
        self.assertEqual(importlib.import_module('incident_bundle').verify_bundle(
            output / 'bundle.json', self.repo)['status'], 'verified')

    def test_sentry_missing_inapp_remains_unknown_gap(self):
        event = self.sentry_event()
        del event['entries'][0]['data']['values'][0]['stacktrace']['frames'][0]['inApp']
        _, report, output = self.prepare(event, ('--input-format', 'sentry-api-event/v1',
            '--release-map', str(self.mapping())))
        self.assertEqual(report['status'], 'needs_input')
        evidence = json.loads((output / 'evidence.json').read_text())
        self.assertIsNone(evidence['events'][0]['exception']['frames'][0]['in_app'])
        self.assertFalse((output / 'bundle.json').exists())


if __name__ == '__main__':
    unittest.main()
