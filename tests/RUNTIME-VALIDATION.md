# Ranker runtime validation

The offline tests exercise the real process boundary with a synthetic CLI. No network or credential read is needed. Run from the repository root:

```sh
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
python3 -B tests/verify_local_cli.py
```

The second command requires an already installed Jev 0.3.2 and inspects its local source with credential/configuration/transport blockers. It is separate from default test discovery.

Version preflight uses empty stdin, a 2 second deadline and 1024 bytes across stdout/stderr. Only canonical `jev 0.3.2\n` permits scoring. Dry runs, unreviewed input, shared sensitive evidence and batches with no eligible candidates start neither process. Scoring remains one process with the original candidate, payload and HTTP attempt budgets. An I/O failure after process startup retains the invocation and reserved budget, and preserves complete valid results.

The upstream credential boundary tests cover `.netrc`/`_netrc` variants, mixed safe/private pools and similarly named ordinary source files. The mixed-pool double explicitly checks preflight arguments and scoring payload; sensitive candidates remain in the investigation order and never reach scoring.

The CI matrix retains Linux/Python 3.9, Linux/Python 3.14 and macOS/Python 3.14. Local tests do not demonstrate a remote CI result or production benefit. Existing historical validation records remain historical.
