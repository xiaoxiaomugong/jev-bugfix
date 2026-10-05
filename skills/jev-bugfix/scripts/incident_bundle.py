"""Bound, offline incident handoffs and read-only historical source contexts.

Hashes check consistency, not authenticity: a self-consistent replacement of an
entire bundle is not detectable without an independently trusted digest.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

from incident_evidence import sanitize_text
from incident_candidates import map_frame_path
from incident_versions import GitSession, read_source_blob
from rank_candidates import InvalidInput, sensitive, sensitive_path, validate_case


MAX_BUNDLE_BYTES = 65536
MAX_CASE_BYTES = 65536
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
MAX_DEPTH = 32
MAX_CONTEXT_BYTES = 16384
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
_ID = re.compile(r'[A-Za-z0-9_-]{1,64}\Z')
_EVENT_REF = re.compile(r'event_\d{3}\Z')
_CAPABILITY = object()


class BundleError(ValueError):
    """A fixed, non-sensitive diagnostic suitable for public reports."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_bytes(value):
    """Canonical JSON used by the manifest; array order is significant."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8', errors='strict')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BundleError('json_invalid')


def _hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _case_content(case):
    core = copy.deepcopy(case)
    core.pop('reviewed_for_secrets', None)
    for candidate in core.get('candidates', []):
        candidate.pop('local_only', None)
    return core


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise BundleError('artifact_schema_invalid')


def _text(value, nullable=False, maximum=4096):
    if nullable and value is None:
        return
    if not isinstance(value, str) or len(value.encode('utf-8', errors='strict')) > maximum or '\x00' in value:
        raise BundleError('artifact_schema_invalid')


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise BundleError('artifact_schema_invalid')


def _strings(value, maximum=20000):
    if not isinstance(value, list) or len(value) > maximum:
        raise BundleError('artifact_schema_invalid')
    for item in value:
        _text(item)


def _indices(value, maximum=200):
    if not isinstance(value, list) or not value or len(value) > maximum or len(set(value)) != len(value):
        raise BundleError('artifact_schema_invalid')
    for item in value:
        _integer(item)


def _objects(value, maximum):
    if not isinstance(value, list) or len(value) > maximum or not all(isinstance(item, dict) for item in value):
        raise BundleError('artifact_schema_invalid')
    return value


def _walk(value, depth=0):
    """Validate in-memory values as strictly as decoded JSON, including keys."""
    if isinstance(value, (dict, list)):
        depth += 1
        if depth > MAX_DEPTH:
            raise BundleError('json_depth_limit')
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise BundleError('json_invalid')
                key.encode('utf-8', errors='strict')
                _walk(item, depth)
        else:
            for item in value:
                _walk(item, depth)
    elif isinstance(value, str):
        value.encode('utf-8', errors='strict')
    elif isinstance(value, float) and not math.isfinite(value):
        raise BundleError('json_nonfinite')
    elif value is not None and type(value) not in (int, float, bool):
        raise BundleError('json_invalid')


def _serialized_limit(value, limit, kind):
    try:
        _walk(value)
        encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, BundleError):
            raise
        raise BundleError('json_invalid')
    if len(encoded) > limit:
        raise BundleError(kind + '_size_limit')


def _event(value):
    _fields(value, ('event_id', 'event_ref', 'timestamp', 'service', 'environment', 'release', 'commit',
                    'exception', 'breadcrumbs', 'trace', 'runtime', 'provenance'))
    for key in ('event_id', 'event_ref', 'timestamp', 'service', 'environment', 'release', 'commit'):
        _text(value[key], nullable=key not in ('event_id', 'event_ref'))
    if not _EVENT_REF.fullmatch(value['event_ref']):
        raise BundleError('event_reference_invalid')
    exception = value['exception']
    _fields(exception, ('type', 'message', 'frames'))
    _text(exception['type'], nullable=True)
    _text(exception['message'], nullable=True)
    for frame in _objects(exception['frames'], 64):
        _fields(frame, ('path', 'line', 'function', 'in_app'))
        for key in ('path', 'function'):
            _text(frame[key], nullable=True)
        if frame['line'] is not None:
            _integer(frame['line'], 1)
        if frame['in_app'] is not None and type(frame['in_app']) is not bool:
            raise BundleError('artifact_schema_invalid')
    for breadcrumb in _objects(value['breadcrumbs'], 100):
        _fields(breadcrumb, ('timestamp', 'category', 'level', 'message'))
        for item in breadcrumb.values():
            _text(item, nullable=True)
    _fields(value['trace'], ('trace_id', 'span_id', 'parent_span_id', 'spans'))
    for key in ('trace_id', 'span_id', 'parent_span_id'):
        _text(value['trace'][key], nullable=True)
    for span in _objects(value['trace']['spans'], 100):
        _fields(span, ('span_id', 'parent_span_id', 'op', 'status', 'start_timestamp', 'end_timestamp'))
        for item in span.values():
            _text(item, nullable=True)
    _fields(value['runtime'], ('language', 'version', 'os', 'arch'))
    for item in value['runtime'].values():
        _text(item, nullable=True)
    _fields(value['provenance'], ('indices', 'duplicate_count', 'missing_fields'))
    _indices(value['provenance']['indices'])
    if value['event_ref'] != 'event_%03d' % value['provenance']['indices'][0] or any(index >= 200 for index in value['provenance']['indices']):
        raise BundleError('event_reference_invalid')
    _integer(value['provenance']['duplicate_count'])
    _strings(value['provenance']['missing_fields'])


def _resolution(value):
    _fields(value, ('status', 'event_commit', 'clues', 'diagnostics'))
    _text(value['status'])
    _text(value['event_commit'], nullable=True)
    _strings(value['diagnostics'])
    for clue in _objects(value['clues'], 200):
        _fields(clue, ('source', 'status', 'commit', 'diagnostic'))
        for key, item in clue.items():
            _text(item, nullable=key in ('commit', 'diagnostic'))


def _frame_record(value):
    _fields(value, ('event_id', 'event_ref', 'frame_index', 'path', 'mapped_path', 'status', 'code'), ('candidate_id',))
    for key in ('event_id', 'event_ref', 'path', 'mapped_path', 'status', 'code', 'candidate_id'):
        if key in value:
            _text(value[key], nullable=key in ('path', 'mapped_path', 'code'))
    _integer(value['frame_index'])


def _import_provenance(value):
    _fields(value, ('schema_version', 'adapter', 'adapter_version', 'mapping_version', 'source_sha256',
                    'projection_sha256', 'exception_selection', 'field_sources', 'frames', 'omissions',
                    'diagnostics', 'completeness'))
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['adapter'] != 'sentry-api-event/v1':
        raise BundleError('artifact_schema_invalid')
    for key in ('adapter_version', 'mapping_version'):
        _text(value[key])
    for key in ('source_sha256', 'projection_sha256'):
        if not isinstance(value[key], str) or not _SHA.fullmatch(value[key]):
            raise BundleError('artifact_schema_invalid')
    selection = value['exception_selection']
    _fields(selection, ('entry_index', 'exception_index', 'exception_count', 'unselected_count', 'unselected_indices'))
    for key in ('entry_index', 'exception_index', 'exception_count', 'unselected_count'):
        if selection[key] is not None:
            _integer(selection[key])
    if not isinstance(selection['unselected_indices'], list) or len(selection['unselected_indices']) > 32:
        raise BundleError('artifact_schema_invalid')
    for index in selection['unselected_indices']:
        _integer(index)
    _fields(value['field_sources'], ('event_id', 'timestamp', 'service', 'environment', 'release',
                                    'exception', 'breadcrumbs', 'trace', 'runtime', 'os'))
    for item in value['field_sources'].values():
        _text(item, nullable=True)
    for frame in _objects(value['frames'], 64):
        _fields(frame, ('normalized_index', 'entry_index', 'exception_index', 'frame_index', 'path_source'))
        for key in ('normalized_index', 'entry_index', 'exception_index', 'frame_index'):
            _integer(frame[key])
        if frame['path_source'] not in ('filename', 'absPath', None):
            raise BundleError('artifact_schema_invalid')
    _fields(value['omissions'], ('request', 'user', 'headers', 'cookies', 'frame_vars', 'frame_context',
                                'frame_source_link', 'raw_stacktrace', 'extra', 'breadcrumb_data', 'unknown_fields',
                                'unknown_entries', 'unselected_exceptions', 'sentry_omitted_frames', 'sentry_omitted_exceptions'))
    for item in value['omissions'].values():
        _integer(item)
    _strings(value['diagnostics'])
    _fields(value['completeness'], ('status', 'incomplete'))
    if value['completeness']['status'] not in ('ready', 'partial', 'needs_input'):
        raise BundleError('artifact_schema_invalid')
    _strings(value['completeness']['incomplete'])


def _evidence(value):
    _fields(value, ('schema_version', 'status', 'input', 'events', 'selected_event', 'selected_event_ref',
                    'selection', 'version', 'related_events', 'timeline', 'timeline_scope', 'candidate_provenance',
                    'frames', 'unresolved_frames', 'summary_omissions', 'budgets', 'diagnostics', 'conclusions'),
            ('import_provenance', 'bug_context'))
    if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['status'] not in ('ready', 'partial'):
        raise BundleError('artifact_schema_invalid')
    _fields(value['input'], ('sha256', 'adapter'))
    if not isinstance(value['input']['sha256'], str) or not _SHA.fullmatch(value['input']['sha256']):
        raise BundleError('artifact_schema_invalid')
    _text(value['input']['adapter'])
    for event in _objects(value['events'], 200):
        _event(event)
    for key in ('selected_event', 'selected_event_ref', 'timeline_scope'):
        _text(value[key])
    _fields(value['selection'], ('groups', 'diagnostics', 'status'))
    _text(value['selection']['status'])
    _strings(value['selection']['diagnostics'])
    for group in _objects(value['selection']['groups'], 200):
        _fields(group, ('group_id', 'key', 'event_ids', 'event_refs', 'event_count', 'missing_fields'))
        _text(group['group_id'])
        _fields(group['key'], ('service', 'environment', 'release', 'exception_type', 'frame_path', 'frame_function'))
        for item in group['key'].values():
            _text(item, nullable=True)
        for key in ('event_ids', 'event_refs', 'missing_fields'):
            _strings(group[key])
        _integer(group['event_count'])
    version = value['version']
    _fields(version, ('resolution', 'checkout', 'baseline', 'diagnostics'))
    _resolution(version['resolution'])
    _fields(version['checkout'], ('head', 'staged', 'unstaged', 'untracked', 'comparison', 'relation', 'shallow'))
    for key in ('head', 'comparison', 'relation'):
        _text(version['checkout'][key], nullable=key == 'head')
    for key in ('staged', 'unstaged', 'untracked', 'shallow'):
        if version['checkout'][key] is not None and type(version['checkout'][key]) is not bool:
            raise BundleError('artifact_schema_invalid')
    _strings(version['diagnostics'])
    if version['baseline'] is not None:
        baseline = version['baseline']
        _fields(baseline, ('status', 'commit', 'changes', 'diagnostics'))
        _text(baseline['status'])
        _text(baseline['commit'], nullable=True)
        _strings(baseline['diagnostics'])
        for change in _objects(baseline['changes'], 20000):
            _fields(change, ('status', 'path'))
            _text(change['status'])
            _text(change['path'])
    for related in _objects(value['related_events'], 200):
        _fields(related, ('event_id', 'event_ref', 'timestamp', 'resolution', 'relationship', 'included_in_version_chain'))
        for key in ('event_id', 'event_ref', 'timestamp', 'relationship'):
            _text(related[key], nullable=key == 'timestamp')
        if type(related['included_in_version_chain']) is not bool:
            raise BundleError('artifact_schema_invalid')
        _resolution(related['resolution'])
    for item in _objects(value['timeline'], 400):
        _fields(item, ('kind', 'event_ref', 'timestamp'), ('event_id', 'evidence_index', 'category', 'level',
                  'message', 'span_id', 'parent_span_id', 'op', 'status', 'start_timestamp', 'end_timestamp'))
        for key, text in item.items():
            if key == 'evidence_index':
                _integer(text)
            else:
                _text(text, nullable=True)
    for provenance in _objects(value['candidate_provenance'], 12):
        _fields(provenance, ('id', 'event_commit', 'blob_oid', 'snippet_sha256', 'path', 'start_line', 'end_line', 'evidence_refs'))
        for key in ('id', 'event_commit', 'blob_oid', 'snippet_sha256', 'path'):
            _text(provenance[key])
        for key in ('start_line', 'end_line'):
            _integer(provenance[key], 1)
        if not _OID.fullmatch(provenance['event_commit']) or not _OID.fullmatch(provenance['blob_oid']) or not _SHA.fullmatch(provenance['snippet_sha256']):
            raise BundleError('artifact_schema_invalid')
        for reference in _objects(provenance['evidence_refs'], 64):
            _fields(reference, ('event_id', 'event_ref', 'event_indices', 'frame_index'))
            _text(reference['event_id'])
            _text(reference['event_ref'])
            _indices(reference['event_indices'])
            _integer(reference['frame_index'])
    for key in ('frames', 'unresolved_frames'):
        for frame in _objects(value[key], 64):
            _frame_record(frame)
    for omission in _objects(value['summary_omissions'], 200):
        _fields(omission, ('field', 'reason'), ('original_bytes', 'limit_bytes', 'original_items', 'limit_items'))
        _text(omission['field'])
        _text(omission['reason'])
        for key in set(omission) - {'field', 'reason'}:
            _integer(omission[key])
    _fields(value['budgets'], ('candidate_count', 'max_candidates', 'case_bytes', 'max_case_bytes', 'payload_bytes', 'max_payload_bytes'))
    for count in value['budgets'].values():
        _integer(count)
    _strings(value['diagnostics'])
    _fields(value['conclusions'], ('online_observation', 'local_reproduction', 'root_cause', 'fix_verification', 'regression_attribution'))
    for conclusion in value['conclusions'].values():
        _text(conclusion)
    if 'import_provenance' in value:
        _import_provenance(value['import_provenance'])
        if value['import_provenance']['source_sha256'] != value['input']['sha256']:
            raise BundleError('input_identity_mismatch')
    if 'bug_context' in value:
        _fields(value['bug_context'], ('file_sha256', 'fields', 'user_supplied_unverified'))
        if not isinstance(value['bug_context']['file_sha256'], str) or not _SHA.fullmatch(value['bug_context']['file_sha256']):
            raise BundleError('artifact_schema_invalid')
        _strings(value['bug_context']['fields'], 2)
        if not value['bug_context']['fields'] or any(key not in ('description', 'reproduction') for key in value['bug_context']['fields']) or value['bug_context']['user_supplied_unverified'] is not True:
            raise BundleError('artifact_schema_invalid')


def _validate_pair(case, evidence):
    try:
        _walk(case)
        _walk(evidence)
        validate_case(case)
        if len(case['candidates']) > 12 or any(len(c['snippet'].encode('utf-8')) > 2048 or c['end_line'] - c['start_line'] + 1 > 60 for c in case['candidates']):
            raise BundleError('case_schema_invalid')
        _evidence(evidence)
    except (InvalidInput, UnicodeError, TypeError, KeyError, RecursionError):
        raise BundleError('artifact_schema_invalid')
    commit = evidence['version']['resolution']['event_commit']
    if evidence['version']['resolution']['status'] != 'resolved' or not isinstance(commit, str) or not _OID.fullmatch(commit):
        raise BundleError('event_version_invalid')
    events = {event['event_ref']: event for event in evidence['events']}
    if len(events) != len(evidence['events']) or evidence['selected_event_ref'] not in events:
        raise BundleError('event_reference_invalid')
    selected = events[evidence['selected_event_ref']]
    if selected['event_id'] != evidence['selected_event']:
        raise BundleError('event_identity_mismatch')
    candidates = {candidate['id']: candidate for candidate in case['candidates']}
    provenance = {item['id']: item for item in evidence['candidate_provenance']}
    if len(provenance) != len(evidence['candidate_provenance']) or set(candidates) != set(provenance):
        raise BundleError('candidate_identity_mismatch')
    frame_records = {}
    for frame in evidence['frames']:
        key = (frame['event_ref'], frame['frame_index'])
        if key in frame_records or frame['event_ref'] != selected['event_ref'] or frame['event_id'] != selected['event_id'] or frame['frame_index'] >= len(selected['exception']['frames']):
            raise BundleError('frame_reference_invalid')
        if frame['path'] != selected['exception']['frames'][frame['frame_index']]['path']:
            raise BundleError('frame_reference_invalid')
        frame_records[key] = frame
    if len(frame_records) != len(selected['exception']['frames']):
        raise BundleError('frame_reference_invalid')
    seen_refs = set()
    for identifier, candidate in candidates.items():
        item = provenance[identifier]
        if item['event_commit'] != commit or any(item[key] != candidate[key] for key in ('path', 'start_line', 'end_line')):
            raise BundleError('candidate_binding_mismatch')
        if hashlib.sha256(candidate['snippet'].encode('utf-8')).hexdigest() != item['snippet_sha256']:
            raise BundleError('snippet_hash_mismatch')
        if not item['evidence_refs']:
            raise BundleError('frame_reference_invalid')
        for reference in item['evidence_refs']:
            key = (reference['event_ref'], reference['frame_index'])
            index = reference['frame_index']
            if (key in seen_refs or reference['event_ref'] != selected['event_ref'] or reference['event_id'] != selected['event_id']
                    or reference['event_indices'] != selected['provenance']['indices'] or index >= len(selected['exception']['frames'])):
                raise BundleError('frame_reference_invalid')
            seen_refs.add(key)
            frame = selected['exception']['frames'][index]
            record = frame_records.get(key)
            frame_path = frame['path']
            source_root = None
            if isinstance(frame_path, str):
                normalized_path = frame_path.replace('\\', '/')
                suffix = '/' + candidate['path']
                if normalized_path.endswith(suffix):
                    source_root = normalized_path[:-len(suffix)] or '/'
            mapped_path, mapping_error = map_frame_path(frame_path, source_root)
            if (not record or record.get('candidate_id') != identifier or record['status'] != 'candidate' or record['code'] is not None
                    or record['mapped_path'] != candidate['path'] or frame['in_app'] is not True
                    or mapping_error or mapped_path != candidate['path']
                    or type(frame['line']) is not int or not candidate['start_line'] <= frame['line'] <= candidate['end_line']):
                raise BundleError('frame_reference_invalid')
    candidate_records = {key for key, record in frame_records.items() if record['status'] == 'candidate'}
    if candidate_records != seen_refs:
        raise BundleError('frame_reference_invalid')
    unresolved = [record for record in evidence['frames'] if record['status'] == 'unresolved']
    if unresolved != evidence['unresolved_frames']:
        raise BundleError('frame_reference_invalid')
    return commit


def make_bundle(case, evidence):
    """Bind a newly prepared pair; never infer an original binding for old files."""
    _serialized_limit(case, MAX_CASE_BYTES, 'case')
    _serialized_limit(evidence, MAX_EVIDENCE_BYTES, 'evidence')
    commit = _validate_pair(case, evidence)
    bundle = {'format': 'incident-bundle/v1', 'case_file': 'case.json', 'evidence_file': 'evidence.json',
              'case_sha256': _hash(_case_content(case)), 'evidence_sha256': _hash(evidence),
              'input_sha256': evidence['input']['sha256'], 'selected_event_ref': evidence['selected_event_ref'],
              'event_commit': commit,
              'required_local_only': [candidate['id'] for candidate in case['candidates'] if candidate.get('local_only') is True]}
    _serialized_limit(bundle, MAX_BUNDLE_BYTES, 'bundle')
    return bundle


def _load(path, limit, kind):
    try:
        # Refuse symlink artifacts and non-files before reading bounded bytes.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BundleError(kind + '_read_invalid')
            raw = stream.read(limit + 1)
    except OSError:
        raise BundleError(kind + '_unavailable')
    if len(raw) > limit:
        raise BundleError(kind + '_size_limit')
    try:
        text = raw.decode('utf-8', errors='strict')
        depth, quoted, escaped = 0, False, False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == '\\':
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in '[{':
                depth += 1
                if depth > MAX_DEPTH:
                    raise BundleError('json_depth_limit')
            elif char in ']}':
                depth -= 1
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise BundleError('json_duplicate_key')
                result[key] = value
            return result
        def constant(_):
            raise BundleError('json_nonfinite')
        def finite(token):
            value = float(token)
            if not math.isfinite(value):
                raise BundleError('json_nonfinite')
            return value
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite)
        _walk(value)
        return value
    except BundleError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise BundleError('json_invalid')


def _manifest(bundle):
    _fields(bundle, ('format', 'case_file', 'evidence_file', 'case_sha256', 'evidence_sha256', 'input_sha256',
                     'selected_event_ref', 'event_commit', 'required_local_only'))
    if bundle['format'] != 'incident-bundle/v1' or bundle['case_file'] != 'case.json' or bundle['evidence_file'] != 'evidence.json':
        raise BundleError('bundle_schema_invalid')
    for key in ('case_sha256', 'evidence_sha256', 'input_sha256'):
        if not isinstance(bundle[key], str) or not _SHA.fullmatch(bundle[key]):
            raise BundleError('bundle_schema_invalid')
    if not isinstance(bundle['event_commit'], str) or not _OID.fullmatch(bundle['event_commit']) or not isinstance(bundle['selected_event_ref'], str) or not _EVENT_REF.fullmatch(bundle['selected_event_ref']):
        raise BundleError('bundle_schema_invalid')
    values = bundle['required_local_only']
    if not isinstance(values, list) or len(values) > 12 or any(not isinstance(item, str) or not _ID.fullmatch(item) for item in values) or len(set(values)) != len(values):
        raise BundleError('bundle_schema_invalid')


class _VerifiedBundle(dict):
    """Public JSON report plus a private, process-local source-read capability."""
    pass


def _report(status='invalid', diagnostics=(), checked=0, version=None):
    return {'schema_version': 1, 'status': status, 'diagnostics': list(diagnostics),
            'checked_candidates': checked, 'version': version}


def _source_lines(text):
    if '\x00' in text or any(char in text for char in '\v\f\x1c\x1d\x1e\x85\u2028\u2029') or re.search(r'\r(?!\n)', text):
        raise BundleError('source_line_separators_unsupported')
    return text.splitlines(keepends=True)


def _verify(bundle_path, repo, git):
    checked = 0
    version = None
    try:
        bundle = _load(bundle_path, MAX_BUNDLE_BYTES, 'bundle')
        _manifest(bundle)
        folder = Path(bundle_path).parent
        case = _load(folder / 'case.json', MAX_CASE_BYTES, 'case')
        evidence = _load(folder / 'evidence.json', MAX_EVIDENCE_BYTES, 'evidence')
        commit = _validate_pair(case, evidence)
        if bundle['evidence_sha256'] != _hash(evidence):
            raise BundleError('evidence_hash_mismatch')
        if bundle['case_sha256'] != _hash(_case_content(case)):
            raise BundleError('case_hash_mismatch')
        if bundle['input_sha256'] != evidence['input']['sha256'] or bundle['selected_event_ref'] != evidence['selected_event_ref'] or bundle['event_commit'] != commit:
            raise BundleError('event_identity_mismatch')
        candidates = {item['id']: item for item in case['candidates']}
        if any(identifier not in candidates or candidates[identifier].get('local_only') is not True for identifier in bundle['required_local_only']):
            raise BundleError('local_only_relaxed')
        for candidate in candidates.values():
            if (sensitive(candidate['snippet']) or sensitive_path(candidate['path']) or sanitize_text(candidate['snippet']) != candidate['snippet']) and candidate.get('local_only') is not True:
                raise BundleError('local_only_relaxed')
        checkout = evidence['version']['checkout']
        version = {'event_commit': commit, 'selected_event_ref': bundle['selected_event_ref'],
                   'evidence_status': evidence['status'], 'reviewed_for_secrets': case['reviewed_for_secrets'],
                   'checkout_head': checkout['head'] if checkout['head'] is None or _OID.fullmatch(checkout['head']) else None,
                   'checkout_comparison': checkout['comparison'] if checkout['comparison'] in ('same', 'different', 'unknown') else 'unknown'}
        provenance = {item['id']: item for item in evidence['candidate_provenance']}
        cache = {}
        for candidate in case['candidates']:
            path = candidate['path']
            if path not in cache:
                cache[path] = read_source_blob(repo, commit, path, git=git)
            blob = cache[path]
            if not blob.get('ok'):
                code = blob.get('code', 'source_unavailable')
                return _report('unavailable', [code], checked, version), None
            item = provenance[candidate['id']]
            if blob['oid'] != item['blob_oid']:
                raise BundleError('blob_oid_mismatch')
            lines = _source_lines(blob['text'])
            if candidate['end_line'] > len(lines):
                raise BundleError('source_range_mismatch')
            snippet = ''.join(lines[candidate['start_line'] - 1:candidate['end_line']])
            if snippet != candidate['snippet'] or hashlib.sha256(snippet.encode('utf-8')).hexdigest() != item['snippet_sha256']:
                raise BundleError('source_snippet_mismatch')
            checked += 1
        report = _report('verified', (), checked, version)
        return report, {'case': case, 'evidence': evidence, 'bundle_hash': _hash(bundle), 'git': git,
                        'repo': repo, 'bundle_path': bundle_path, 'token': _CAPABILITY}
    except BundleError as error:
        if error.code == 'bundle_unavailable' and not Path(bundle_path).exists() and ((Path(bundle_path).parent / 'case.json').is_file() or (Path(bundle_path).parent / 'evidence.json').is_file()):
            return _report('legacy_unbound', ['bundle_missing']), None
        status = 'unavailable' if error.code.endswith('_unavailable') else 'invalid'
        return _report(status, [error.code], checked, version), None
    except (OSError, ValueError, UnicodeError, TypeError, KeyError, RecursionError):
        return _report('invalid', ['artifact_schema_invalid'], checked, version), None


def verify_bundle(bundle_path, repo):
    """Verify the complete package before minting a local source-read capability."""
    try:
        path = Path(bundle_path).absolute()
        repo = Path(repo).absolute()
        report, handle = _verify(path, repo, GitSession(repo))
    except (OSError, ValueError, TypeError):
        return _report('invalid', ['bundle_read_invalid'])
    if handle is None:
        return report
    result = _VerifiedBundle(report)
    result._handle = handle
    return result


def _write_new_file(path, raw):
    """Use directory FDs and O_NOFOLLOW: conflicts never replace existing files."""
    directory = None
    descriptor = None
    created = False
    try:
        path = Path(path).absolute()
        if path.name in ('', '.', '..'):
            raise OSError('invalid target')
        # macOS exposes /var and /tmp as OS-owned aliases. Canonicalize those
        # roots once; every user-selected remaining component is no-follow.
        parts = path.parts
        if len(parts) > 1 and parts[1] in ('var', 'tmp') and Path('/' + parts[1]).is_symlink():
            path = Path(os.path.realpath('/' + parts[1])).joinpath(*parts[2:])
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open('/', flags)
        for part in path.parent.parts[1:]:
            next_directory = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        created = True
        with os.fdopen(descriptor, 'wb') as stream:
            descriptor = None
            stream.write(raw)
        return str(path)
    except (OSError, ValueError, TypeError):
        if descriptor is not None:
            os.close(descriptor)
        if created and directory is not None:
            try:
                os.unlink(path.name, dir_fd=directory)
            except OSError:
                pass
        raise BundleError('source_output_invalid')
    finally:
        if directory is not None:
            os.close(directory)


def read_candidate_context(verified, candidate_id, context_lines=20, source_output=None):
    """Export exact historical whole lines; public dictionaries confer no trust."""
    failure = lambda code, status='invalid': {'status': status, 'diagnostics': [code]}
    if type(context_lines) is not int or not 0 <= context_lines <= 60 or not isinstance(candidate_id, str) or not _ID.fullmatch(candidate_id) or source_output is None:
        return failure('context_arguments_invalid')
    if type(verified) is not _VerifiedBundle or not isinstance(getattr(verified, '_handle', None), dict):
        return failure('verification_required')
    handle = verified._handle
    if handle.get('token') is not _CAPABILITY:
        return failure('verification_required')
    try:
        report, current = _verify(handle['bundle_path'], handle['repo'], handle['git'])
        if report['status'] != 'verified':
            return {'status': report['status'], 'diagnostics': report['diagnostics']}
        if current['bundle_hash'] != handle['bundle_hash']:
            return failure('bundle_changed')
        candidate = next((item for item in current['case']['candidates'] if item['id'] == candidate_id), None)
        if candidate is None:
            return failure('candidate_unknown')
        provenance = next(item for item in current['evidence']['candidate_provenance'] if item['id'] == candidate_id)
        blob = read_source_blob(handle['repo'], provenance['event_commit'], candidate['path'], git=handle['git'])
        if not blob.get('ok'):
            return failure(blob.get('code', 'source_unavailable'), 'unavailable')
        if blob['oid'] != provenance['blob_oid']:
            return failure('blob_oid_mismatch')
        lines = _source_lines(blob['text'])
        snippet = ''.join(lines[candidate['start_line'] - 1:candidate['end_line']])
        if snippet != candidate['snippet'] or hashlib.sha256(snippet.encode('utf-8')).hexdigest() != provenance['snippet_sha256']:
            return failure('source_snippet_mismatch')
        start = max(1, candidate['start_line'] - context_lines)
        end = min(len(lines), candidate['end_line'] + context_lines)
        raw = ''.join(lines[start - 1:end]).encode('utf-8')
        if len(raw) > MAX_CONTEXT_BYTES:
            return failure('context_output_limit')
        output_path = _write_new_file(source_output, raw)
        return {'status': 'written', 'diagnostics': [], 'source_output': sanitize_text(output_path),
                'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'start_line': start, 'end_line': end,
                'event_commit': provenance['event_commit'], 'blob_oid': provenance['blob_oid']}
    except BundleError as error:
        return failure(error.code)
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, RecursionError):
        return failure('context_read_invalid')
