#!/usr/bin/env python3
"""Explicit offline contract check for an installed Jev 0.3.2 Python CLI.

Run: python3 tests/verify_local_cli.py [--jev /path/to/jev]
This file is excluded from default unittest discovery. It imports the resolved
CLI source, runs its real parser, line renderer and retry code, and substitutes
only credentials, configuration and transport. No subprocess or API is used.
The responses are synthetic; this does not verify the live API's response data.
"""

import argparse
import contextlib
import copy
import io
import json
from pathlib import Path
import shutil
import socket
import types
import unittest
from unittest.mock import patch


QUESTION = "Offline contract probe"
ARGV = ["score", QUESTION, "--range", "0-4", "--lines", "--json",
        "--jobs", "2", "--retries", "0", "--timeout", "10"]
ANSWER = {
    "type": "score", "score": 2.7,
    "legend": {"0": "0", "1": "1", "2": "2", "3": "3", "4": "4"},
    "probabilities": {"0": 0.05, "1": 0.1, "2": 0.15, "3": 0.5, "4": 0.2},
    "confidence": 0.8,
}


def forbidden(*args, **kwargs):
    raise AssertionError("Offline probe attempted real I/O or unexpected retry sleep")


def load_cli(executable):
    located = shutil.which(executable)
    if located is None:
        raise ValueError("Jev executable was not found; pass --jev /path/to/jev")
    path = Path(located).resolve(strict=True)
    source = path.read_text(encoding="utf-8")
    cli = types.ModuleType("jev_offline_contract")
    cli.__file__ = str(path)
    # Global open resolves to this guard inside the CLI; imports remain usable.
    cli.open = forbidden
    exec(compile(source, str(path), "exec"), cli.__dict__)
    if getattr(cli, "VERSION", None) != "0.3.2":
        raise ValueError("This offline contract check supports only Jev 0.3.2")
    return cli, path


