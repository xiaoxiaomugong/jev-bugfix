"""Offline binding and provenance tests using real frozen Git objects."""
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from incident_test_support import GitRepository

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills/jev-bugfix/scripts'
sys.path.insert(0, str(SCRIPTS))


class BundleFixture:
    def setup_fixture(self):
        self.assertIsNotNone(importlib.util.find_spec('incident_bundle'),
                             'incident_bundle must provide offline binding')
        self.module = importlib.import_module('incident_bundle')
        self.repository = GitRepository()
        self.addCleanup(self.repository.close)
        self.repo = self.repository.path
        self.source = ''.join('line %03d\n' % n for n in range(1, 101))
        self.repository.write('src/app.py', self.source)
        self.commit = self.repository.commit()
        self.tmp = tempfile.TemporaryDirectory(prefix='bundle-test-')
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.input = self.work / 'input.json'
        self.input.write_text(json.dumps({'schema_version': 1, 'events': [{
            'event_id': 'observed-event', 'commit': self.commit,
            'exception': {'type': 'Error', 'message': 'observed failure',
                          'frames': [{'path': 'src/app.py', 'line': 50, 'in_app': True}]}}]}))
        args = SimpleNamespace(input=str(self.input), repo=str(self.repo), event_id=None,
                               release_map=None, baseline_revision=None, source_root=None)
        self.evidence, self.case = importlib.import_module('prepare_incident').build_evidence(args)
        self.assertIsNotNone(self.case)
        self.bundle = self.module.make_bundle(self.case, self.evidence)
        self.output = self.work / 'artifacts'
        self.output.mkdir()
        self.write_bundle()

    def write_bundle(self):
        for name, value in (('case', self.case), ('evidence', self.evidence), ('bundle', self.bundle)):
            (self.output / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

    def verify(self):
        return self.module.verify_bundle(self.output / 'bundle.json', self.repo)

    def rebind(self):
        self.bundle = self.module.make_bundle(self.case, self.evidence)
        self.write_bundle()


class IncidentBundleTests(BundleFixture, unittest.TestCase):
    def setUp(self):
        self.setup_fixture()

    def test_verified_report_contains_count_and_version_but_no_source(self):
        report = self.verify()
        self.assertEqual(report['status'], 'verified')
        self.assertEqual(report['checked_candidates'], 1)
        self.assertEqual(report['version']['event_commit'], self.commit)
        self.assertEqual(report['version']['evidence_status'], self.evidence['status'])
        self.assertNotIn('line 050', json.dumps(report))

    def test_manifest_hash_is_canonical_utf8_and_excludes_review_controls(self):
        core = copy.deepcopy(self.case)
        core.pop('reviewed_for_secrets')
        for candidate in core['candidates']:
            candidate.pop('local_only', None)
        expected = hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True,
                                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.assertEqual(self.bundle['case_sha256'], expected)
        self.assertEqual(self.bundle['format'], 'incident-bundle/v1')
        self.assertEqual(self.bundle['case_file'], 'case.json')
        self.assertEqual(self.bundle['evidence_file'], 'evidence.json')

    def test_another_incident_sidecar_with_same_candidate_is_rejected(self):
        self.evidence['events'][0]['exception']['message'] = 'another accident'
        self.evidence['input']['sha256'] = 'a' * 64
        self.write_bundle()
        self.assertEqual(self.verify()['status'], 'invalid')

    def test_case_content_edits_never_rebind_automatically(self):
        for field in ('description', 'snippet', 'start_line', 'path'):
            original = copy.deepcopy(self.case)
            if field == 'description':
                self.case['bug'][field] = 'user changed summary'
            else:
                candidate = self.case['candidates'][0]
                candidate[field] = {'snippet': 'different\n' * 60, 'start_line': 22,
                                    'path': 'src/changed.py'}[field]
            self.write_bundle()
            self.assertEqual(self.verify()['status'], 'invalid', field)
            self.case = original

    def test_review_and_local_only_tightening_are_allowed(self):
        self.case['reviewed_for_secrets'] = True
        self.case['candidates'][0]['local_only'] = True
        self.write_bundle()
        self.assertEqual(self.verify()['status'], 'verified')

    def test_required_local_only_cannot_be_relaxed_or_removed_from_case(self):
        self.case['candidates'][0]['local_only'] = True
        self.rebind()
        self.assertEqual(self.bundle['required_local_only'], [self.case['candidates'][0]['id']])
        for value in (False, None):
            if value is None:
                self.case['candidates'][0].pop('local_only', None)
            else:
                self.case['candidates'][0]['local_only'] = value
            self.write_bundle()
            self.assertEqual(self.verify()['status'], 'invalid')

    def test_identity_manifest_values_must_match_evidence(self):
        for key in ('input_sha256', 'selected_event_ref', 'event_commit'):
            original = copy.deepcopy(self.bundle)
            self.bundle[key] = 'b' * 64 if key != 'selected_event_ref' else 'event_999'
            self.write_bundle()
            self.assertEqual(self.verify()['status'], 'invalid', key)
            self.bundle = original

    def test_duplicate_or_mismatching_provenance_ids_are_rejected(self):
        for change in ('duplicate', 'replace'):
            evidence = copy.deepcopy(self.evidence)
            if change == 'duplicate':
                self.evidence['candidate_provenance'].append(copy.deepcopy(self.evidence['candidate_provenance'][0]))
            else:
                self.evidence['candidate_provenance'][0]['id'] = 'different'
            with self.assertRaises(ValueError):
                self.module.make_bundle(self.case, self.evidence)
            self.evidence = evidence

    def test_forged_frame_reference_cannot_be_bound(self):
        for change in ('event', 'index', 'indices', 'line', 'candidate'):
            evidence = copy.deepcopy(self.evidence)
            reference = self.evidence['candidate_provenance'][0]['evidence_refs'][0]
            if change == 'event':
                reference['event_ref'] = 'event_999'
            elif change == 'index':
                reference['frame_index'] = 10
            elif change == 'indices':
                reference['event_indices'] = [10]
            elif change == 'line':
                self.evidence['events'][0]['exception']['frames'][0]['line'] = 1
            else:
                self.evidence['frames'][0]['candidate_id'] = 'different'
            with self.assertRaises(ValueError, msg=change):
                self.module.make_bundle(self.case, self.evidence)
            self.evidence = evidence

    def test_frame_path_cannot_be_remapped_to_an_unrelated_real_file(self):
        self.evidence['events'][0]['exception']['frames'][0]['path'] = 'src/unrelated.py'
        self.evidence['frames'][0]['path'] = 'src/unrelated.py'
        with self.assertRaises(ValueError):
            self.module.make_bundle(self.case, self.evidence)

    def test_event_reference_must_match_original_input_indices(self):
        self.evidence['events'][0]['provenance']['indices'] = [10]
        self.evidence['candidate_provenance'][0]['evidence_refs'][0]['event_indices'] = [10]
        with self.assertRaises(ValueError):
            self.module.make_bundle(self.case, self.evidence)

    def test_multiple_frame_references_are_preserved_for_one_candidate(self):
        frame = copy.deepcopy(self.evidence['events'][0]['exception']['frames'][0])
        self.evidence['events'][0]['exception']['frames'].append(frame)
        record = copy.deepcopy(self.evidence['frames'][0])
        record['frame_index'] = 1
        self.evidence['frames'].append(record)
        reference = copy.deepcopy(self.evidence['candidate_provenance'][0]['evidence_refs'][0])
        reference['frame_index'] = 1
        self.evidence['candidate_provenance'][0]['evidence_refs'].append(reference)
        self.rebind()
        self.assertEqual(self.verify()['status'], 'verified')
        self.assertEqual(len(self.evidence['candidate_provenance'][0]['evidence_refs']), 2)

    def test_absolute_source_root_mapping_remains_verifiable(self):
        for raw_path in ('/srv/application/src/app.py', 'C:\\application\\src\\app.py'):
            self.evidence['events'][0]['exception']['frames'][0]['path'] = raw_path
            self.evidence['frames'][0]['path'] = raw_path
            self.rebind()
            self.assertEqual(self.verify()['status'], 'verified', raw_path)

    def test_total_git_budget_is_shared_between_candidates(self):
        self.repository.write('src/other.py', 'other source\n')
        commit = self.repository.commit()
        raw = json.loads(self.input.read_text())
        raw['events'][0]['commit'] = commit
        raw['events'][0]['exception']['frames'].append({'path': 'src/other.py', 'line': 1, 'in_app': True})
        self.input.write_text(json.dumps(raw))
        args = SimpleNamespace(input=str(self.input), repo=str(self.repo), event_id=None,
                               release_map=None, baseline_revision=None, source_root=None)
        self.evidence, self.case = importlib.import_module('prepare_incident').build_evidence(args)
        self.rebind()
        clock = [1000.0]
        original = self.module.read_source_blob
        def read(repo, revision, path, git=None):
            if path == 'src/other.py':
                clock[0] = 1021.0
            return original(repo, revision, path, git=git)
        with patch('incident_versions.time.monotonic', side_effect=lambda: clock[0]), patch.object(self.module, 'read_source_blob', side_effect=read):
            report = self.verify()
        self.assertEqual(report['status'], 'unavailable')
        self.assertEqual(report['checked_candidates'], 1)
        self.assertIn('git_budget_exceeded', report['diagnostics'])

    def test_self_consistent_fake_snippet_still_fails_real_git_read(self):
        candidate = self.case['candidates'][0]
        candidate['snippet'] = 'FAKE\n' * 60
        self.evidence['candidate_provenance'][0]['snippet_sha256'] = hashlib.sha256(candidate['snippet'].encode()).hexdigest()
        self.rebind()
        self.assertEqual(self.verify()['status'], 'invalid')

    def test_self_consistent_wrong_blob_oid_still_fails_real_git_read(self):
        self.evidence['candidate_provenance'][0]['blob_oid'] = '1' * 40
        self.rebind()
        self.assertEqual(self.verify()['status'], 'invalid')

    def test_historical_source_survives_head_rename_and_dirty_checkout(self):
        self.repository.git('mv', 'src/app.py', 'src/renamed.py')
        self.repository.commit()
        self.repository.write('src/renamed.py', 'DIRTY\n')
        before = self.repository.git('status', '--porcelain')
        self.assertEqual(self.verify()['status'], 'verified')
        self.assertEqual(self.repository.git('status', '--porcelain'), before)

    def test_missing_event_commit_is_unavailable(self):
        missing = '1' * 40
        self.evidence['version']['resolution']['event_commit'] = missing
        self.evidence['candidate_provenance'][0]['event_commit'] = missing
        self.rebind()
        report = self.verify()
        self.assertEqual(report['status'], 'unavailable')
        self.assertIn('commit_missing', report['diagnostics'])

    def test_legacy_directory_has_explicit_unbound_status(self):
        (self.output / 'bundle.json').unlink()
        self.assertEqual(self.verify()['status'], 'legacy_unbound')

    def test_unknown_fields_in_all_artifacts_are_rejected(self):
        for artifact, value in (('bundle', self.bundle), ('case', self.case), ('evidence', self.evidence)):
            value['unexpected_private_marker'] = 'DO_NOT_PRINT'
            self.write_bundle()
            report = self.verify()
            self.assertEqual(report['status'], 'invalid', artifact)
            self.assertNotIn('DO_NOT_PRINT', json.dumps(report))
            value.pop('unexpected_private_marker')

    def test_nested_unknown_fields_are_rejected_even_when_hash_is_updated(self):
        self.evidence['events'][0]['exception']['frames'][0]['untrusted'] = 'marker'
        with self.assertRaises(ValueError):
            self.module.make_bundle(self.case, self.evidence)

    def test_fixed_filenames_cannot_reference_arbitrary_paths(self):
        self.bundle['case_file'] = '../case.json'
        self.write_bundle()
        self.assertEqual(self.verify()['status'], 'invalid')

    def test_duplicate_nonfinite_and_invalid_unicode_json_are_rejected(self):
        path = self.output / 'bundle.json'
        for raw in (b'{"format":"incident-bundle/v1","format":"incident-bundle/v1"}',
                    b'{"number":NaN}', b'{"number":1e999}', b'{"value":"\\ud800"}', b'\xff'):
            path.write_bytes(raw)
            self.assertEqual(self.verify()['status'], 'invalid')

    def test_each_read_limit_and_container_depth_are_enforced(self):
        for name, limit in (('bundle', 65536), ('case', 65536), ('evidence', 16 * 1024 * 1024)):
            self.write_bundle()
            (self.output / (name + '.json')).write_bytes(b' ' * (limit + 1))
            self.assertEqual(self.verify()['status'], 'invalid', name)
        self.write_bundle()
        (self.output / 'bundle.json').write_text('[' * 33 + '0' + ']' * 33)
        self.assertEqual(self.verify()['status'], 'invalid')

    def test_producer_refuses_oversized_evidence(self):
        self.evidence['timeline_scope'] = 'x' * (16 * 1024 * 1024)
        with self.assertRaises(ValueError):
            self.module.make_bundle(self.case, self.evidence)


if __name__ == '__main__':
    unittest.main()
