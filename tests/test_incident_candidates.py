"""Offline candidate provenance and path confinement against real Git blobs."""
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'skills/jev-bugfix/scripts'
sys.path.insert(0, str(SCRIPTS))


class IncidentCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='incident-candidates-')
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic')
        self.git('config', 'user.email', 'synthetic@example.invalid')
        self.write('src/app.py', 'def fail(value):\n    return 1 / value\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'event version')
        self.commit = self.git('rev-parse', 'HEAD').strip()

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'core.fsmonitor=false', *args],
                                       cwd=self.repo, text=True, stderr=subprocess.DEVNULL)

    def write(self, path, text):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')

    def collect(self, paths=None, commit=None, **kwargs):
        module = importlib.import_module('incident_candidates')
        frames = paths if paths is not None else [self.frame('src/app.py', 2)]
        selected = {'event_id': 'synthetic-event', 'exception': {'frames': frames},
                    'provenance': {'indices': [0]}}
        version = {'resolution': {'status': 'resolved', 'event_commit': commit or self.commit}}
        return module.collect_candidates(self.repo, selected, version, **kwargs)

    @staticmethod
    def frame(path, line=1, in_app=True):
        return {'path': path, 'line': line, 'function': 'fail', 'in_app': in_app}

    def test_event_blob_not_dirty_or_renamed_head(self):
        self.git('mv', 'src/app.py', 'src/renamed.py')
        self.write('src/renamed.py', '# different lines\n# at HEAD\ndef fail(value):\n    return 0\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'head changed')
        self.write('src/renamed.py', 'UNCOMMITTED\n')
        before = self.git('status', '--porcelain')
        result = self.collect()
        candidate = result['candidates'][0]
        self.assertEqual(candidate['snippet'], 'def fail(value):\n    return 1 / value\n')
        provenance = result['provenance'][0]
        self.assertEqual(provenance['event_commit'], self.commit)
        self.assertEqual(provenance['blob_oid'], self.git('rev-parse', self.commit + ':src/app.py').strip())
        self.assertEqual(provenance['snippet_sha256'], hashlib.sha256(candidate['snippet'].encode()).hexdigest())
        self.assertEqual(provenance['evidence_refs'][0]['frame_index'], 0)
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.assertEqual(result['candidates'], self.collect()['candidates'])

    def test_absolute_path_requires_source_root_and_segment_boundary(self):
        self.assertFalse(self.collect([self.frame('/srv/app/src/app.py', 2)])['candidates'])
        result = self.collect([self.frame('/srv/app/src/app.py', 2)], source_root='/srv/app')
        self.assertEqual(result['candidates'][0]['path'], 'src/app.py')
        self.assertFalse(self.collect([self.frame('/srv/application/src/app.py', 2)],
                                      source_root='/srv/app')['candidates'])

    def test_windows_mapping_is_explicit_and_drive_aware(self):
        result = self.collect([self.frame(r'C:\service\app\src\app.py', 2)],
                              source_root=r'C:\service\app')
        self.assertEqual(result['candidates'][0]['path'], 'src/app.py')
        self.assertFalse(self.collect([self.frame(r'D:\service\app\src\app.py')],
                                      source_root=r'C:\service\app')['candidates'])

    def test_windows_mapping_does_not_normalize_invalid_segments(self):
        for path in (r'C:\service\app\src\.\app.py', r'C:\service\app\src\\app.py',
                     r'C:\service\app\src\..\src\app.py'):
            with self.subTest(path=path):
                self.assertFalse(self.collect([self.frame(path, 2)], source_root=r'C:\service\app')['candidates'])

    def test_traversal_and_v1_invalid_paths_are_never_read(self):
        for path in ('../src/app.py', 'src/../src/app.py', '/srv/app/../app/src/app.py',
                     'src//app.py', 'src/./app.py', 'src/app.py\x00', 'C:src/app.py'):
            with self.subTest(path=path):
                self.assertFalse(self.collect([self.frame(path)], source_root='/srv/app')['candidates'])

    def test_symlink_and_submodule_are_rejected(self):
        (self.repo / 'link.py').symlink_to('src/app.py')
        (self.repo / 'dirlink').symlink_to('src', target_is_directory=True)
        self.git('add', '.')
        self.git('update-index', '--add', '--cacheinfo', '160000,' + self.commit + ',vendor')
        self.git('commit', '-qm', 'unsafe tree entries')
        commit = self.git('rev-parse', 'HEAD').strip()
        for path in ('link.py', 'dirlink/app.py', 'vendor/src/app.py'):
            with self.subTest(path=path):
                result = self.collect([self.frame(path)], commit=commit)
                self.assertFalse(result['candidates'])
                self.assertEqual(len(result['unresolved_frames']), 1)

    def test_build_artifacts_are_explicit_gaps(self):
        for path in ('dist/app.js', 'src/app.min.js', 'src/app.js.map'):
            self.write(path, 'throw new Error("synthetic");\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'generated artifacts')
        commit = self.git('rev-parse', 'HEAD').strip()
        result = self.collect([self.frame(p) for p in ('dist/app.js', 'src/app.min.js', 'src/app.js.map')], commit=commit)
        self.assertFalse(result['candidates'])
        self.assertTrue(all(f['code'] == 'build_artifact_unmapped' for f in result['unresolved_frames']))

    def test_all_frames_preserved_and_partial_candidate_retained(self):
        result = self.collect([self.frame('src/app.py', 2), self.frame('missing.py'),
                               self.frame('src/app.py', 300), self.frame('stdlib.py', in_app=False),
                               self.frame('src/app.py', None), self.frame('src/app.py', in_app=None)])
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual(len(result['frames']), 6)
        self.assertEqual(len(result['unresolved_frames']), 5)

    def test_deduplicated_intervals_preserve_all_frame_refs(self):
        result = self.collect([self.frame('src/app.py', 1), self.frame('src/app.py', 2)])
        self.assertEqual(len(result['candidates']), 1)
        self.assertEqual([r['frame_index'] for r in result['provenance'][0]['evidence_refs']], [0, 1])

    def test_whole_lines_and_exception_line_stay_within_budget(self):
        self.write('src/app.py', ''.join('line%03d_' % i + 'x' * 70 + '\n' for i in range(100)))
        self.git('add', '.')
        self.git('commit', '-qm', 'long context')
        result = self.collect([self.frame('src/app.py', 50)], commit=self.git('rev-parse', 'HEAD').strip())
        candidate = result['candidates'][0]
        self.assertLessEqual(len(candidate['snippet'].encode()), 2048)
        self.assertLessEqual(len(candidate['snippet'].splitlines()), 60)
        self.assertLessEqual(candidate['start_line'], 50)
        self.assertGreaterEqual(candidate['end_line'], 50)
        self.assertTrue(all(len(line) == 78 for line in candidate['snippet'].splitlines()))

    def test_overlong_error_line_never_truncated(self):
        self.write('src/app.py', 'x' * 2049 + '\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'oversize line')
        result = self.collect([self.frame('src/app.py')], commit=self.git('rev-parse', 'HEAD').strip())
        self.assertFalse(result['candidates'])
        self.assertEqual(result['unresolved_frames'][0]['code'], 'source_line_over_budget')

    def test_over_12_candidates_preserves_complete_position_inventory(self):
        for i in range(13):
            self.write('src/f%d.py' % i, 'raise RuntimeError("synthetic")\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'many candidates')
        result = self.collect([self.frame('src/f%d.py' % i) for i in range(13)],
                              commit=self.git('rev-parse', 'HEAD').strip())
        self.assertEqual(len(result['provenance']), 13)
        self.assertIn('candidates_over_budget', result['diagnostics'])

    def test_unknown_or_ambiguous_version_cannot_borrow_head(self):
        module = importlib.import_module('incident_candidates')
        for status in ('unknown', 'ambiguous'):
            result = module.collect_candidates(self.repo, {'event_id': 'event', 'exception': {
                'frames': [self.frame('src/app.py')]}}, {'resolution': {'status': status, 'event_commit': None}})
            self.assertFalse(result['candidates'])
            self.assertEqual(len(result['frames']), 1)

    def test_no_stack_remains_an_evidence_gap(self):
        result = self.collect([])
        self.assertFalse(result['candidates'])
        self.assertIn('no_application_stack', result['diagnostics'])

    def test_sensitive_source_stays_local_and_provenance_has_no_source(self):
        self.write('src/app.py', 'api_key = "synthetic-secret-value"\nraise RuntimeError("synthetic")\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic secret')
        result = self.collect([self.frame('src/app.py', 2)], commit=self.git('rev-parse', 'HEAD').strip())
        self.assertTrue(result['candidates'][0]['local_only'])
        self.assertNotIn('synthetic-secret-value', json.dumps(result['provenance']))

    def test_netrc_event_blobs_are_retained_only_for_local_investigation(self):
        paths = ['config/.netrc', 'config/_netrc']
        for path in paths:
            self.write(path, 'machine example.invalid login demo password SYNTHETIC_NETRC_VALUE\n')
        self.git('add', '--', *paths)
        self.git('commit', '-qm', 'synthetic credential blobs')
        result = self.collect([self.frame(path) for path in paths],
                              commit=self.git('rev-parse', 'HEAD').strip())
        self.assertEqual([item['path'] for item in result['candidates']], paths)
        self.assertTrue(all(item.get('local_only') for item in result['candidates']))
        self.assertTrue(all(item['origins'] == ['stack'] for item in result['candidates']))
        self.assertNotIn('SYNTHETIC_NETRC_VALUE', json.dumps(result['provenance']))

    def test_sensitive_path_is_an_explicit_gap_before_v1_payload(self):
        self.write('src/person@example.com/app.py', 'raise RuntimeError("synthetic")\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'synthetic personal path')
        result = self.collect([self.frame('src/person@example.com/app.py')],
                              commit=self.git('rev-parse', 'HEAD').strip())
        self.assertFalse(result['candidates'])
        self.assertEqual(result['unresolved_frames'][0]['code'], 'sensitive_frame_metadata')

    def test_unusual_source_separators_cannot_shift_exception_line(self):
        self.write('src/app.py', '# comment' + '\f' * 70 + '\nraise RuntimeError("synthetic")\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'formfeed in Python comment')
        result = self.collect([self.frame('src/app.py', 2)], commit=self.git('rev-parse', 'HEAD').strip())
        self.assertFalse(result['candidates'])
        self.assertEqual(result['unresolved_frames'][0]['code'], 'source_line_separators_unsupported')


if __name__ == '__main__':
    unittest.main()
