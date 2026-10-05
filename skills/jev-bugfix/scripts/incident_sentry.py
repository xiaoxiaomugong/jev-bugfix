#!/usr/bin/env python3
"""Offline whitelist projection of a Sentry project-event API JSON response.

No API clients, credential readers, downloads, Git or scorer calls live here.
Exact retained values stay in memory for downstream release/path resolution;
call incident_evidence.sanitize_value before persistence or display.
"""

import hashlib
import json
import re

from incident_evidence import (EvidenceError, MAX_BREADCRUMBS, MAX_FRAMES,
                               load_bounded_json, normalize_projection)

ADAPTER = 'sentry-api-event/v1'
ADAPTER_VERSION = '1'
MAPPING_VERSION = '1'
MAX_ENTRIES = 200
MAX_TAGS = 200
MAX_EXCEPTIONS = 32
MAX_OMITTED = 2147483647
URI = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
WINDOWS_DRIVE = re.compile(r'^[A-Za-z]:[\\/]')
ROOT_FIELDS = frozenset(('id', 'eventID', 'groupID', 'projectID', 'message', 'title', 'location',
    'user', 'tags', 'platform', 'dateReceived', 'dateCreated', 'contexts', 'size', 'entries',
    'dist', 'sdk', 'context', 'extra', 'packages', 'type', 'metadata', 'errors', 'occurrence',
    '_meta', 'crashFile', 'culprit', 'fingerprints', 'groupingConfig', 'release', 'userReport',
    'sdkUpdates', 'resolvedWith', 'nextEventID', 'previousEventID', 'startTimestamp',
    'endTimestamp', 'measurements', 'breakdowns', 'headers', 'cookies', 'request'))
EXCEPTION_FIELDS = frozenset(('type', 'value', 'mechanism', 'threadId', 'module', 'stacktrace',
    'rawStacktrace', 'rawValue', 'rawModule', 'rawType'))
FRAME_FIELDS = frozenset(('filename', 'absPath', 'module', 'package', 'platform', 'instructionAddr',
    'symbolAddr', 'function', 'rawFunction', 'symbol', 'context', 'lineNo', 'colNo', 'parentIndex',
    'sampleCount', 'inApp', 'trust', 'errors', 'lock', 'sourceLink', 'vars', 'addrMode', 'map',
    'origFunction', 'origAbsPath', 'origFilename', 'origLineNo', 'origColNo'))
OMISSION_FIELDS = ('request', 'user', 'headers', 'cookies', 'frame_vars', 'frame_context',
    'frame_source_link', 'raw_stacktrace', 'extra', 'breadcrumb_data', 'unknown_fields',
    'unknown_entries', 'unselected_exceptions', 'sentry_omitted_frames', 'sentry_omitted_exceptions')


def _object(value, optional=False):
    if value is None and optional:
        return {}
    if type(value) is not dict:
        raise EvidenceError('input_type_invalid')
    return value


def _collection(value, maximum, code):
    if value is None:
        return []
    if type(value) is not list:
        raise EvidenceError('input_type_invalid')
    if len(value) > maximum:
        raise EvidenceError(code)
    return value


def _unicode(value):
    """Ignored strings must be legal Unicode, but have no per-string size cap."""
    if isinstance(value, str):
        try:
            value.encode('utf-8', errors='strict')
        except UnicodeEncodeError:
            raise EvidenceError('input_encoding_invalid') from None
    elif isinstance(value, dict):
        for key, item in value.items():
            _unicode(key)
            _unicode(item)
    elif isinstance(value, list):
        for item in value:
            _unicode(item)


def _unknown(value, allowed, omissions):
    omissions['unknown_fields'] += len(set(value) - set(allowed))


def _presence(value):
    return int(value is not None and value != {} and value != [])


def _ignored_container(value):
    # Count a fixed category, never traverse or emit its user-controlled keys.
    if isinstance(value, (dict, list)):
        return len(value)
    return _presence(value)


def _omitted(value):
    if value is None:
        return 0
    if (type(value) is not list or len(value) != 2 or
            any(type(item) is not int or item < 0 or item > MAX_OMITTED for item in value) or
            value[1] < value[0]):
        raise EvidenceError('input_type_invalid')
    return value[1] - value[0]


