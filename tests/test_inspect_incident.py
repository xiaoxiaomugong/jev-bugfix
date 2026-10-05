"""Bound historical context and source-free CLI reports."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_incident_bundle import BundleFixture, SCRIPTS


class InspectIncidentTests(BundleFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def read(self, report=None, lines=20, output=None):
        return self.module.read_candidate_context(report if report is not None else self.verify(),
                                                   self.case['candidates'][0]['id'], lines,
                                                   output or self.work / 'source.txt')

    def cli(self, *extra):
        result = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'inspect_incident.py'),
                                 '--bundle', str(self.output / 'bundle.json'), '--repo', str(self.repo),
                                 *map(str, extra)], capture_output=True, text=True, timeout=30)
        self.assertNotIn('Traceback', result.stderr)
        return result

    def test_context_exports_exact_whole_lines_and_only_metadata(self):
        result = self.read(lines=2)
        expected = ''.join('line %03d\n' % n for n in range(19, 83))
        self.assertEqual(result['status'], 'written')
        self.assertEqual((self.work / 'source.txt').read_text(), expected)
        self.assertEqual(result['sha256'], hashlib.sha256(expected.encode()).hexdigest())
        self.assertEqual((result['start_line'], result['end_line']), (19, 82))
        self.assertNotIn('line 050', json.dumps(result))

    def test_zero_context_keeps_only_bound_candidate(self):
        self.assertEqual(self.read(lines=0)['status'], 'written')
        self.assertEqual((self.work / 'source.txt').read_text(), self.case['candidates'][0]['snippet'])

    def test_plain_forged_or_copied_verified_dict_cannot_authorize_source(self):
        for report in ({'status': 'verified', 'verified': True}, dict(self.verify())):
            self.assertEqual(self.read(report=report)['status'], 'invalid')
            self.assertFalse((self.work / 'source.txt').exists())

    def test_changed_case_after_verification_blocks_read(self):
        verified = self.verify()
        self.case['bug']['description'] = 'modified after verification'
        self.write_bundle()
        self.assertEqual(self.read(report=verified)['status'], 'invalid')
        self.assertFalse((self.work / 'source.txt').exists())

    def test_replaced_self_consistent_bundle_after_verification_blocks_read(self):
        verified = self.verify()
        self.evidence['input']['sha256'] = 'a' * 64
        self.rebind()
        self.assertEqual(self.read(report=verified)['status'], 'invalid')

    def test_context_reads_event_blob_after_head_rename_and_dirty(self):
        verified = self.verify()
        self.repository.git('mv', 'src/app.py', 'src/current.py')
        self.repository.commit()
        self.repository.write('src/current.py', 'CURRENT DIRTY SOURCE\n')
        result = self.read(report=verified)
        self.assertEqual(result['status'], 'written')
        self.assertEqual((self.work / 'source.txt').read_text(), self.source)

    def test_context_limit_values_fail_without_creating_file(self):
        for value in (-1, 61, True, 1.5, '2'):
            self.assertEqual(self.read(lines=value)['status'], 'invalid')
            self.assertFalse((self.work / 'source.txt').exists())

    def test_existing_file_directory_and_symlink_are_preserved(self):
        existing = self.work / 'existing.txt'
        existing.write_text('KEEP')
        link = self.work / 'link.txt'
        link.symlink_to(existing)
        for output in (existing, self.work, link):
            self.assertEqual(self.read(output=output)['status'], 'invalid')
        self.assertEqual(existing.read_text(), 'KEEP')
        self.assertTrue(link.is_symlink())

    def test_source_output_parent_symlink_is_refused(self):
        real = self.work / 'real'
        real.mkdir()
        alias = self.work / 'alias'
        alias.symlink_to(real, target_is_directory=True)
        self.assertEqual(self.read(output=alias / 'source.txt')['status'], 'invalid')
        self.assertFalse((real / 'source.txt').exists())

    def test_source_exceeding_output_limit_fails_without_half_line(self):
        self.repository.write('src/app.py', 'x' * 300 + '\n' + 'short\n' + 'x' * 17000 + '\n')
        self.commit = self.repository.commit()
        self.case['candidates'][0].update(start_line=2, end_line=2, snippet='short\n')
        provenance = self.evidence['candidate_provenance'][0]
        provenance.update(event_commit=self.commit, start_line=2, end_line=2,
                          snippet_sha256=hashlib.sha256(b'short\n').hexdigest(),
                          blob_oid=self.repository.git('rev-parse', self.commit + ':src/app.py'))
        self.evidence['version']['resolution']['event_commit'] = self.commit
        self.evidence['events'][0]['exception']['frames'][0]['line'] = 2
        self.rebind()
        result = self.read(lines=1)
        self.assertEqual(result['status'], 'invalid')
        self.assertIn('context_output_limit', result['diagnostics'])
        self.assertFalse((self.work / 'source.txt').exists())

    def test_shared_git_budget_is_not_reset_for_context(self):
        verified = self.verify()
        with patch('incident_versions.time.monotonic', return_value=10 ** 12):
            result = self.read(report=verified)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('git_budget_exceeded', result['diagnostics'])
        self.assertFalse((self.work / 'source.txt').exists())

    def test_cli_verifies_without_printing_source(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['status'], 'verified')
        self.assertNotIn('line 050', result.stdout + result.stderr)

    def test_cli_exports_context_and_keeps_stdout_source_free(self):
        target = self.work / 'export.txt'
        result = self.cli('--candidate-id', self.case['candidates'][0]['id'], '--context-lines', 0,
                          '--source-output', target)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['context']['status'], 'written')
        self.assertEqual(target.read_text(), self.case['candidates'][0]['snippet'])
        self.assertNotIn('line 050', result.stdout + result.stderr)

    def test_cli_rejects_partial_source_arguments_and_suppresses_untrusted_text(self):
        for extra in (('--candidate-id', 'api_key=PRIVATE_MARKER'), ('--context-lines', '61'),
                      ('--unknown', 'api_key=PRIVATE_MARKER')):
            result = self.cli(*extra)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('PRIVATE_MARKER', result.stdout + result.stderr)

    def test_cli_empty_source_arguments_are_invalid(self):
        result = self.cli('--candidate-id', '', '--source-output', '')
        self.assertEqual(result.returncode, 2)

    def test_cli_missing_manifest_reports_legacy_and_exit_two(self):
        (self.output / 'bundle.json').unlink()
        result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['status'], 'legacy_unbound')

    def test_fifo_artifact_is_rejected_without_waiting_for_a_writer(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('POSIX FIFO unavailable')
        path = self.output / 'evidence.json'
        path.unlink()
        os.mkfifo(path)
        try:
            result = subprocess.run([sys.executable, '-B', str(SCRIPTS / 'inspect_incident.py'),
                                     '--bundle', str(self.output / 'bundle.json'), '--repo', str(self.repo)],
                                    capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail('artifact reader blocked on a FIFO')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['status'], 'invalid')


if __name__ == '__main__':
    unittest.main()
