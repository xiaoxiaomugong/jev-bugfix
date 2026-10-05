#!/usr/bin/env python3
"""Bounded local production-events/v1 adapter; never invokes Git or a scorer.

Normalized values remain exact in memory for grouping and release resolution.
Call sanitize_value before deriving a report, and escape_markdown for prose.
"""

from datetime import datetime, timezone
import hashlib
import html
import ipaddress
import json
import math
from pathlib import Path
import re


ADAPTER = "production-events/v1"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 200
MAX_FRAMES = 64
MAX_BREADCRUMBS = 100
MAX_SPANS = 100
MAX_TEXT_BYTES = 4096
MAX_DEPTH = 32

EVENT_FIELDS = ("event_id", "timestamp", "service", "environment", "release", "commit",
                "exception", "breadcrumbs", "trace", "runtime")
EXCEPTION_FIELDS = ("type", "message", "frames")
FRAME_FIELDS = ("path", "line", "function", "in_app")
BREADCRUMB_FIELDS = ("timestamp", "category", "level", "message")
TRACE_FIELDS = ("trace_id", "span_id", "parent_span_id", "spans")
SPAN_FIELDS = ("span_id", "parent_span_id", "op", "status", "start_timestamp", "end_timestamp")
RUNTIME_FIELDS = ("language", "version", "os", "arch")
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z")