def _tag(tags, key):
    values = []
    indices = []
    for index, tag in enumerate(tags):
        tag = _object(tag)
        if tag.get('key') == key:
            value = tag.get('value')
            if value is not None and type(value) is not str:
                raise EvidenceError('input_type_invalid')
            values.append(value)
            indices.append(index)
    conflict = len(set(values)) > 1
    value = None if conflict or not values else values[0]
    source = 'tags[%d].value' % indices[0] if indices and not conflict else None
    return value, source, conflict


def _preflight(raw, exception_index):
    if type(raw) is not dict or 'entries' not in raw:
        raise EvidenceError('sentry_api_shape_invalid')
    entries = _collection(raw['entries'], MAX_ENTRIES, 'sentry_entries_over_limit')
    # Null entries is not an API single-event response array.
    if raw['entries'] is None:
        raise EvidenceError('input_type_invalid')
    tags = _collection(raw.get('tags'), MAX_TAGS, 'sentry_tags_over_limit')
    located = {}
    for index, entry in enumerate(entries):
        entry = _object(entry)
        kind = entry.get('type')
        if type(kind) is not str:
            raise EvidenceError('input_type_invalid')
        if kind in ('exception', 'breadcrumbs'):
            if kind in located:
                raise EvidenceError('sentry_unsupported_shape')
            data = _object(entry.get('data'), optional=True)
            maximum = MAX_EXCEPTIONS if kind == 'exception' else MAX_BREADCRUMBS
            code = 'sentry_exceptions_over_limit' if kind == 'exception' else 'breadcrumbs_over_limit'
            values = _collection(data.get('values'), maximum, code)
            located[kind] = (index, data, values)
            if kind == 'exception':
                for exception in values:
                    _object(exception)
    values = located.get('exception', (None, {}, []))[2]
    if exception_index is not None and (type(exception_index) is not int or
                                       not 0 <= exception_index < len(values)):
        raise EvidenceError('sentry_exception_index_invalid')
    selected_index = exception_index if exception_index is not None else 0 if len(values) == 1 else None
    if selected_index is not None:
        stack = _object(values[selected_index].get('stacktrace'), optional=True)
        _collection(stack.get('frames'), MAX_FRAMES, 'frames_over_limit')
    return entries, tags, located