def suite_for(cli):
    class LocalCLIContract(unittest.TestCase):
        def setUp(self):
            guards = contextlib.ExitStack()
            self.addCleanup(guards.close)
            # Replace the module's os reference instead of mutating os.environ.
            # Actual provider/model resolution sees an empty, private mapping.
            private_os = types.SimpleNamespace(environ={}, path=cli.os.path)
            replacements = {
                "os": private_os,
                "open": forbidden,
                "parse_env_file": forbidden,
                "read_pinned_provider": lambda: None,
                "default_env_path": lambda: "/offline/jev/.env",
                "locate_api_key": lambda provider: ("OFFLINE_PLACEHOLDER", "probe", None),
                "http_post": forbidden,
                "http_get": forbidden,
            }
            for name, value in replacements.items():
                guards.enter_context(patch.object(cli, name, value))
            # Transport guards also cover accidental bypasses of http_post.
            for name in ("socket", "create_connection", "getaddrinfo"):
                guards.enter_context(patch.object(socket, name, forbidden))
            guards.enter_context(patch.object(cli.time, "sleep", forbidden))

        def run_main(self, text, argv=ARGV):
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(cli.sys, "stdin", io.StringIO(text)), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(list(argv))
            return code, stdout.getvalue(), stderr.getvalue()

        def response(self, ctx, state, questions):
            self.assertEqual(questions, {"answer": {
                "type": "score", "instructions": QUESTION,
                "criteria": ["0", "1", "2", "3", "4"],
            }})
            self.assertEqual(ctx.retries, 0)
            self.assertEqual(ctx.timeout, 10)
            return {"answers": {"answer": copy.deepcopy(ANSWER)}, "usage": {}}

        def test_success_uses_object_echo_and_real_score_enrichment(self):
            state = {"candidate": {"id": "ok"}}
            with patch.object(cli, "call_api", self.response):
                code, stdout, stderr = self.run_main(json.dumps(state) + "\n")
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            rows = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(set(rows[0]), {"input", "answer"})
            self.assertEqual(rows[0]["input"], state)
            expected = dict(ANSWER, label="3", value=2.7)
            self.assertEqual(rows[0]["answer"], expected)

        def test_item_error_echoes_raw_line_and_keeps_other_results(self):
            good = '{"candidate":{"id":"ok"}}'
            bad = '  {"candidate": {"id": "bad"}}  '

            def response(ctx, state, questions):
                if state["candidate"]["id"] == "bad":
                    raise cli.JevError("jev: API 错误 (HTTP 429): offline fixture", 2)
                return self.response(ctx, state, questions)

            with patch.object(cli, "call_api", response):
                code, stdout, stderr = self.run_main(good + "\n\n" + bad + "\n")
            self.assertEqual(code, 2)
            rows = [json.loads(line) for line in stdout.splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["input"], json.loads(good))
            self.assertEqual(rows[1], {
                "input": bad, "error": "jev: API 错误 (HTTP 429): offline fixture",
            })
            self.assertIn("429", stderr)

        def test_missing_credentials_returns_no_result_rows(self):
            for flags, marker in (([], "API key"),
                                  (["--provider", "typesafe"], "TYPESAFE_API_KEY")):
                with self.subTest(provider=flags):
                    with patch.object(cli, "locate_api_key", lambda provider: (None, None, None)):
                        code, stdout, stderr = self.run_main(
                            '{"candidate":{"id":"ok"}}\n', ARGV + flags)
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertIn(marker, stderr)

        def test_retries_zero_makes_one_outer_transport_call(self):
            ctx = cli.Ctx(cli.build_parser().parse_args(ARGV))
            for failure in (429, 500, "network"):
                with self.subTest(failure=failure):
                    calls = []

                    def response(*args, **kwargs):
                        calls.append(1)
                        if failure == "network":
                            raise cli.JevNetworkError("offline fixture")
                        return failure, "{}", {}

                    with patch.object(cli, "http_post", response):
                        with self.assertRaises(cli.JevError):
                            cli.call_api_payload(ctx, {})
                    self.assertEqual(len(calls), 1)

        def test_stale_reused_connection_allows_at_most_one_extra_post(self):
            class Response:
                headers = {}
                status = 200
                version = 11

                def read(self):
                    return b"{}"

            class Connection:
                def __init__(self, stale=False):
                    self.stale = stale
                    self.attempts = 0

                def request(self, method, path, **kwargs):
                    self.attempts += 1
                    if self.stale:
                        raise cli.http.client.RemoteDisconnected("offline fixture")

                def getresponse(self):
                    return Response()

                def close(self):
                    pass

            ctx = cli.Ctx(cli.build_parser().parse_args(ARGV))
            ctx.base_url = "https://offline.invalid/"
            for reused, replacement_fails, expected in (
                    (False, True, 1), (True, False, 2), (True, True, 2)):
                with self.subTest(reused=reused, replacement_fails=replacement_fails):
                    initial = Connection(stale=True)
                    replacement = Connection(stale=replacement_fails)
                    pool = {("https", "offline.invalid", 443, None): initial} if reused else {}
                    fresh = replacement if reused else initial
                    with patch.object(cli, "http_post", real_http_post), \
                            patch.object(cli, "resolve_proxy", lambda *args: None), \
                            patch.object(cli, "_conn_pool", lambda: pool), \
                            patch.object(cli, "_new_connection", lambda *args: fresh):
                        if reused and not replacement_fails:
                            self.assertEqual(cli.call_api_payload(ctx, {}), {})
                        else:
                            with self.assertRaises(cli.JevError):
                                cli.call_api_payload(ctx, {})
                    self.assertEqual(initial.attempts + replacement.attempts, expected)
                    self.assertLessEqual(initial.attempts + replacement.attempts, 2)

    real_http_post = cli.http_post
    return unittest.defaultTestLoader.loadTestsFromTestCase(LocalCLIContract)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jev", default="jev", help="Installed Jev executable (default: PATH)")
    args = parser.parse_args()
    try:
        # Import-time transport guards complement per-test transport/config guards.
        with contextlib.ExitStack() as guards:
            for name in ("socket", "create_connection", "getaddrinfo"):
                guards.enter_context(patch.object(socket, name, forbidden))
            cli, path = load_cli(args.jev)
    except (OSError, ValueError, SyntaxError, AssertionError) as error:
        print("Offline CLI verification unavailable: " + str(error))
        return 2
    print("Offline CLI source: " + str(path), flush=True)
    print("Version: " + cli.VERSION + "; synthetic responses; network and credentials blocked.", flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(suite_for(cli))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
