"""Synthetic schema fixtures for an offline Sentry project-event API adapter."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'skills' / 'jev-bugfix' / 'scripts'
FIXTURES = ROOT / 'tests' / 'fixtures' / 'incidents' / 'sentry-api'
sys.path.insert(0, str(SCRIPTS))
SENTRY = None
if (SCRIPTS / 'incident_sentry.py').exists():
    SPEC = importlib.util.spec_from_file_location('incident_sentry', SCRIPTS / 'incident_sentry.py')
    SENTRY = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(SENTRY)


class IncidentSentryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SENTRY, 'offline Sentry adapter is not implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'event.json'
        self.value = json.loads((FIXTURES / 'complete.json').read_text())

    def load(self, value=None, **kwargs):
        self.path.write_text(json.dumps(self.value if value is None else value, ensure_ascii=True), encoding='utf-8')
        return SENTRY.load_sentry_api_event(self.path, **kwargs)

    def invalid(self, value, code, **kwargs):
        with self.assertRaises(SENTRY.EvidenceError) as caught:
            self.load(value, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def frames(self):
        return self.value['entries'][0]['data']['values'][0]['stacktrace']['frames']

    def test_complete_api_fixture_maps_independent_projection_and_two_hashes(self):
        result = self.load()
        expected = json.loads((FIXTURES / 'expected-projection.json').read_text())
        projected = {'schema_version': result['schema_version'], 'events': [
            {key: value for key, value in result['events'][0].items() if key != 'provenance'}]}
        self.assertEqual(projected, expected)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['incomplete'], [])
        self.assertEqual(result['input_sha256'], hashlib.sha256(self.path.read_bytes()).hexdigest())
        encoded = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        provenance = result['import_provenance']
        self.assertEqual(provenance['source_sha256'], result['input_sha256'])
        self.assertEqual(provenance['projection_sha256'], hashlib.sha256(encoded).hexdigest())
        self.assertNotEqual(provenance['source_sha256'], provenance['projection_sha256'])
        self.assertEqual(provenance['frames'], [
            {'normalized_index': 0, 'entry_index': 0, 'exception_index': 0, 'frame_index': 0, 'path_source': 'filename'},
            {'normalized_index': 1, 'entry_index': 0, 'exception_index': 0, 'frame_index': 1, 'path_source': 'filename'}])

    def test_event_id_is_api_event_id_and_never_falls_back_to_id(self):
        result = self.load()
        self.assertEqual(result['events'][0]['event_id'], 'schema-fixture-event-1')
        self.value.pop('eventID')
        self.invalid(self.value, 'event_id_invalid')

    def test_sdk_envelope_issue_and_list_shapes_are_rejected(self):
        for value in ({'event_id': 'sdk', 'exception': {'values': []}}, {'eventID': 'sdk', 'exception': {'values': []}}, {'id': 'issue', 'count': 1}, [{'eventID': 'one'}]):
            with self.subTest(value=value):
                self.invalid(value, 'sentry_api_shape_invalid')

    def test_occurrence_time_does_not_fall_back_to_received(self):
        self.value.pop('dateCreated')
        result = self.load()
        self.assertIsNone(result['events'][0]['timestamp'])
        self.assertEqual(result['status'], 'partial')
        self.assertIn('sentry_timestamp_missing', result['incomplete'])

    def test_project_sdk_device_and_last_commit_do_not_become_application_metadata(self):
        self.value['tags'] = [{'key': 'environment', 'value': 'production'}]
        self.value['contexts'].pop('runtime')
        self.value['contexts'].pop('os')
        result = self.load()
        event = result['events'][0]
        self.assertIsNone(event['service'])
        self.assertIsNone(event['commit'])
        self.assertEqual(event['runtime'], {'language': None, 'version': None, 'os': None, 'arch': None})
        self.assertEqual(event['release'], 'deploy-1')
        self.assertIn('sentry_service_missing', result['incomplete'])

    def test_explicit_service_and_matching_duplicate_tags_are_supported(self):
        self.value['tags'].extend(copy.deepcopy(self.value['tags']))
        self.assertEqual(self.load(service='worker')['status'], 'ready')
        self.value['tags'] = [{'key': 'environment', 'value': 'production'}]
        result = self.load(service='api')
        self.assertEqual(result['events'][0]['service'], 'api')
        self.assertEqual(result['import_provenance']['field_sources']['service'], 'argument.service')
        self.assertEqual(result['status'], 'ready')

    def test_conflicting_environment_or_service_tags_and_service_argument_need_input(self):
        for key, kwargs in (('environment', {}), ('service', {}), ('service', {'service': 'api'})):
            with self.subTest(key=key, kwargs=kwargs):
                value = copy.deepcopy(self.value)
                if not kwargs:
                    value['tags'].append({'key': key, 'value': 'different'})
                result = self.load(value, **kwargs)
                self.assertEqual(result['status'], 'needs_input')
                self.assertIsNone(result['events'][0][key])
                self.assertIn('sentry_' + key + '_conflict', result['diagnostics'])

    def test_missing_environment_is_a_conversion_gap(self):
        self.value['tags'] = [{'key': 'service', 'value': 'worker'}]
        result = self.load()
        self.assertEqual(result['status'], 'partial')
        self.assertIn('sentry_environment_missing', result['incomplete'])

    def test_multiple_exceptions_require_selection_and_preserve_unselected_indices(self):
        value = json.loads((FIXTURES / 'multi-exception.json').read_text())
        result = self.load(value)
        self.assertEqual(result['status'], 'needs_input')
        self.assertEqual(result['events'][0]['exception']['frames'], [])
        self.assertIn('sentry_exception_selection_required', result['diagnostics'])
        selected = self.load(value, exception_index=1)
        self.assertEqual(selected['status'], 'partial')
        self.assertEqual(selected['events'][0]['exception']['type'], 'RuntimeError')
        self.assertEqual(selected['events'][0]['exception']['frames'][0]['path'], 'src/selected.py')
        self.assertEqual(selected['import_provenance']['exception_selection'], {'entry_index': 0, 'exception_index': 1, 'exception_count': 2, 'unselected_count': 1, 'unselected_indices': [0]})
        self.assertIn('sentry_exception_chain_unprocessed', selected['incomplete'])
        self.assertEqual(selected['import_provenance']['frames'][0]['exception_index'], 1)

    def test_exception_index_is_bounded_and_requires_an_exception(self):
        for index in (-1, 1, True, '0'):
            with self.subTest(index=index):
                self.invalid(self.value, 'sentry_exception_index_invalid', exception_index=index)
        self.value['entries'] = []
        self.invalid(self.value, 'sentry_exception_index_invalid', exception_index=0)

    def test_unselected_stack_only_obeys_raw_limits_and_selected_stack_obeys_projection_limit(self):
        value = json.loads((FIXTURES / 'multi-exception.json').read_text())
        value['entries'][0]['data']['values'][0]['stacktrace']['frames'] *= 33
        selected = self.load(value, exception_index=1)
        self.assertEqual(selected['status'], 'partial')
        self.assertEqual(len(selected['events'][0]['exception']['frames']), 1)
        self.assertEqual(self.load(value)['status'], 'needs_input')
        self.invalid(value, 'frames_over_limit', exception_index=0)

    def test_duplicate_exception_entry_is_unsupported_instead_of_silently_merged(self):
        self.value['entries'].append(copy.deepcopy(self.value['entries'][0]))
        self.invalid(self.value, 'sentry_unsupported_shape')

    def test_duplicate_breadcrumb_entry_is_unsupported_instead_of_silently_merged(self):
        self.value['entries'].append(copy.deepcopy(self.value['entries'][1]))
        self.invalid(self.value, 'sentry_unsupported_shape')

    def test_frame_array_order_is_retained_and_unknown_in_app_stays_unknown(self):
        self.frames()[0].pop('inApp')
        result = self.load()
        frames = result['events'][0]['exception']['frames']
        self.assertEqual([frame['function'] for frame in frames], ['run', 'parse'])
        self.assertIsNone(frames[0]['in_app'])
        self.assertEqual(result['status'], 'partial')
        self.assertIn('sentry_frame_in_app_missing', result['incomplete'])

    def test_filename_is_preferred_and_abs_path_is_only_a_missing_filename_fallback(self):
        self.frames()[0].pop('filename')
        result = self.load()
        self.assertEqual(result['events'][0]['exception']['frames'][0]['path'], '/srv/schema-fixture/src/worker.py')
        self.assertEqual(result['import_provenance']['frames'][0]['path_source'], 'absPath')
        self.frames()[0]['filename'] = ''
        self.assertEqual(self.load()['events'][0]['exception']['frames'][0]['path'], '')

    def test_uri_paths_are_preserved_as_unresolved_evidence_and_never_downloaded(self):
        for path in ('https://private.invalid/file.py', 'webpack:///src/file.py', 'app:///src/file.py'):
            with self.subTest(path=path):
                self.frames()[0]['filename'] = path
                result = self.load()
                self.assertEqual(result['events'][0]['exception']['frames'][0]['path'], path)
                self.assertEqual(result['status'], 'partial')
                self.assertIn('sentry_frame_path_unsupported', result['incomplete'])

    def test_missing_frame_fields_and_no_exception_leave_partial_evidence(self):
        for key, code in (('filename', 'sentry_frame_path_missing'), ('lineNo', 'sentry_frame_line_missing'), ('function', 'sentry_frame_function_missing')):
            with self.subTest(key=key):
                value = copy.deepcopy(self.value)
                frame = value['entries'][0]['data']['values'][0]['stacktrace']['frames'][0]
                frame.pop(key)
                frame.pop('absPath', None)
                result = self.load(value)
                self.assertEqual(result['status'], 'partial')
                self.assertIn(code, result['incomplete'])
        self.value['entries'] = []
        result = self.load()
        self.assertEqual(result['events'][0]['exception']['frames'], [])
        self.assertIn('sentry_exception_missing', result['incomplete'])

    def test_processed_stacktrace_is_used_and_raw_stacktrace_never_substituted(self):
        exception = self.value['entries'][0]['data']['values'][0]
        exception['rawStacktrace'] = {'frames': [{'filename': 'RAW_SECRET.py', 'lineNo': 99}]}
        self.assertEqual(self.load()['events'][0]['exception']['frames'][0]['path'], 'src/worker.py')
        exception.pop('stacktrace')
        result = self.load()
        self.assertEqual(result['events'][0]['exception']['frames'], [])
        self.assertIn('sentry_frames_missing', result['incomplete'])
        self.assertNotIn('RAW_SECRET', json.dumps(result))

    def test_server_omitted_frames_and_exceptions_are_counted_and_make_partial(self):
        data = self.value['entries'][0]['data']
        data['excOmitted'] = [1, 3]
        data['values'][0]['stacktrace']['framesOmitted'] = [2, 6]
        result = self.load()
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['import_provenance']['omissions']['sentry_omitted_frames'], 4)
        self.assertEqual(result['import_provenance']['omissions']['sentry_omitted_exceptions'], 2)

    def test_trace_identifier_without_spans_remains_known_and_not_invented(self):
        self.value['contexts']['trace'] = {'trace_id': 'trace-only', 'type': 'trace'}
        result = self.load()
        self.assertEqual(result['events'][0]['trace'], {'trace_id': 'trace-only', 'span_id': None, 'parent_span_id': None, 'spans': []})
        self.assertEqual(result['status'], 'ready')

    def test_ignored_large_sensitive_fields_and_unknown_key_names_never_escape_projection(self):
        marker = 'UNTRUSTED_SECRET_MARKER'
        self.value[marker] = {'unknown': marker * 500}
        self.value['context'] = {marker: marker * 500}
        self.value['user'] = {'email': marker}
        self.value['entries'][2]['data'].update({'headers': [[marker, marker * 500]], 'cookies': [marker], 'data': marker * 500})
        self.frames()[0].update({'vars': {marker: marker * 500}, 'context': [[3, marker * 500]], 'sourceLink': 'https://' + marker})
        result = self.load()
        rendered = json.dumps(result)
        self.assertNotIn(marker, rendered)
        self.assertEqual(result['status'], 'ready')
        omissions = result['import_provenance']['omissions']
        for category in ('request', 'user', 'headers', 'cookies', 'frame_vars', 'frame_context', 'frame_source_link', 'extra', 'unknown_fields'):
            self.assertGreater(omissions[category], 0, category)

    def test_breadcrumb_data_is_ignored_but_missing_message_records_gap(self):
        crumb = self.value['entries'][1]['data']['values'][0]
        crumb.pop('message')
        crumb['data'] = {'url': 'BREADCRUMB_SECRET', 'status_code': 503}
        result = self.load()
        self.assertNotIn('BREADCRUMB_SECRET', json.dumps(result))
        self.assertIsNone(result['events'][0]['breadcrumbs'][0]['message'])
        self.assertEqual(result['status'], 'partial')
        self.assertIn('sentry_breadcrumb_details_unimported', result['incomplete'])

    def test_exact_release_and_path_remain_in_memory_until_existing_sanitizer(self):
        self.value['release']['version'] = 'password=SECRET_RELEASE'
        self.frames()[0]['filename'] = '/srv/token=SECRET_PATH/worker.py'
        result = self.load()
        self.assertEqual(result['events'][0]['release'], 'password=SECRET_RELEASE')
        self.assertEqual(result['events'][0]['exception']['frames'][0]['path'], '/srv/token=SECRET_PATH/worker.py')
        from incident_evidence import sanitize_value
        rendered = json.dumps(sanitize_value(result))
        self.assertNotIn('SECRET_RELEASE', rendered)
        self.assertNotIn('SECRET_PATH', rendered)

    def test_entries_tags_exceptions_frames_and_breadcrumb_limits_reject_without_truncation(self):
        cases = []
        for field, maximum, code in (('entries', 200, 'sentry_entries_over_limit'), ('tags', 200, 'sentry_tags_over_limit')):
            value = copy.deepcopy(self.value)
            value[field] = [{}] * (maximum + 1)
            cases.append((value, code))
        value = copy.deepcopy(self.value)
        value['entries'][0]['data']['values'] *= 33
        cases.append((value, 'sentry_exceptions_over_limit'))
        value = copy.deepcopy(self.value)
        value['entries'][0]['data']['values'][0]['stacktrace']['frames'] = [{}] * 65
        cases.append((value, 'frames_over_limit'))
        value = copy.deepcopy(self.value)
        value['entries'][1]['data']['values'] = [{}] * 101
        cases.append((value, 'breadcrumbs_over_limit'))
        for value, code in cases:
            with self.subTest(code=code):
                self.invalid(value, code)

    def test_retained_text_limits_use_utf8_bytes_but_ignored_values_do_not(self):
        self.value['release']['version'] = '界' * 1365 + 'a'
        self.load()
        self.value['release']['version'] = '界' * 1366
        self.invalid(self.value, 'text_over_limit')

    def test_wrong_types_in_retained_sources_are_rejected(self):
        for path, value in (('release', 'deploy'), ('entries', {}), ('tags', {}), ('contexts', []), ('lineNo', True), ('inApp', 1), ('service', 3)):
            with self.subTest(path=path):
                raw = copy.deepcopy(self.value)
                if path in ('lineNo', 'inApp'):
                    raw['entries'][0]['data']['values'][0]['stacktrace']['frames'][0][path] = value
                elif path == 'service':
                    raw['tags'][1]['value'] = value
                else:
                    raw[path] = value
                self.invalid(raw, 'input_type_invalid')

    def test_occurrence_and_breadcrumb_timestamps_keep_strict_timestamp_validation(self):
        for location in ('event', 'breadcrumb'):
            with self.subTest(location=location):
                value = copy.deepcopy(self.value)
                if location == 'event':
                    value['dateCreated'] = '2026-10-05T08:00:00'
                else:
                    value['entries'][1]['data']['values'][0]['timestamp'] = 'yesterday'
                self.invalid(value, 'timestamp_invalid')

    def test_server_omission_counts_are_bounded_and_cannot_be_negative(self):
        for omitted in ([-1, 2], [3, 1], [0, 2147483648], [False, 1], [1], 'unknown'):
            with self.subTest(omitted=omitted):
                value = copy.deepcopy(self.value)
                value['entries'][0]['data']['excOmitted'] = omitted
                self.invalid(value, 'input_type_invalid')

    def test_ignored_unicode_pairs_remain_valid_even_above_text_limit(self):
        self.value['context'] = {'synthetic': '\U0001f600' * 1500}
        self.assertEqual(self.load()['status'], 'ready')

    def test_missing_release_version_is_unknown_even_when_last_commit_exists(self):
        self.value['release'].pop('version')
        result = self.load()
        self.assertIsNone(result['events'][0]['release'])
        self.assertIsNone(result['events'][0]['commit'])
        self.assertIn('sentry_release_missing', result['incomplete'])

    def test_raw_json_limits_duplicates_nonfinite_and_unicode_apply_to_ignored_content(self):
        raw_cases = [
            (b' ' * (2 * 1024 * 1024 + 1), 'input_file_over_limit'),
            (b'[' * 33 + b'0' + b']' * 33, 'input_depth_over_limit'),
            (b'{"eventID":"one","eventID":"two","entries":[]}', 'input_duplicate_key'),
            (b'{"eventID":"one","entries":[],"extra":NaN}', 'input_non_finite'),
            (b'{"eventID":"one","entries":[],"extra":1e9999}', 'input_non_finite'),
            (b'\xff', 'input_encoding_invalid'),
            (b'{"eventID":"one","entries":[],"unknown":"\\ud800"}', 'input_encoding_invalid'),
            (b'{"eventID":"one","entries":[],"\\udfff":"ignored"}', 'input_encoding_invalid'),
        ]
        for raw, code in raw_cases:
            with self.subTest(code=code):
                self.path.write_bytes(raw)
                with self.assertRaises(SENTRY.EvidenceError) as caught:
                    SENTRY.load_sentry_api_event(self.path)
                self.assertEqual(caught.exception.code, code)

    def test_missing_input_only_emits_fixed_diagnostic(self):
        with self.assertRaises(SENTRY.EvidenceError) as caught:
            SENTRY.load_sentry_api_event(Path(self.temp.name) / 'password=SECRET_PATH.json')
        self.assertEqual(str(caught.exception), 'input_io_error')


if __name__ == '__main__':
    unittest.main()