class EvidenceError(ValueError):
    """Only a fixed code is exposed; input text and OS errors stay private."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _depth_check(text):
    depth = 0
    quoted = False
    escaped = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise EvidenceError("input_depth_over_limit")
        elif character in "]}":
            depth -= 1


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("input_duplicate_key")
        result[key] = value
    return result


def _nonfinite(_token):
    raise EvidenceError("input_non_finite")


def _finite_float(token):
    value = float(token)
    if not math.isfinite(value):
        raise EvidenceError("input_non_finite")
    return value


def _check_strings(value):
    # Also inspect dictionary keys, even when a schema check will reject them.
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            raise EvidenceError("input_encoding_invalid") from None
        if len(encoded) > MAX_TEXT_BYTES:
            raise EvidenceError("text_over_limit")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_strings(key)
            _check_strings(item)
    elif isinstance(value, list):
        for item in value:
            _check_strings(item)


def _fields(value, allowed, optional=False):
    if value is None and optional:
        return {}
    if type(value) is not dict:
        raise EvidenceError("input_type_invalid")
    if set(value) - set(allowed):
        raise EvidenceError("input_unknown_field")
    return value


def parse_timestamp(value):
    if value is None:
        return None
    if not TIMESTAMP_RE.fullmatch(value):
        raise EvidenceError("timestamp_invalid")
    try:
        # Python 3.9 accepts only 3/6 fraction digits. Parse a bounded
        # microsecond projection; retain the original text for exact ordering.
        compatible = re.sub(r"\.(\d+)",
                            lambda match: "." + match.group(1)[:6].ljust(6, "0"), value, count=1)
        instant = datetime.fromisoformat(compatible[:-1] + "+00:00"
                                         if compatible.endswith("Z") else compatible)
        # UTC conversion can overflow for otherwise valid year 0001/9999 offsets.
        instant = instant.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise EvidenceError("timestamp_invalid") from None
    return instant


def _scalar(source, key, missing, prefix="", kind="text"):
    value = source.get(key)
    field = prefix + key
    if value is None:
        missing.append(field)
        return None
    if kind == "line":
        if type(value) is not int or value < 1:
            raise EvidenceError("input_type_invalid")
    elif kind == "bool":
        if type(value) is not bool:
            raise EvidenceError("input_type_invalid")
    else:
        if type(value) is not str:
            raise EvidenceError("input_type_invalid")
        if kind == "timestamp":
            parse_timestamp(value)
        if not value:
            missing.append(field)
    return value


def _list(source, key, maximum, code, missing, prefix=""):
    value = source.get(key, [])
    if value is None:
        value = []
    if type(value) is not list:
        raise EvidenceError("input_type_invalid")
    if len(value) > maximum:
        raise EvidenceError(code)
    if not value:
        missing.append(prefix + key)
    return value


def _normalize_event(raw, index):
    raw = _fields(raw, EVENT_FIELDS)
    identifier = raw.get("event_id")
    if type(identifier) is not str or not identifier:
        raise EvidenceError("event_id_invalid")
    missing = []
    result = {"event_id": identifier}
    for key in ("timestamp", "service", "environment", "release", "commit"):
        result[key] = _scalar(raw, key, missing, kind="timestamp" if key == "timestamp" else "text")
    exception = _fields(raw.get("exception"), EXCEPTION_FIELDS, optional=True)
    result["exception"] = {
        key: _scalar(exception, key, missing, "exception.") for key in ("type", "message")
    }
    frames = _list(exception, "frames", MAX_FRAMES, "frames_over_limit", missing, "exception.")
    result["exception"]["frames"] = []
    for frame_index, frame in enumerate(frames):
        frame = _fields(frame, FRAME_FIELDS)
        prefix = "exception.frames[%d]." % frame_index
        result["exception"]["frames"].append({
            key: _scalar(frame, key, missing, prefix, {"line": "line", "in_app": "bool"}.get(key, "text"))
            for key in FRAME_FIELDS
        })
    breadcrumbs = _list(raw, "breadcrumbs", MAX_BREADCRUMBS, "breadcrumbs_over_limit", missing)
    result["breadcrumbs"] = []
    for breadcrumb_index, breadcrumb in enumerate(breadcrumbs):
        breadcrumb = _fields(breadcrumb, BREADCRUMB_FIELDS)
        prefix = "breadcrumbs[%d]." % breadcrumb_index
        result["breadcrumbs"].append({
            key: _scalar(breadcrumb, key, missing, prefix, "timestamp" if key == "timestamp" else "text")
            for key in BREADCRUMB_FIELDS
        })
    trace = _fields(raw.get("trace"), TRACE_FIELDS, optional=True)
    result["trace"] = {key: _scalar(trace, key, missing, "trace.") for key in TRACE_FIELDS if key != "spans"}
    spans = _list(trace, "spans", MAX_SPANS, "spans_over_limit", missing, "trace.")
    result["trace"]["spans"] = []
    for span_index, span in enumerate(spans):
        span = _fields(span, SPAN_FIELDS)
        prefix = "trace.spans[%d]." % span_index
        result["trace"]["spans"].append({
            key: _scalar(span, key, missing, prefix, "timestamp" if key.endswith("timestamp") else "text")
            for key in SPAN_FIELDS
        })
    runtime = _fields(raw.get("runtime"), RUNTIME_FIELDS, optional=True)
    result["runtime"] = {key: _scalar(runtime, key, missing, "runtime.") for key in RUNTIME_FIELDS}
    result["provenance"] = {"indices": [index], "duplicate_count": 0, "missing_fields": missing}
    return result


def _preflight_collections(raw):
    """Check list counts before any entry text is traversed or normalized."""
    raw = _fields(raw, EVENT_FIELDS)
    exception = _fields(raw.get("exception"), EXCEPTION_FIELDS, optional=True)
    trace = _fields(raw.get("trace"), TRACE_FIELDS, optional=True)
    _list(exception, "frames", MAX_FRAMES, "frames_over_limit", [])
    _list(raw, "breadcrumbs", MAX_BREADCRUMBS, "breadcrumbs_over_limit", [])
    _list(trace, "spans", MAX_SPANS, "spans_over_limit", [])


def load_events(input_path):
    """Load only the documented UTF-8 JSON format, keeping exact values local."""
    try:
        with Path(input_path).open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except (OSError, ValueError, TypeError):
        raise EvidenceError("input_io_error") from None
    if len(raw) > MAX_FILE_BYTES:
        raise EvidenceError("input_file_over_limit")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise EvidenceError("input_encoding_invalid") from None
    _depth_check(text)
    try:
        value = json.loads(text, object_pairs_hook=_object_pairs,
                           parse_constant=_nonfinite, parse_float=_finite_float)
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        if isinstance(error, EvidenceError):
            raise
        raise EvidenceError("input_json_invalid") from None
    value = _fields(value, ("schema_version", "events"))
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise EvidenceError("input_schema_invalid")
    events = value.get("events")
    if type(events) is not list:
        raise EvidenceError("input_type_invalid")
    if len(events) > MAX_EVENTS:
        raise EvidenceError("events_over_limit")
    for raw_event in events:
        _preflight_collections(raw_event)
    _check_strings(value)
    result = {"schema_version": 1, "adapter": ADAPTER, "input_sha256": hashlib.sha256(raw).hexdigest(),
              "events": [], "diagnostics": []}
    seen = {}
    for index, raw_event in enumerate(events):
        normalized = _normalize_event(raw_event, index)
        # Compare parsed input, not defaults: omitted/null are not identical.
        fingerprint = json.dumps(raw_event, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        identifier = normalized["event_id"]
        if identifier in seen:
            previous, original_fingerprint = seen[identifier]
            if fingerprint != original_fingerprint:
                raise EvidenceError("conflicting_event_id")
            previous["provenance"]["indices"].append(index)
            previous["provenance"]["duplicate_count"] += 1
            if "identical_events_merged" not in result["diagnostics"]:
                result["diagnostics"].append("identical_events_merged")
        else:
            seen[identifier] = (normalized, fingerprint)
            result["events"].append(normalized)
    return result


def _group_key(event):
    application_frames = [frame for frame in event["exception"]["frames"] if frame["in_app"] is True]
    frame = application_frames[-1] if application_frames else {"path": None, "function": None}
    return {"service": event["service"], "environment": event["environment"], "release": event["release"],
            "exception_type": event["exception"]["type"], "frame_path": frame["path"], "frame_function": frame["function"]}


def _event_order(event):
    timestamp = parse_timestamp(event["timestamp"])
    # datetime truncates after microseconds; compare the full decimal fraction
    # separately so nanosecond (or finer) observations retain their order.
    fraction = re.search(r"\.(\d+)", event["timestamp"]) if timestamp is not None else None
    fraction = fraction.group(1).rstrip("0") if fraction else ""
    instant = timestamp.replace(microsecond=0) if timestamp is not None else datetime.max.replace(tzinfo=timezone.utc)
    return (timestamp is None, instant, fraction, event["provenance"]["indices"][0])


def select_incident(normalized, event_id=None):
    """Select a representative and trace candidates; no causal/version claims."""
    events = normalized["events"]
    result = {"selected": None, "groups": [], "related_events": [],
              "diagnostics": list(normalized.get("diagnostics", [])), "status": "needs_input"}
    grouped = {}
    for event in events:
        key = _group_key(event)
        signature = tuple(key.values())
        if signature not in grouped:
            group = {"group_id": "group-%03d" % (len(grouped) + 1), "key": key, "event_ids": [],
                     "event_count": 0, "missing_fields": [field for field, value in key.items() if not value]}
            grouped[signature] = group
            result["groups"].append(group)
        grouped[signature]["event_ids"].append(event["event_id"])
        grouped[signature]["event_count"] += 1
    if event_id is not None:
        selected = next((event for event in events if event["event_id"] == event_id), None)
        if selected is None:
            result["diagnostics"].append("event_id_not_found")
            return result
    elif not events:
        result["diagnostics"].append("no_events")
        return result
    elif len(grouped) != 1:
        result["diagnostics"].append("multiple_incident_groups")
        return result
    else:
        exceptions = [event for event in events if any(event["exception"].values())]
        selected = min(exceptions or events, key=_event_order)
    result.update(selected=selected, status="selected")
    if not any(selected["exception"].values()):
        result["diagnostics"].append("exception_evidence_missing")
    if selected["timestamp"] is None:
        result["diagnostics"].append("selected_timestamp_missing")
    trace_id = selected["trace"]["trace_id"]
    if not trace_id:
        result["diagnostics"].append("selected_trace_missing")
    else:
        key = _group_key(selected)
        result["related_events"] = sorted([
            event for event in events if event is not selected and _group_key(event) == key
            and event["trace"]["trace_id"] == trace_id
        ], key=_event_order)
    return result


# Conservative local display redaction, not a substitute for secret review.
REDACTED = "[REDACTED]"
REDACTION_PATTERNS = [
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|$)",
    r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|x-auth-token|headers?)\s*[:=][^\r\n]*",
    r"(?i)\b[a-z0-9_-]*(?:api[_-]?key|password|passwd|secret|token|private[_-]?key|credential)[a-z0-9_-]*[\"']?\s*[:=][^\r\n]*",
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b",
    r"(?i)\bbearer\s+[A-Za-z0-9_.~+/-]+",
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@",
    r"(?i)\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)",
    r"(?i)(?<=/)(?:users|home)/[^/\s]+",
    r"(?i)\b[A-Z]:[\\/]Users[\\/][^\\/\s]+",
    r"(?i)(?<![\w.])(?:\.env(?:\.[^/\\\s]*)?|\.aws|\.ssh|\.git|credentials|id_(?:rsa|dsa|ecdsa|ed25519))(?![\w])",
    r"(?i)[^/\\\s]+\.(?:key|pem|p12|pfx|keystore)\b",
]
REDACTION_RE = [re.compile(pattern) for pattern in REDACTION_PATTERNS]
IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,}[0-9A-Fa-f:.]{0,39}(?![\w:])")


def _redact_ip(match):
    try:
        ipaddress.ip_address(match.group(0))
    except ValueError:
        return match.group(0)
    return REDACTED


def sanitize_text(text):
    """Redact obvious credentials, personal identifiers and sensitive paths."""
    for pattern in REDACTION_RE:
        text = pattern.sub(REDACTED, text)
    text = IPV6_RE.sub(_redact_ip, text)
    text = IPV4_RE.sub(_redact_ip, text)
    # Hide terminal controls and Unicode direction overrides in local displays.
    return "".join(character if character in "\n\t" or
                   (ord(character) >= 32 and ord(character) != 127 and
                    not 0x80 <= ord(character) <= 0x9F and
                    ord(character) not in (0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069))
                   else REDACTED for character in text)


def sanitize_value(value):
    """Build a redacted copy without changing values used for exact mapping."""
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {sanitize_text(key): sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    return value


def escape_markdown(text):
    """Render untrusted event text literally, disabling HTML/links/resources."""
    escaped = html.escape(text, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", escaped)
