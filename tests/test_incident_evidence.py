"""Synthetic, offline tests for production-events/v1 ingestion and selection."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "jev-bugfix" / "scripts" / "incident_evidence.py"
EVIDENCE = None
if SCRIPT.exists():
    SPEC = importlib.util.spec_from_file_location("incident_evidence", SCRIPT)
    EVIDENCE = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(EVIDENCE)


def event(identifier="event-1", **overrides):
    value = {
        "event_id": identifier,
        "timestamp": "2026-10-05T08:00:00Z",
        "service": "worker",
        "environment": "production",
        "release": "deploy-1",
        "commit": "a" * 40,
        "exception": {
            "type": "ValueError",
            "message": "Bad synthetic input",
            "frames": [
                {"path": "src/worker.py", "line": 3, "function": "run", "in_app": True},
                {"path": "src/parse.py", "line": 7, "function": "parse", "in_app": True},
            ],
        },
        "breadcrumbs": [],
        "trace": {"trace_id": "trace-1", "spans": []},
        "runtime": {"language": "python", "version": "3.9", "os": "linux", "arch": "x86_64"},
    }
    value.update(overrides)
    return value


class IncidentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(EVIDENCE, "production event adapter is not implemented")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.json"

    def load(self, events):
        return self.load_value({"schema_version": 1, "events": events})

    def load_value(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=True), encoding="utf-8")
        return EVIDENCE.load_events(self.path)

    def assert_invalid(self, value, code):
        with self.assertRaises(EVIDENCE.EvidenceError) as caught:
            self.load_value(value)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def test_missing_fields_preserve_unknown_values_and_input_hash(self):
        result = self.load([{"event_id": "minimal"}])
        selected = result["events"][0]
        self.assertEqual(result["adapter"], "production-events/v1")
        self.assertEqual(result["input_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertIsNone(selected["timestamp"])
        self.assertEqual(selected["exception"], {"type": None, "message": None, "frames": []})
        self.assertEqual(selected["breadcrumbs"], [])
        self.assertEqual(selected["trace"], {"trace_id": None, "span_id": None, "parent_span_id": None, "spans": []})
        self.assertEqual(selected["provenance"]["indices"], [0])
        self.assertEqual(selected["provenance"]["duplicate_count"], 0)
        self.assertIn("exception.frames", selected["provenance"]["missing_fields"])
        self.assertIn("runtime.language", selected["provenance"]["missing_fields"])

    def test_mixed_groups_need_explicit_selection(self):
        result = EVIDENCE.select_incident(self.load([
            event("one"), event("two", service="api"),
            event("three", environment="staging"), event("four", release="deploy-2"),
        ]))
        self.assertEqual(result["status"], "needs_input")
        self.assertIsNone(result["selected"])
        self.assertEqual(len(result["groups"]), 4)
        self.assertEqual(result["related_events"], [])
        self.assertIn("multiple_incident_groups", result["diagnostics"])

    def test_group_uses_innermost_application_frame_shape(self):
        other = event("two")
        other["exception"]["frames"][-1]["function"] = "parse_other"
        external = event("three")
        external["exception"]["frames"].append({"path": "lib/external.py", "in_app": False})
        selected = EVIDENCE.select_incident(self.load([event("one"), other, external]), "one")
        self.assertEqual(len(selected["groups"]), 2)
        self.assertEqual(selected["groups"][0]["event_ids"], ["one", "three"])
        self.assertEqual(selected["groups"][0]["key"]["frame_path"], "src/parse.py")
        self.assertEqual(selected["groups"][0]["key"]["frame_function"], "parse")

    def test_missing_group_values_are_not_wildcards(self):
        result = EVIDENCE.select_incident(self.load([event("one"), event("two", service=None)]))
        self.assertEqual(len(result["groups"]), 2)
        self.assertIn("service", result["groups"][1]["missing_fields"])

    def test_representative_is_earliest_instant_then_original_input_order(self):
        result = EVIDENCE.select_incident(self.load([
            event("no-time", timestamp=None),
            event("later", timestamp="2026-10-05T08:00:01Z"),
            event("first-tie", timestamp="2026-10-05T16:00:00+08:00"),
            event("second-tie", timestamp="2026-10-05T08:00:00Z"),
        ]))
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected"]["event_id"], "first-tie")
        self.assertEqual(result["selected"]["exception"]["frames"][1]["line"], 7)

    def test_all_missing_times_use_input_order(self):
        result = EVIDENCE.select_incident(self.load([event("first", timestamp=None), event("second", timestamp=None)]))
        self.assertEqual(result["selected"]["event_id"], "first")
        self.assertIn("selected_timestamp_missing", result["diagnostics"])

    def test_submicrosecond_timestamp_precision_controls_representative(self):
        for later, earlier in [(".0000002", ".0000001"), (".2", ".1"), (".0002", ".0001")]:
            with self.subTest(fraction=earlier):
                times = ["2026-10-05T08:00:00" + fraction + "Z" for fraction in (later, earlier)]
                normalized = self.load([event("later", timestamp=times[0]),
                                        event("earlier", timestamp=times[1])])
                self.assertEqual([item["timestamp"] for item in normalized["events"]], times)
                result = EVIDENCE.select_incident(normalized)
                self.assertEqual(result["selected"]["event_id"], "earlier")

    def test_explicit_event_id_is_exact_and_unknown_id_needs_input(self):
        normalized = self.load([event("first"), event("second", service="api")])
        self.assertEqual(EVIDENCE.select_incident(normalized, "second")["selected"]["event_id"], "second")
        missing = EVIDENCE.select_incident(normalized, "second ")
        self.assertEqual(missing["status"], "needs_input")
        self.assertIn("event_id_not_found", missing["diagnostics"])

    def test_empty_events_need_input(self):
        result = EVIDENCE.select_incident(self.load([]))
        self.assertEqual(result["status"], "needs_input")
        self.assertIn("no_events", result["diagnostics"])

    def test_exception_content_is_preferred_to_plain_event_in_same_unknown_shape(self):
        result = EVIDENCE.select_incident(self.load([
            event("plain", exception={}),
            event("exception", timestamp="2026-10-05T08:00:01Z", exception={"message": "Synthetic failure"}),
        ]))
        self.assertEqual(result["selected"]["event_id"], "exception")

    def test_plain_event_is_retained_with_explicit_missing_exception_diagnostic(self):
        result = EVIDENCE.select_incident(self.load([{"event_id": "plain"}]))
        self.assertEqual(result["selected"]["event_id"], "plain")
        self.assertIn("exception_evidence_missing", result["diagnostics"])

    def test_identical_duplicate_ids_merge_and_preserve_each_index(self):
        original = event()
        normalized = self.load([original, copy.deepcopy(original)])
        self.assertEqual(len(normalized["events"]), 1)
        self.assertEqual(normalized["events"][0]["provenance"]["indices"], [0, 1])
        self.assertEqual(normalized["events"][0]["provenance"]["duplicate_count"], 1)
        self.assertIn("identical_events_merged", normalized["diagnostics"])

    def test_conflicting_duplicate_id_rejected(self):
        self.assert_invalid({"schema_version": 1, "events": [event(), event(release="deploy-2")]}, "conflicting_event_id")

    def test_omission_and_null_are_not_completely_identical_duplicates(self):
        self.assert_invalid({"schema_version": 1, "events": [{"event_id": "one"}, {"event_id": "one", "service": None}]}, "conflicting_event_id")

    def test_trace_association_requires_same_group_and_same_nonempty_trace(self):
        selected = EVIDENCE.select_incident(self.load([
            event("selected"), event("related"),
            event("different-trace", trace={"trace_id": "trace-2"}),
            event("different-group", service="api"),
            event("no-trace", trace={}),
        ]), "selected")
        self.assertEqual([item["event_id"] for item in selected["related_events"]], ["related"])
        for trace in ({}, {"trace_id": None}, {"trace_id": ""}):
            result = EVIDENCE.select_incident(self.load([event("one", trace=trace), event("two", trace=trace)]), "one")
            self.assertEqual(result["related_events"], [])
            self.assertIn("selected_trace_missing", result["diagnostics"])

    def test_association_is_only_candidate_and_preserves_conflicting_commits_for_resolver(self):
        result = EVIDENCE.select_incident(self.load([event("one"), event("two", commit="b" * 40)]), "one")
        self.assertEqual(result["related_events"][0]["commit"], "b" * 40)
        self.assertEqual(result["selected"]["commit"], "a" * 40)

    def test_unknown_fields_rejected_at_every_level(self):
        values = []
        root = {"schema_version": 1, "events": [], "user": {}}
        values.append(root)
        for location in ("event", "exception", "frame", "breadcrumb", "trace", "span", "runtime"):
            item = event()
            targets = {
                "event": item, "exception": item["exception"],
                "frame": item["exception"]["frames"][0], "trace": item["trace"],
                "runtime": item["runtime"],
            }
            item["breadcrumbs"] = [{}]
            item["trace"]["spans"] = [{}]
            targets.update(breadcrumb=item["breadcrumbs"][0], span=item["trace"]["spans"][0])
            targets[location]["authorization"] = "UNTRUSTED_SECRET"
            values.append({"schema_version": 1, "events": [item]})
        for value in values:
            with self.subTest(value=value):
                self.assert_invalid(value, "input_unknown_field")

    def test_duplicate_json_keys_rejected(self):
        self.path.write_text('{"schema_version":1,"events":[{"event_id":"one","event_id":"two"}]}', encoding="utf-8")
        with self.assertRaises(EVIDENCE.EvidenceError) as caught:
            EVIDENCE.load_events(self.path)
        self.assertEqual(caught.exception.code, "input_duplicate_key")

    def test_nonfinite_numbers_rejected_including_float_overflow(self):
        for token in ("NaN", "Infinity", "-Infinity", "1e9999"):
            with self.subTest(token=token):
                self.path.write_text('{"schema_version":1,"events":[{"event_id":"one","commit":' + token + '}]}', encoding="utf-8")
                with self.assertRaises(EVIDENCE.EvidenceError) as caught:
                    EVIDENCE.load_events(self.path)
                self.assertEqual(caught.exception.code, "input_non_finite")

    def test_invalid_utf8_and_escaped_surrogates_rejected(self):
        for raw in (b'\xff', b'{"schema_version":1,"events":[{"event_id":"\\ud800"}]}', b'{"schema_version":1,"events":[{"event_id":"\\udfff"}]}'):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                with self.assertRaises(EVIDENCE.EvidenceError) as caught:
                    EVIDENCE.load_events(self.path)
                self.assertEqual(caught.exception.code, "input_encoding_invalid")

    def test_valid_unicode_pair_is_accepted(self):
        result = self.load([{"event_id": "synthetic-\U0001f600"}])
        self.assertEqual(result["events"][0]["event_id"], "synthetic-\U0001f600")

    def test_file_limit_checked_before_json_parse(self):
        self.path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(EVIDENCE.EvidenceError) as caught:
            EVIDENCE.load_events(self.path)
        self.assertEqual(caught.exception.code, "input_file_over_limit")

    def test_missing_file_has_safe_error(self):
        with self.assertRaises(EVIDENCE.EvidenceError) as caught:
            EVIDENCE.load_events(Path(self.temp.name) / "password=SECRET_NOT_PRINTED.json")
        self.assertEqual(str(caught.exception), "input_io_error")

    def test_event_limit_applies_before_deduplication(self):
        self.assert_invalid({"schema_version": 1, "events": [event()] * 201}, "events_over_limit")

    def test_collection_limits_checked_before_processing_entry_text(self):
        self.assert_invalid({"schema_version": 1, "events": [{"event_id": "\ud800"}] * 201}, "events_over_limit")
        item = event()
        item["exception"]["frames"] = [{"path": "\ud800"}] * 65
        self.assert_invalid({"schema_version": 1, "events": [item]}, "frames_over_limit")

    def test_collection_limits_reject_without_truncation(self):
        for field, maximum, code in (("frames", 64, "frames_over_limit"), ("breadcrumbs", 100, "breadcrumbs_over_limit"), ("spans", 100, "spans_over_limit")):
            with self.subTest(field=field):
                item = event()
                parent = item["exception"] if field == "frames" else item["trace"] if field == "spans" else item
                parent[field] = [{}] * (maximum + 1)
                self.assert_invalid({"schema_version": 1, "events": [item]}, code)

    def test_text_limit_counts_utf8_bytes(self):
        self.load([event(release="界" * 1365 + "a")])
        self.assert_invalid({"schema_version": 1, "events": [event(release="界" * 1366)]}, "text_over_limit")

    def test_depth_limit_is_enforced_before_json_parser(self):
        self.path.write_text("[" * 33 + "0" + "]" * 33, encoding="utf-8")
        with self.assertRaises(EVIDENCE.EvidenceError) as caught:
            EVIDENCE.load_events(self.path)
        self.assertEqual(caught.exception.code, "input_depth_over_limit")

    def test_depth_scanner_does_not_count_brackets_inside_strings(self):
        result = self.load([event(release="[" * 100 + '\\"}' * 100)])
        self.assertEqual(len(result["events"]), 1)

    def test_root_schema_and_required_event_id_are_strict(self):
        for value in ([], {}, {"schema_version": True, "events": []}, {"schema_version": 2, "events": []}, {"schema_version": 1, "events": {}}, {"schema_version": 1, "events": [{}]}, {"schema_version": 1, "events": [{"event_id": ""}]}, {"schema_version": 1, "events": [{"event_id": None}]}):
            with self.subTest(value=value):
                with self.assertRaises(EVIDENCE.EvidenceError):
                    self.load_value(value)

    def test_wrong_types_rejected_at_each_nested_field(self):
        modifications = [
            ("service", 3), ("exception", []), ("breadcrumbs", "text"),
            ("trace", False), ("runtime", "python"),
        ]
        for field, value in modifications:
            with self.subTest(field=field):
                self.assert_invalid({"schema_version": 1, "events": [event(**{field: value})]}, "input_type_invalid")
        for frame in ({"line": True}, {"line": 0}, {"line": -1}, {"line": 1.5}, {"in_app": 1}, {"path": []}):
            with self.subTest(frame=frame):
                self.assert_invalid({"schema_version": 1, "events": [event(exception={"frames": [frame]})]}, "input_type_invalid")

    def test_naive_or_malformed_timestamps_rejected_in_all_locations(self):
        for location in ("event", "breadcrumb", "span_start", "span_end"):
            for timestamp in ("2026-10-05T08:00:00", "yesterday", "2026-02-30T08:00:00Z", "2026-10-05 08:00:00Z"):
                with self.subTest(location=location, timestamp=timestamp):
                    item = event()
                    if location == "event":
                        item["timestamp"] = timestamp
                    elif location == "breadcrumb":
                        item["breadcrumbs"] = [{"timestamp": timestamp}]
                    else:
                        item["trace"]["spans"] = [{"start_timestamp" if location == "span_start" else "end_timestamp": timestamp}]
                    self.assert_invalid({"schema_version": 1, "events": [item]}, "timestamp_invalid")

    def test_full_fixture_loads_without_unknown_keys(self):
        fixture = ROOT / "tests" / "fixtures" / "incidents" / "production-events.json"
        self.assertTrue(fixture.exists(), "complete synthetic fixture is missing")
        result = EVIDENCE.load_events(fixture)
        self.assertGreaterEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][0]["provenance"]["missing_fields"], [])

    def test_sensitive_text_is_redacted_and_raw_normalization_stays_exact(self):
        markers = [
            "MARKER_PASSWORD", "MARKER_TOKEN", "MARKER_HEADER", "private.person@example.test",
            "+86 13800138000", "MARKER_HOME", "MARKER_PATH", "MARKER_COOKIE",
        ]
        text = ('password=MARKER_PASSWORD token: MARKER_TOKEN\nAuthorization: Bearer MARKER_HEADER\n'
                'email private.person@example.test phone +86 13800138000\n'
                '/Users/MARKER_HOME/app/src.py /srv/app/token=MARKER_PATH/worker.py\nCookie: session=MARKER_COOKIE')
        normalized = self.load([event(release=text)])
        self.assertEqual(normalized["events"][0]["release"], text)
        sanitized = EVIDENCE.sanitize_value(normalized)
        serialized = json.dumps(sanitized)
        for marker in markers:
            self.assertNotIn(marker, serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertEqual(normalized["events"][0]["release"], text)

    def test_provider_keys_jwt_private_keys_and_url_credentials_redacted(self):
        for text in ('sk-' + 'a' * 24, 'ghp_' + 'b' * 28, 'AKIA' + 'C' * 16, 'eyJab.eyJcd.secret', '-----BEGIN PRIVATE KEY-----\nMARKER_BODY\n-----END PRIVATE KEY-----', 'https://person:MARKER_PASSWORD@host.test/path'):
            with self.subTest(text=text):
                sanitized = EVIDENCE.sanitize_text(text)
                self.assertNotIn(text, sanitized)
                self.assertNotIn("MARKER_BODY", sanitized)
                self.assertNotIn("MARKER_PASSWORD", sanitized)

    def test_ip_addresses_redacted_but_runtime_versions_preserved(self):
        sanitized = EVIDENCE.sanitize_text("source 192.168.10.12 [2001:db8::1] loopback ::1 runtime 3.9.20")
        for address in ("192.168.10.12", "2001:db8::1", "::1"):
            self.assertNotIn(address, sanitized)
        self.assertIn("3.9.20", sanitized)

    def test_ipv4_mapped_ipv6_is_redacted_as_one_identifier(self):
        self.assertEqual(EVIDENCE.sanitize_text("source ::ffff:192.0.2.1"), "source [REDACTED]")

    def test_personal_phone_identifiers_and_synthetic_secret_assignments_redacted(self):
        text = "SECRET = MARKER_ONE\nAPI_KEY: MARKER_TWO\nphone +1 (415) 555-2671\n电话 13800138000"
        sanitized = EVIDENCE.sanitize_text(text)
        for marker in ("MARKER_ONE", "MARKER_TWO", "+1 (415) 555-2671", "13800138000"):
            self.assertNotIn(marker, sanitized)

    def test_sanitize_value_redacts_sensitive_dictionary_keys_and_path_segments(self):
        safe = EVIDENCE.sanitize_value({"MARKER_USER@example.test": ["C:\\Users\\MARKER_WINDOWS\\src.py", "/home/MARKER_LINUX/app.py", "/srv/.env.production", "id_rsa"], "count": 3})
        rendered = json.dumps(safe)
        for marker in ("MARKER_USER", "MARKER_WINDOWS", "MARKER_LINUX", ".env.production", "id_rsa"):
            self.assertNotIn(marker, rendered)
        self.assertEqual(safe["count"], 3)

    def test_markdown_escape_blocks_html_and_resource_embeddings(self):
        malicious = '<script>run()</script> ![image](https://tracker.test/x) [link](file:///secret) `shell`'
        escaped = EVIDENCE.escape_markdown(malicious)
        self.assertNotIn("<script>", escaped)
        self.assertNotIn("![image](", escaped)
        self.assertNotIn("[link](", escaped)
        self.assertNotIn("`shell`", escaped)
        self.assertIn("&lt;script&gt;", escaped)


if __name__ == "__main__":
    unittest.main()
