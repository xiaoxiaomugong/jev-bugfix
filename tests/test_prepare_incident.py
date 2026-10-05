"""Real offline CLI boundary, output lifecycle, V1 bridge and trace version isolation."""
import copy
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
CLI = SCRIPTS / 'prepare_incident.py'


class PrepareIncidentTests(unittest.TestCase):
    def setUp(self):
        self.repository = GitRepository()
        self.addCleanup(self.repository.close)
        self.repo = self.repository.path
        self.repository.write('src/app.py', 'def fail(value):\n    return 1 / value\n')
        self.commit = self.repository.commit()
        self.tmp = tempfile.TemporaryDirectory(prefix='prepare-test-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.count = 0

    def event(self, event_id='synthetic-event', **updates):
        event = {'event_id': event_id, 'service': 'synthetic-service', 'environment': 'production',
                 'release': 'deployment-001', 'commit': self.commit,
                 'exception': {'type': 'ZeroDivisionError', 'message': 'synthetic division',
                               'frames': [{'path': 'src/app.py', 'line': 2, 'function': 'fail', 'in_app': True}]}}
        event.update(updates)
        return event

    def run_prepare(self, events=None, raw=None, extra=(), output=None, input_path=None):
        self.count += 1
        input_path = input_path or self.work / ('input-%d.json' % self.count)
        if raw is not None:
            input_path.write_bytes(raw)
        else:
            input_path.write_text(json.dumps({'schema_version': 1, 'events': events or [self.event()]}), encoding='utf-8')
        output = output or self.work / ('result-%d' % self.count)
        result = subprocess.run([sys.executable, '-B', str(CLI), '--input', str(input_path),
                                 '--repo', str(self.repo), '--output-dir', str(output), *extra],
                                capture_output=True, text=True, timeout=30)
        self.assertNotIn('Traceback', result.stderr)
        report = json.loads(result.stdout)
        evidence = json.loads((output / 'evidence.json').read_text()) if (output / 'evidence.json').exists() else None
        case = json.loads((output / 'case.json').read_text()) if (output / 'case.json').exists() else None
        return result, report, evidence, case, output

    def test_ready_compatible_with_original_ranker_and_no_reproduction_invented(self):
        result, report, evidence, case, output = self.run_prepare()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['status'], 'ready')
        importlib.import_module('rank_candidates').validate_case(case)
        self.assertFalse(case['reviewed_for_secrets'])
        self.assertEqual(case['bug']['reproduction']['steps'], ['尚无本地复现步骤；仅有导入的线上事件'])
        self.assertIn('未知', case['bug']['reproduction']['expected'])
        self.assertEqual(evidence['conclusions']['local_reproduction'], 'not_attempted')
        self.assertEqual(evidence['conclusions']['root_cause'], 'hypothesis')
        self.assertEqual(evidence['conclusions']['fix_verification'], 'unverified')
        self.assertEqual(evidence['conclusions']['regression_attribution'], 'unknown')
        ranking = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'rank_candidates.py'),
                                  '--input', str(output / 'case.json'), '--jev', '/missing/jev'],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(ranking.returncode, 0)
        ranked = json.loads(ranking.stdout)
        self.assertEqual(ranked['status'], 'dry_run')
        self.assertEqual(ranked['usage']['cli_invocations'], 0)
        self.assertEqual(ranked['cli_preflight']['invocations'], 0)

    def test_partial_keeps_usable_candidate_and_visible_gap(self):
        event = self.event()
        event['exception']['frames'].append({'path': 'missing.py', 'line': 1, 'in_app': True})
        result, report, evidence, case, _ = self.run_prepare([event])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report['status'], 'partial')
        self.assertEqual(len(case['candidates']), 1)
        self.assertEqual(len(evidence['unresolved_frames']), 1)

    def test_no_stack_preserves_evidence_and_does_not_forge_candidate(self):
        event = self.event(exception={'type': 'Error', 'message': 'observed with no stack'})
        result, report, evidence, case, _ = self.run_prepare([event])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report['status'], 'needs_input')
        self.assertIsNone(case)
        self.assertEqual(evidence['events'][0]['exception']['message'], 'observed with no stack')

    def test_unknown_event_version_does_not_borrow_current_head(self):
        event = self.event(commit=None, release='deployment-unmapped')
        result, report, evidence, case, _ = self.run_prepare([event])
        self.assertEqual(report['status'], 'needs_input')
        self.assertIsNone(case)
        self.assertEqual(evidence['version']['resolution']['status'], 'unknown')

    def test_multiple_groups_need_explicit_event_id(self):
        events = [self.event(), self.event('second', environment='staging')]
        result, report, evidence, case, _ = self.run_prepare(events)
        self.assertEqual(report['status'], 'needs_input')
        self.assertIsNone(case)
        self.assertEqual(len(evidence['selection']['groups']), 2)
        result, report, evidence, case, _ = self.run_prepare(events, extra=('--event-id', 'second'))
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(evidence['selected_event'], 'second')

    def test_related_trace_events_with_different_or_unknown_commits_stay_separate(self):
        self.repository.write('src/app.py', 'def fail(value):\n    return 0\n')
        new_commit = self.repository.commit()
        trace = {'trace_id': 'synthetic-trace', 'spans': []}
        events = [self.event(trace=trace), self.event('other-version', trace=trace, commit=new_commit),
                  self.event('unknown-version', trace=trace, commit=None)]
        result, report, evidence, case, _ = self.run_prepare(events, extra=('--event-id', 'synthetic-event'))
        self.assertEqual(report['status'], 'partial')
        related = {entry['event_id']: entry for entry in evidence['related_events']}
        self.assertEqual(related['other-version']['relationship'], 'different_event_commit')
        self.assertEqual(related['unknown-version']['relationship'], 'unknown_commit')
        self.assertFalse(any(entry['included_in_version_chain'] for entry in related.values()))
        self.assertEqual(evidence['candidate_provenance'][0]['event_commit'], self.commit)

    def test_missing_trace_id_cannot_create_nearby_chain(self):
        result, _, evidence, _, _ = self.run_prepare([
            self.event(timestamp='2026-10-01T01:00:00Z'),
            self.event('nearby', timestamp='2026-10-01T01:00:01Z')])
        self.assertEqual(evidence['related_events'], [])

    def test_timeline_preserves_submicrosecond_order_and_offsets(self):
        event = self.event(timestamp='2026-10-01T01:00:00.0000009Z')
        event['breadcrumbs'] = [
            {'timestamp': '2026-10-01T09:00:00.0000008+08:00', 'message': 'earlier'},
            {'timestamp': '2026-10-01T01:00:00.0000001Z', 'message': 'earliest'},
            {'message': 'unknown time'}]
        _, _, evidence, _, _ = self.run_prepare([event])
        self.assertEqual([entry.get('message') for entry in evidence['timeline']],
                         ['earliest', 'earlier', None, 'unknown time'])

    def test_checkout_gap_and_baseline_diff_gap_are_partial(self):
        module = importlib.import_module('prepare_incident')
        args = type('Args', (), {'input': str(self.work / 'direct-input.json'), 'repo': str(self.repo),
                                 'event_id': None, 'release_map': None, 'baseline_revision': None,
                                 'source_root': None})()
        Path(args.input).write_text(json.dumps({'schema_version': 1, 'events': [self.event()]}))
        version = {'resolution': {'status': 'resolved', 'event_commit': self.commit, 'clues': [], 'diagnostics': []},
                   'checkout': {'head': self.commit, 'staged': None, 'unstaged': None, 'untracked': None,
                                'comparison': 'same', 'relation': 'same', 'shallow': False},
                   'baseline': None, 'diagnostics': ['git_timeout']}
        with patch.object(module, 'resolve_incident_version', return_value=version):
            evidence, case = module.build_evidence(args)
        self.assertEqual(evidence['status'], 'partial')
        self.assertIsNotNone(case)
        args.baseline_revision = self.commit
        version['checkout'].update(staged=False, unstaged=False, untracked=False)
        version['diagnostics'] = []
        version['baseline'] = {'status': 'resolved', 'commit': self.commit, 'changes': [], 'diagnostics': ['git_output_limit']}
        with patch.object(module, 'resolve_incident_version', return_value=version):
            evidence, case = module.build_evidence(args)
        self.assertEqual(evidence['status'], 'partial')
        self.assertIn('git_output_limit', evidence['diagnostics'])

    def test_sensitive_and_malicious_text_never_leaks_or_executes(self):
        marker = self.work / 'executed'
        event = self.event()
        event['exception']['message'] = ('api_key=DO_NOT_PRINT_SYNTHETIC_TOKEN email=person@example.com '
                                         '<img src="https://example.invalid/tracker"> $(touch ' + str(marker) + ')')
        result, _, evidence, case, output = self.run_prepare([event])
        all_outputs = result.stdout + result.stderr + (output / 'report.md').read_text() + json.dumps(evidence) + json.dumps(case)
        self.assertNotIn('DO_NOT_PRINT_SYNTHETIC_TOKEN', all_outputs)
        self.assertNotIn('person@example.com', all_outputs)
        self.assertNotIn('<img', (output / 'report.md').read_text())
        self.assertFalse(marker.exists())
        self.assertFalse(case['reviewed_for_secrets'])

    def test_redacted_id_collision_cannot_change_representative_or_refs(self):
        events = [self.event('1111111111'), self.event('2222222222')]
        events[0]['exception']['message'] = 'WRONG EVENT'
        events[1]['exception']['message'] = 'SELECTED EVENT'
        _, _, evidence, case, output = self.run_prepare(events, extra=('--event-id', '2222222222'))
        selected_ref = evidence['selected_event_ref']
        selected = next(e for e in evidence['events'] if e['event_ref'] == selected_ref)
        self.assertEqual(selected['exception']['message'], 'SELECTED EVENT')
        self.assertEqual(evidence['candidate_provenance'][0]['evidence_refs'][0]['event_ref'], selected_ref)
        self.assertEqual(len(set(e['event_ref'] for e in evidence['events'])), 2)
        self.assertIn('SELECTED EVENT', (output / 'report.md').read_text())
        self.assertNotIn('WRONG EVENT', (output / 'report.md').read_text())
        self.assertIn('SELECTED EVENT', case['bug']['description'])

    def test_invalid_input_produces_error_evidence_before_git(self):
        result, report, evidence, case, _ = self.run_prepare(raw=b'{"schema_version":1,"events":[],"events":[]}')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report['status'], 'error')
        self.assertEqual(evidence['status'], 'error')
        self.assertIsNone(evidence['version'])
        self.assertIsNone(case)

    def test_existing_output_is_refused_without_replacing_old_case(self):
        output = self.work / 'old'
        output.mkdir()
        (output / 'case.json').write_text('OLD CASE')
        input_path = self.work / 'input.json'
        input_path.write_text(json.dumps({'schema_version': 1, 'events': [self.event()]}))
        result = subprocess.run([sys.executable, '-B', str(CLI), '--input', str(input_path),
                                 '--repo', str(self.repo), '--output-dir', str(output)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        report = json.loads(result.stdout)
        self.assertEqual(report['status'], 'error')
        self.assertIsNone(report['artifacts'])
        self.assertIn('output_not_empty', report['diagnostics'])
        self.assertEqual((output / 'case.json').read_text(), 'OLD CASE')

    def test_empty_output_directory_is_accepted(self):
        output = self.work / 'empty'
        output.mkdir()
        result, report, _, case, _ = self.run_prepare(output=output)
        self.assertEqual(report['status'], 'ready')
        self.assertIsNotNone(case)

    def test_output_install_failure_removes_all_new_artifacts(self):
        module = importlib.import_module('prepare_incident')
        input_path = self.work / 'write-failure.json'
        input_path.write_text(json.dumps({'schema_version': 1, 'events': [self.event()]}))
        output = self.work / 'write-failure-output'
        args = type('Args', (), {'input': str(input_path), 'repo': str(self.repo),
                                 'output_dir': str(output), 'event_id': None, 'release_map': None,
                                 'baseline_revision': None, 'source_root': None})()
        real_link = module.os.link
        def fail_last(source, destination):
            if Path(destination).name == 'case.json':
                raise OSError('synthetic write failure')
            real_link(source, destination)
        with patch.object(module.os, 'link', side_effect=fail_last):
            report = module.prepare_incident(args)
        self.assertEqual(report['status'], 'error')
        self.assertIsNone(report['artifacts'])
        self.assertIn('output_write_failed', report['diagnostics'])
        self.assertEqual(list(output.iterdir()), [])

    def test_candidate_count_budget_keeps_positions_without_case(self):
        for i in range(13):
            self.repository.write('src/f%d.py' % i, 'raise RuntimeError("synthetic")\n')
        commit = self.repository.commit()
        event = self.event(commit=commit)
        event['exception']['frames'] = [{'path': 'src/f%d.py' % i, 'line': 1, 'in_app': True} for i in range(13)]
        result, report, evidence, case, _ = self.run_prepare([event])
        self.assertEqual(report['status'], 'needs_input')
        self.assertIsNone(case)
        self.assertEqual(len(evidence['candidate_provenance']), 13)
        self.assertIn('candidates_over_budget', evidence['diagnostics'])

    def test_total_payload_budget_is_exact_and_not_silent_truncation(self):
        for i in range(12):
            self.repository.write('src/f%d.py' % i, 'return "' + 'x' * 1800 + '"\n')
        commit = self.repository.commit()
        event = self.event(commit=commit)
        event['exception']['message'] = 'M' * 3000
        event['exception']['frames'] = [{'path': 'src/f%d.py' % i, 'line': 1, 'in_app': True} for i in range(12)]
        result, report, evidence, case, _ = self.run_prepare([event])
        self.assertEqual(report['status'], 'needs_input')
        self.assertIsNone(case)
        self.assertIn('candidates_over_budget', evidence['diagnostics'])
        self.assertEqual(len(evidence['candidate_provenance']), 12)
        self.assertGreater(evidence['budgets']['payload_bytes'], 24576)

    def test_explicit_baseline_only_and_dirty_workspace_remains_unchanged(self):
        self.repository.write('src/app.py', 'def fail(value):\n    return 0\n')
        baseline = self.repository.commit()
        self.repository.write('src/app.py', 'LOCAL DIRTY\n')
        self.repository.write('new.txt', 'UNTRACKED\n')
        before = self.repository.git('-c', 'core.fsmonitor=false', 'status', '--porcelain')
        result, report, evidence, case, _ = self.run_prepare()
        self.assertIsNone(evidence['version']['baseline'])
        result, report, evidence, case, _ = self.run_prepare(extra=('--baseline-revision', baseline))
        self.assertEqual(evidence['version']['baseline']['commit'], baseline)
        self.assertEqual(evidence['version']['resolution']['event_commit'], self.commit)
        self.assertEqual(self.repository.git('-c', 'core.fsmonitor=false', 'status', '--porcelain'), before)
        self.assertEqual((self.repo / 'src/app.py').read_text(), 'LOCAL DIRTY\n')

    def test_release_map_conflict_is_not_hidden_by_event_commit(self):
        self.repository.write('src/app.py', 'return 0\n')
        other = self.repository.commit()
        mapping = self.work / 'release-map.json'
        mapping.write_text(json.dumps({'schema_version': 1, 'entries': [
            {'service': 'synthetic-service', 'environment': 'production', 'release': 'deployment-001', 'revision': other}]}))
        _, report, evidence, case, _ = self.run_prepare(extra=('--release-map', str(mapping)))
        self.assertEqual(evidence['version']['resolution']['status'], 'ambiguous')
        self.assertEqual(report['status'], 'needs_input')
        self.assertIsNone(case)

    def test_long_summary_records_omission_and_preserves_all_sidecar_frames(self):
        event = self.event()
        event['exception']['message'] = '观察' * 600
        event['exception']['frames'] *= 9
        _, report, evidence, case, _ = self.run_prepare([event])
        self.assertEqual(report['status'], 'ready')
        self.assertLessEqual(len(case['bug']['description'].encode()), 1024)
        self.assertLessEqual(len(case['bug']['reproduction']['actual'].encode()), 1024)
        self.assertEqual(len(case['bug']['stack_trace']), 8)
        self.assertEqual(len(evidence['frames']), 9)
        self.assertTrue(evidence['summary_omissions'])

    def test_argparse_failure_suppresses_untrusted_option_value(self):
        result = subprocess.run([sys.executable, '-B', str(CLI), '--unknown', 'api_key=DO_NOT_PRINT'],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('DO_NOT_PRINT', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
