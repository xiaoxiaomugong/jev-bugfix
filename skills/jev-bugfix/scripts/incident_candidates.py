"""Collect application exception candidates exclusively from a frozen Git commit."""
import hashlib
from pathlib import PureWindowsPath
import re

from rank_candidates import sensitive, sensitive_path
from incident_evidence import sanitize_text


def map_frame_path(path, source_root=None):
    """Map only explicit absolute prefixes; never infer a basename or normalize '..'."""
    if not isinstance(path, str) or not path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return None, 'frame_path_invalid'
    windows = bool(re.match(r'^[A-Za-z]:', path) or path.startswith('\\\\'))
    if windows:
        unanchored = path.replace('\\', '/')[2:]
        if any(part in ('', '.', '..') for part in unanchored.lstrip('/').split('/')):
            return None, 'frame_path_invalid'
        value = PureWindowsPath(path)
        if not value.is_absolute() or not source_root:
            return None, 'source_root_required'
        root = PureWindowsPath(source_root)
        if not root.is_absolute() or any(p in ('.', '..') for p in source_root.replace('\\', '/').split('/')):
            return None, 'frame_path_invalid'
        # Explicit Windows mapping uses Windows segment and drive semantics.
        parts, prefix = value.parts, root.parts
        if len(parts) <= len(prefix) or [p.casefold() for p in parts[:len(prefix)]] != [p.casefold() for p in prefix]:
            return None, 'source_root_mismatch'
        mapped = '/'.join(parts[len(prefix):])
    elif path.startswith('/'):
        if not source_root:
            return None, 'source_root_required'
        root = source_root.rstrip('/')
        if not root.startswith('/') or not path.startswith(root + '/'):
            return None, 'source_root_mismatch'
        mapped = path[len(root) + 1:]
        if '..' in path.split('/') or '.' in path.split('/'):
            return None, 'frame_path_invalid'
    else:
        mapped = path
    if (not mapped or mapped.startswith('/') or '\\' in mapped or ':' in mapped
            or any(p in ('', '.', '..') for p in mapped.split('/'))
            or len(mapped.encode('utf-8')) > 512):
        return None, 'frame_path_invalid'
    if sensitive(mapped) or sanitize_text(mapped) != mapped:
        return None, 'sensitive_frame_metadata'
    parts = mapped.lower().split('/')
    if (any(p in ('dist', 'build', '.next', '.nuxt', 'webpack') for p in parts[:-1])
            or re.search(r'\.min\.[^.]+$|\.map$', parts[-1])):
        return None, 'build_artifact_unmapped'
    return mapped, None


def source_interval(text, line):
    """Keep the exception line and whole continuous source lines under V1 limits."""
    if any(character in text for character in '\v\f\x1c\x1d\x1e\x85\u2028\u2029') or re.search(r'\r(?!\n)', text):
        # V1 splitlines uses broader separators than source line numbers. Do not bind the wrong line.
        return None, 'source_line_separators_unsupported'
    lines = text.splitlines(keepends=True)
    if line > len(lines):
        return None, 'frame_line_out_of_range'
    if '\x00' in text:
        return None, 'source_encoding_invalid'
    if len(lines[line - 1].encode('utf-8')) > 2048:
        return None, 'source_line_over_budget'
    start, end = max(1, line - 29), min(len(lines), line + 30)
    while len(''.join(lines[start - 1:end]).encode('utf-8')) > 2048:
        if line - start >= end - line and start < line:
            start += 1
        elif end > line:
            end -= 1
        else:
            return None, 'source_line_over_budget'
    snippet = ''.join(lines[start - 1:end])
    if not snippet.strip() or len(snippet.splitlines()) != end - start + 1:
        return None, 'source_text_unusable'
    return {'start_line': start, 'end_line': end, 'snippet': snippet}, None


def collect_candidates(repo, selected, version_report, source_root=None, git=None):
    """Return V1 candidates plus a separate complete source and frame inventory."""
    from incident_versions import GitSession, read_source_blob
    result = {'candidates': [], 'provenance': [], 'frames': [], 'unresolved_frames': [], 'diagnostics': []}
    frames = selected.get('exception', {}).get('frames', [])
    resolution = version_report.get('resolution', {})
    commit = resolution.get('event_commit')
    usable_version = resolution.get('status') == 'resolved' and commit
    if usable_version:
        git = git or GitSession(repo)
    inventory = {}
    blob_cache = {}
    for index, frame in enumerate(frames):
        record = {'event_id': selected['event_id'], 'frame_index': index,
                  'path': frame.get('path'), 'mapped_path': None, 'status': 'unresolved', 'code': None}
        if 'event_ref' in selected:
            record['event_ref'] = selected['event_ref']
        code = None
        if not usable_version:
            code = 'event_version_' + resolution.get('status', 'unknown')
        elif frame.get('in_app') is not True:
            code = 'frame_not_verified_application'
        elif type(frame.get('line')) is not int or frame['line'] < 1:
            code = 'frame_line_unknown'
        else:
            mapped, code = map_frame_path(frame.get('path'), source_root)
            record['mapped_path'] = mapped
            if not code:
                if mapped not in blob_cache:
                    blob_cache[mapped] = read_source_blob(repo, commit, mapped, git=git)
                blob = blob_cache[mapped]
                if not blob.get('ok'):
                    code = blob.get('code') or 'source_unavailable'
                else:
                    interval, code = source_interval(blob['text'], frame['line'])
                    if not code:
                        key = (mapped, interval['start_line'], interval['end_line'])
                        reference = {'event_id': selected['event_id'],
                                     'event_indices': selected.get('provenance', {}).get('indices', []),
                                     'frame_index': index}
                        if 'event_ref' in selected:
                            reference['event_ref'] = selected['event_ref']
                        if key not in inventory:
                            identity = '\0'.join((commit, blob['oid'], mapped,
                                                   str(interval['start_line']), str(interval['end_line'])))
                            identifier = 'stack_' + hashlib.sha256(identity.encode()).hexdigest()[:24]
                            candidate = dict(id=identifier, path=mapped, origins=['stack'], **interval)
                            # Source stays byte-exact locally. Sensitive excerpts never enter the scoring payload.
                            if sensitive(interval['snippet']) or sensitive_path(mapped) or sanitize_text(interval['snippet']) != interval['snippet']:
                                candidate['local_only'] = True
                            provenance = {'id': identifier, 'event_commit': commit, 'blob_oid': blob['oid'],
                                          'snippet_sha256': hashlib.sha256(interval['snippet'].encode()).hexdigest(),
                                          'path': mapped, 'start_line': interval['start_line'],
                                          'end_line': interval['end_line'], 'evidence_refs': []}
                            inventory[key] = (candidate, provenance)
                            result['candidates'].append(candidate)
                            result['provenance'].append(provenance)
                        candidate, provenance = inventory[key]
                        provenance['evidence_refs'].append(reference)
                        record.update(status='candidate', candidate_id=candidate['id'])
        record['code'] = code
        result['frames'].append(record)
        if code:
            result['unresolved_frames'].append(dict(record))
    if not frames or not any(frame.get('in_app') is True for frame in frames):
        result['diagnostics'].append('no_application_stack')
    if len(result['candidates']) > 12:
        result['diagnostics'].append('candidates_over_budget')
    return result