def load_sentry_api_event(path, exception_index=None, service=None):
    """Return normalized evidence and fixed import provenance for one API event.

    status describes conversion completeness only; downstream preparation still
    has to resolve the event version and collect candidates. This reader is
    always offline and never persists a raw or intermediate external object.
    """
    raw, source_sha256 = load_bounded_json(path)
    entries, tags, located = _preflight(raw, exception_index)
    _unicode(raw)
    if type(raw.get('eventID')) is not str or not raw['eventID']:
        raise EvidenceError('event_id_invalid')
    if service is not None and (type(service) is not str or not service):
        raise EvidenceError('input_type_invalid')
    omissions = {key: 0 for key in OMISSION_FIELDS}
    _unknown(raw, ROOT_FIELDS, omissions)
    diagnostics = []
    incomplete = []
    needs_input = False

    def gap(code, blocking=False):
        nonlocal needs_input
        if code not in incomplete:
            incomplete.append(code)
            diagnostics.append(code)
        needs_input = needs_input or blocking

    field_sources = {'event_id': 'eventID', 'timestamp': None, 'service': None,
        'environment': None, 'release': None, 'exception': None, 'breadcrumbs': None,
        'trace': None, 'runtime': None, 'os': None}
    event = {'event_id': raw['eventID'], 'timestamp': raw.get('dateCreated'), 'commit': None}
    if event['timestamp']:
        field_sources['timestamp'] = 'dateCreated'
    else:
        gap('sentry_timestamp_missing')
    # Count unknown tag fields once (two mappings must not double the count).
    for tag in tags:
        _unknown(_object(tag), ('key', 'value', 'query'), omissions)
    # _tag reads only the explicitly named tags; unknown tag names never escape.
    environment, source, conflict = _tag(tags, 'environment')
    event['environment'] = environment
    field_sources['environment'] = source
    if conflict:
        gap('sentry_environment_conflict', blocking=True)
    elif not environment:
        gap('sentry_environment_missing')
    service_tag, source, conflict = _tag(tags, 'service')
    if service is not None and service_tag is not None and service != service_tag:
        conflict = True
    event['service'] = None if conflict else service if service is not None else service_tag
    field_sources['service'] = None if conflict else 'argument.service' if service is not None else source
    if conflict:
        gap('sentry_service_conflict', blocking=True)
    elif not event['service']:
        gap('sentry_service_missing')
    release = _object(raw.get('release'), optional=True)
    _unknown(release, ('version', 'shortVersion', 'dateReleased', 'dateCreated', 'dateStarted',
        'dateFinished', 'commitCount', 'lastCommit', 'lastDeploy', 'data', 'url', 'newGroups',
        'ref', 'projects', 'authors', 'deployCount', 'firstEvent', 'lastEvent', 'owner', 'versionInfo'), omissions)
    event['release'] = release.get('version')
    if event['release']:
        field_sources['release'] = 'release.version'
    else:
        gap('sentry_release_missing')

    omissions['request'] += _presence(raw.get('request'))
    omissions['user'] += _presence(raw.get('user'))
    omissions['headers'] += _ignored_container(raw.get('headers'))
    omissions['cookies'] += _ignored_container(raw.get('cookies'))
    omissions['extra'] += _ignored_container(raw.get('context')) + _ignored_container(raw.get('extra'))
    for entry in entries:
        _unknown(entry, ('type', 'data'), omissions)
        if entry['type'] == 'request':
            omissions['request'] += 1
            data = entry.get('data')
            if type(data) is dict:
                omissions['headers'] += _ignored_container(data.get('headers'))
                omissions['cookies'] += _ignored_container(data.get('cookies'))
        elif entry['type'] not in ('exception', 'breadcrumbs'):
            omissions['unknown_entries'] += 1
            if entry['type'] == 'spans':
                gap('sentry_spans_unimported')

    entry_index, data, exceptions = located.get('exception', (None, {}, []))
    _unknown(data, ('values', 'excOmitted', 'hasSystemFrames'), omissions)
    omitted_exceptions = _omitted(data.get('excOmitted'))
    omissions['sentry_omitted_exceptions'] = omitted_exceptions
    if omitted_exceptions:
        gap('sentry_exceptions_omitted')
    selected_index = exception_index if exception_index is not None else 0 if len(exceptions) == 1 else None
    if len(exceptions) > 1 and selected_index is None:
        gap('sentry_exception_selection_required', blocking=True)
    if not exceptions:
        gap('sentry_exception_missing')
    unselected = [index for index in range(len(exceptions)) if index != selected_index]
    omissions['unselected_exceptions'] = len(unselected)
    if unselected and selected_index is not None:
        gap('sentry_exception_chain_unprocessed')
    event['exception'] = {'type': None, 'message': None, 'frames': []}
    frame_sources = []
    # All exception entries are counted without importing unselected text.
    for exception in exceptions:
        _unknown(exception, EXCEPTION_FIELDS, omissions)
        omissions['raw_stacktrace'] += _presence(exception.get('rawStacktrace'))
    if selected_index is not None:
        exception = exceptions[selected_index]
        field_sources['exception'] = 'entries[%d].data.values[%d]' % (entry_index, selected_index)
        event['exception']['type'] = exception.get('type')
        event['exception']['message'] = exception.get('value')
        if not event['exception']['type']:
            gap('sentry_exception_type_missing')
        if not event['exception']['message']:
            gap('sentry_exception_message_missing')
        stack = _object(exception.get('stacktrace'), optional=True)
        _unknown(stack, ('frames', 'framesOmitted', 'registers', 'hasSystemFrames'), omissions)
        omissions['sentry_omitted_frames'] = _omitted(stack.get('framesOmitted'))
        if omissions['sentry_omitted_frames']:
            gap('sentry_frames_omitted')
        frames = _collection(stack.get('frames'), MAX_FRAMES, 'frames_over_limit')
        if not frames:
            gap('sentry_frames_missing')
        for index, frame in enumerate(frames):
            frame = _object(frame)
            _unknown(frame, FRAME_FIELDS, omissions)
            for source, category in (('vars', 'frame_vars'), ('context', 'frame_context'), ('sourceLink', 'frame_source_link')):
                omissions[category] += _presence(frame.get(source))
            path_source = 'filename' if frame.get('filename') is not None else 'absPath' if frame.get('absPath') is not None else None
            path_value = frame.get(path_source) if path_source else None
            projected = {'path': path_value, 'line': frame.get('lineNo'),
                         'function': frame.get('function'), 'in_app': frame.get('inApp')}
            event['exception']['frames'].append(projected)
            frame_sources.append({'normalized_index': index, 'entry_index': entry_index,
                'exception_index': selected_index, 'frame_index': index, 'path_source': path_source})
            for key, code in (('path', 'sentry_frame_path_missing'), ('line', 'sentry_frame_line_missing'),
                              ('function', 'sentry_frame_function_missing')):
                if not projected[key]:
                    gap(code)
            if projected['in_app'] is None:
                gap('sentry_frame_in_app_missing')
            if isinstance(path_value, str) and URI.match(path_value) and not WINDOWS_DRIVE.match(path_value):
                gap('sentry_frame_path_unsupported')

    event['breadcrumbs'] = []
    crumb_index, crumb_data, crumbs = located.get('breadcrumbs', (None, {}, []))
    if crumb_index is not None:
        field_sources['breadcrumbs'] = 'entries[%d].data.values' % crumb_index
    _unknown(crumb_data, ('values',), omissions)
    for crumb in crumbs:
        crumb = _object(crumb)
        _unknown(crumb, ('timestamp', 'category', 'level', 'message', 'data', 'type', 'event_id'), omissions)
        has_data = _presence(crumb.get('data'))
        omissions['breadcrumb_data'] += has_data
        if has_data and not crumb.get('message'):
            gap('sentry_breadcrumb_details_unimported')
        event['breadcrumbs'].append({key: crumb.get(key) for key in ('timestamp', 'category', 'level', 'message')})

    contexts = _object(raw.get('contexts'), optional=True)
    _unknown(contexts, ('trace', 'runtime', 'os', 'device', 'browser', 'app', 'gpu', 'response',
        'replay', 'cloud_resource', 'profile', 'flags', 'feedback', 'organization'), omissions)
    trace = _object(contexts.get('trace'), optional=True)
    if trace:
        field_sources['trace'] = 'contexts.trace'
    _unknown(trace, ('trace_id', 'span_id', 'parent_span_id', 'type', 'op', 'status', 'origin',
        'data', 'sampled', 'client_sample_rate', 'exclusive_time', 'description'), omissions)
    event['trace'] = {key: trace.get(key) for key in ('trace_id', 'span_id', 'parent_span_id')}
    event['trace']['spans'] = []
    runtime = _object(contexts.get('runtime'), optional=True)
    os_context = _object(contexts.get('os'), optional=True)
    if runtime:
        field_sources['runtime'] = 'contexts.runtime'
    if os_context:
        field_sources['os'] = 'contexts.os'
    _unknown(runtime, ('type', 'name', 'version', 'raw_description', 'build'), omissions)
    _unknown(os_context, ('type', 'name', 'version', 'raw_description', 'build', 'kernel_version', 'rooted'), omissions)
    event['runtime'] = {'language': runtime.get('name'), 'version': runtime.get('version'),
                        'os': os_context.get('name'), 'arch': None}

    normalized = normalize_projection({'schema_version': 1, 'events': [event]}, source_sha256, adapter=ADAPTER)
    projection = {'schema_version': 1, 'events': [{key: value for key, value in normalized['events'][0].items()
                                                if key != 'provenance'}]}
    projection_sha256 = hashlib.sha256(json.dumps(projection, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    status = 'needs_input' if needs_input else 'partial' if incomplete else 'ready'
    normalized.update(status=status, incomplete=incomplete, diagnostics=diagnostics)
    normalized['import_provenance'] = {'schema_version': 1, 'adapter': ADAPTER,
        'adapter_version': ADAPTER_VERSION, 'mapping_version': MAPPING_VERSION,
        'source_sha256': source_sha256, 'projection_sha256': projection_sha256,
        'exception_selection': {'entry_index': entry_index, 'exception_index': selected_index,
            'exception_count': len(exceptions), 'unselected_count': len(unselected), 'unselected_indices': unselected},
        'field_sources': field_sources, 'frames': frame_sources, 'omissions': omissions,
        'diagnostics': list(diagnostics), 'completeness': {'status': status, 'incomplete': list(incomplete)}}
    return normalized
