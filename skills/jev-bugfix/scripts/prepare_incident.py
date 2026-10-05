#!/usr/bin/env python3
"""Prepare local production-events/v1 evidence and an optional strict V1 case."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace

from incident_candidates import collect_candidates
from incident_evidence import EvidenceError, escape_markdown, load_events, parse_timestamp, sanitize_text, sanitize_value, select_incident
from incident_versions import GitSession, load_release_map, resolve_incident_version
import rank_candidates as ranker


CONCLUSIONS = {'online_observation': 'imported_only', 'local_reproduction': 'not_attempted',
               'root_cause': 'hypothesis', 'fix_verification': 'unverified',
               'regression_attribution': 'unknown'}


def bounded_summary(value, maximum, field, omissions):
    clean = sanitize_text(value)
    encoded = clean.encode('utf-8')
    if len(encoded) > maximum:
        omissions.append({'field': field, 'reason': 'summary_byte_limit',
                          'original_bytes': len(encoded), 'limit_bytes': maximum})
        clean = encoded[:maximum].decode('utf-8', errors='ignore')
    return clean.strip() or '未知'


def make_case(selected, candidates, omissions):
    exception = selected['exception']
    observed = '%s: %s' % (exception['type'] or '异常类型未知', exception['message'] or '异常详情未知')
    stack = []
    frames = exception['frames']
    for frame in frames[:8]:
        stack.append(bounded_summary('%s:%s in %s' % (frame['path'] or '路径未知',
                                                      frame['line'] or '行号未知', frame['function'] or '函数未知'),
                                     512, 'bug.stack_trace', omissions))
    if len(frames) > 8:
        omissions.append({'field': 'bug.stack_trace', 'reason': 'summary_item_limit',
                          'original_items': len(frames), 'limit_items': 8})
    return {'schema_version': 1, 'reviewed_for_secrets': False,
            'bug': {'description': bounded_summary('线上观察：' + observed, 1024, 'bug.description', omissions),
                    'reproduction': {'steps': ['尚无本地复现步骤；仅有导入的线上事件'],
                                     'expected': '未知；尚无已核实的业务预期',
                                     'actual': bounded_summary('仅有线上观察，尚无本地复现步骤。' + observed,
                                                               1024, 'bug.reproduction.actual', omissions)},
                    'stack_trace': stack},
            'candidates': candidates}


def observed_timeline(selected, related):
    items = [{'kind': 'selected_event', 'timestamp': selected['timestamp'], 'event_id': selected['event_id'],
              'event_ref': selected['event_ref']}]
    for index, breadcrumb in enumerate(selected['breadcrumbs']):
        items.append(dict(breadcrumb, kind='breadcrumb', event_ref=selected['event_ref'], evidence_index=index))
    for index, span in enumerate(selected['trace']['spans']):
        items.append(dict(span, kind='span', event_ref=selected['event_ref'], timestamp=span['start_timestamp'], evidence_index=index))
    for entry in related:
        # Only the representative's verified version belongs to this limited observed chain.
        if entry['included_in_version_chain']:
            items.append({'kind': 'related_event', 'timestamp': entry['timestamp'], 'event_id': entry['event_id'],
                          'event_ref': entry['event_ref']})
    def ordering(item):
        timestamp = item.get('timestamp')
        if timestamp:
            instant = parse_timestamp(timestamp)
            fraction = re.search(r'\.(\d+)', timestamp)
            return (0, instant.replace(microsecond=0), fraction.group(1).rstrip('0') if fraction else '')
        return (1, datetime.max.replace(tzinfo=timezone.utc), '')
    return sorted(items, key=ordering)


def build_evidence(args):
    evidence = {'schema_version': 1, 'status': 'needs_input', 'input': None, 'events': [],
                'selected_event': None, 'selected_event_ref': None, 'selection': None, 'version': None, 'related_events': [],
                'timeline': [], 'timeline_scope': 'Only imported observed order; completeness and causality unknown.',
                'candidate_provenance': [], 'frames': [], 'unresolved_frames': [], 'summary_omissions': [],
                'budgets': {}, 'diagnostics': [], 'conclusions': dict(CONCLUSIONS)}
    case = None
    try:
        normalized = load_events(args.input)
        # Display IDs may collide after redaction; references use original array positions.
        for event in normalized['events']:
            event['event_ref'] = 'event_%03d' % event['provenance']['indices'][0]
        evidence['input'] = {'sha256': normalized['input_sha256'], 'adapter': normalized['adapter']}
        evidence['events'] = normalized['events']
        evidence['diagnostics'].extend(normalized['diagnostics'])
        selection = select_incident(normalized, args.event_id)
        evidence['selection'] = {key: selection[key] for key in ('groups', 'diagnostics', 'status')}
        references = {event['event_id']: event['event_ref'] for event in normalized['events']}
        for group in evidence['selection']['groups']:
            group['event_refs'] = [references[identifier] for identifier in group['event_ids']]
        evidence['diagnostics'].extend(selection['diagnostics'])
        selected = selection['selected']
        if selected is None:
            return sanitize_value(evidence), None
        evidence['selected_event'] = selected['event_id']
        evidence['selected_event_ref'] = selected['event_ref']
        release_map = None
        if args.release_map:
            loaded_map = load_release_map(args.release_map)
            if not loaded_map['ok']:
                evidence['status'] = 'error'
                evidence['diagnostics'].extend(loaded_map['diagnostics'])
                return sanitize_value(evidence), None
            release_map = loaded_map['release_map']
        git = GitSession(args.repo)
        version = resolve_incident_version(args.repo, selected, release_map,
                                           args.baseline_revision, git=git)
        evidence['version'] = version
        evidence['diagnostics'].extend(version['diagnostics'])
        evidence['diagnostics'].extend(version['resolution']['diagnostics'])
        if version['baseline']:
            evidence['diagnostics'].extend(version['baseline']['diagnostics'])
        commit = version['resolution']['event_commit']
        for event in selection['related_events']:
            related_version = resolve_incident_version(args.repo, event, release_map, git=git)
            resolution = related_version['resolution']
            relationship = ('unknown_commit' if resolution['status'] != 'resolved' or not commit
                            else 'same_event_commit' if resolution['event_commit'] == commit
                            else 'different_event_commit')
            evidence['related_events'].append({'event_id': event['event_id'], 'event_ref': event['event_ref'], 'timestamp': event['timestamp'],
                                               'resolution': resolution, 'relationship': relationship,
                                               'included_in_version_chain': relationship == 'same_event_commit'})
            if relationship != 'same_event_commit':
                evidence['diagnostics'].append('related_version_conflict' if relationship == 'different_event_commit'
                                               else 'related_version_unknown')
        evidence['timeline'] = observed_timeline(selected, evidence['related_events'])
        collected = collect_candidates(args.repo, selected, version, args.source_root, git=git)
        evidence['candidate_provenance'] = collected['provenance']
        evidence['frames'] = collected['frames']
        evidence['unresolved_frames'] = collected['unresolved_frames']
        evidence['diagnostics'].extend(collected['diagnostics'])
        candidates = collected['candidates']
        if version['resolution']['status'] != 'resolved' or not candidates:
            evidence['status'] = 'needs_input'
            return sanitize_value(evidence), None
        case = make_case(selected, candidates, evidence['summary_omissions'])
        raw_case = json.dumps(case, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8') + b'\n'
        budget_report = ranker.rank_case(case, SimpleNamespace(execute=False, batch_timeout=45, request_timeout=10))
        evidence['budgets'] = {'candidate_count': len(candidates), 'max_candidates': 12,
                               'case_bytes': len(raw_case), 'max_case_bytes': 65536,
                               'payload_bytes': budget_report['usage']['payload_bytes'], 'max_payload_bytes': 24576}
        budget_codes = {d['code'] for d in budget_report['diagnostics']}
        if len(candidates) > 12 or len(raw_case) > 65536 or 'payload_limit' in budget_codes:
            evidence['diagnostics'].append('candidates_over_budget')
            evidence['status'] = 'needs_input'
            case = None
        elif budget_codes & {'input_invalid', 'sensitive_metadata', 'sensitive_bug'}:
            evidence['diagnostics'].append('case_shared_evidence_blocked')
            evidence['status'] = 'needs_input'
            case = None
        else:
            incomplete = bool(evidence['unresolved_frames'] or
                              any(not entry['included_in_version_chain'] for entry in evidence['related_events']) or
                              any(version['checkout'].get(field) is None for field in ('head', 'staged', 'unstaged', 'untracked')) or
                              version['checkout'].get('comparison') == 'unknown' or version['diagnostics'])
            if args.baseline_revision and (not version['baseline'] or version['baseline']['status'] != 'resolved'
                                          or version['baseline']['diagnostics']):
                incomplete = True
            evidence['status'] = 'partial' if incomplete else 'ready'
    except EvidenceError as error:
        evidence['status'] = 'error'
        evidence['diagnostics'].append(error.code)
        case = None
    except (OSError, ValueError, UnicodeError, RecursionError):
        evidence['status'] = 'error'
        evidence['diagnostics'].append('preparation_failed')
        case = None
    evidence['diagnostics'] = list(dict.fromkeys(evidence['diagnostics']))
    return sanitize_value(evidence), case


def render_report(evidence):
    """All dynamic values are redacted and rendered as literal text, never links."""
    lines = ['# 本地线上事件证据准备', '', '状态：' + evidence['status'], '',
             '仅有线上观察，尚无本地复现步骤。完整链路、因果、根因和修复均未确认。', '',
             '线上环境仅取自显式导入字段；未读取或转储环境变量。', '']
    def section(title, value):
        lines.extend(['## ' + title, ''])
        # Escaping JSON as prose prevents event text closing a code fence or embedding a resource.
        for line in json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).splitlines():
            lines.append(escape_markdown(line) + '  ')
        lines.append('')
    selected = next((event for event in evidence['events'] if event['event_ref'] == evidence['selected_event_ref']), None)
    section('代表事件与环境、release、实际异常', selected)
    section('各组与选择缺口', evidence['selection'])
    section('事件版本与当前 checkout', evidence['version'])
    section('关联事件逐条版本核对', evidence['related_events'])
    section('实际导入顺序；缺时间项在后；不声明因果', evidence['timeline'])
    section('候选来源与全部异常帧处理结果', {'candidate_provenance': evidence['candidate_provenance'],
                                                       'frames': evidence['frames'],
                                                       'unresolved_frames': evidence['unresolved_frames']})
    section('摘要省略、预算和诊断', {'omissions': evidence['summary_omissions'],
                                             'budgets': evidence['budgets'], 'diagnostics': evidence['diagnostics']})
    section('结论边界', evidence['conclusions'])
    lines.extend(['## 后续调查', '',
                  '用 sidecar 绑定的 event_commit 继续读取上下文、搜索符号和核实调用关系；'
                  'V1 的路径和行号不能直接用于读取 HEAD。缺 sidecar 时先补齐来源。', '',
                  'release 未解析时补精确映射或本地提交对象；路径缺口补显式 source-root 或还原构建产物；'
                  '候选超预算时缩小事件或调查范围。历史不足时由用户单独补齐，不自动 fetch。', '',
                  '明确拟修改版本后单独映射当前源码。对比明确提供的环境输入并补本地复现或独立验证，'
                  '本地通过不能证明线上已修复。baseline 的变化仅是线索。', '',
                  'case 默认 reviewed_for_secrets=false；人工审核全部共享证据后才进入既有评分流程。'
                  '敏感源码保留 local_only，无法安全精简共享证据时跳过评分。脱敏不是完整秘密审核。', ''])
    return '\n'.join(lines)


def prepare_incident(args):
    output = Path(args.output_dir)
    summary = {'schema_version': 1, 'status': 'error', 'diagnostics': [], 'candidate_count': 0, 'artifacts': None}
    try:
        if output.is_symlink():
            summary['diagnostics'] = ['output_invalid']
            return summary
        if output.exists():
            if not output.is_dir() or any(output.iterdir()):
                summary['diagnostics'] = ['output_not_empty']
                return summary
        else:
            output.mkdir(parents=True)
        evidence, case = build_evidence(args)
        names = ['evidence.json', 'report.md'] + (['case.json'] if case is not None else [])
        installed = []
        try:
            with tempfile.TemporaryDirectory(prefix='.prepare-', dir=output) as staging:
                stage = Path(staging)
                (stage / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2,
                                                               allow_nan=False) + '\n', encoding='utf-8')
                (stage / 'report.md').write_text(render_report(evidence), encoding='utf-8')
                if case is not None:
                    (stage / 'case.json').write_text(json.dumps(case, ensure_ascii=False, indent=2,
                                                               allow_nan=False) + '\n', encoding='utf-8')
                for name in names:
                    # Avoid replacing an artifact created by another process since the initial empty check.
                    if (output / name).exists():
                        raise OSError('output changed')
                    os.link(stage / name, output / name)
                    installed.append(output / name)
        except (OSError, ValueError, UnicodeError):
            for path in installed:
                path.unlink()
            summary['diagnostics'] = ['output_write_failed']
            return summary
        summary.update(status=evidence['status'], diagnostics=evidence['diagnostics'],
                       candidate_count=len(evidence['candidate_provenance']),
                       artifacts={name: sanitize_text(str(output / name)) for name in names})
    except OSError:
        summary['diagnostics'] = ['output_unavailable']
    return summary


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error can include an untrusted option value or path.
        self.exit(2, 'invalid_arguments\n')


def main():
    parser = SafeParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--release-map')
    parser.add_argument('--event-id')
    parser.add_argument('--source-root')
    parser.add_argument('--baseline-revision')
    report = prepare_incident(parser.parse_args())
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0 if report['status'] == 'ready' else 2


if __name__ == '__main__':
    sys.exit(main())
