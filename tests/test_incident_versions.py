"""Offline version resolution and bounded, read-only Git tests."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from incident_test_support import GitRepository

SCRIPT = Path(__file__).resolve().parents[1] / "skills/jev-bugfix/scripts/incident_versions.py"
SPEC = importlib.util.spec_from_file_location("incident_versions", SCRIPT)
VERSIONS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERSIONS)


def event(commit=None, release=None, service="api", environment="production"):
    return {"event_id": "synthetic-001", "commit": commit, "release": release,
            "service": service, "environment": environment}


def mapping(revision, service="api", environment="production", release="deploy-1"):
    return {"schema_version": 1, "entries": [
        {"service": service, "environment": environment, "release": release,
         "revision": revision}]}


class IncidentVersions(unittest.TestCase):
    def setUp(self):
        self.repo = GitRepository()
        self.addCleanup(self.repo.close)
        self.repo.write("src/app.py", "def process():\n    return 'old'\n")
        self.first = self.repo.commit("first synthetic revision")
        self.repo.git("tag", "-a", "deployed-v1", "-m", "synthetic tag")
        self.repo.write("src/app.py", "def process():\n    return 'new'\n")
        self.second = self.repo.commit("second synthetic revision")

    def resolve(self, selected, **options):
        return VERSIONS.resolve_incident_version(self.repo.path, selected, **options)

    def test_same_checkout_resolves_full_and_short_commit(self):
        for revision in (self.second, self.second[:10]):
            with self.subTest(revision=revision):
                report = self.resolve(event(commit=revision))
                self.assertEqual(report["resolution"]["status"], "resolved")
                self.assertEqual(report["resolution"]["event_commit"], self.second)
                self.assertEqual(report["checkout"]["comparison"], "same")
                self.assertEqual(report["checkout"]["relation"], "same")
                self.assertIsNone(report["baseline"])

    def test_old_commit_uses_history_and_checkout_relation(self):
        report = self.resolve(event(commit=self.first))
        self.assertEqual(report["checkout"]["head"], self.second)
        self.assertEqual(report["checkout"]["comparison"], "different")
        self.assertEqual(report["checkout"]["relation"], "ancestor")
        self.repo.git("checkout", "--detach", self.first)
        report = self.resolve(event(commit=self.second))
        self.assertEqual(report["checkout"]["relation"], "descendant")

    def test_diverged_checkout_has_no_implied_baseline(self):
        self.repo.git("checkout", "--detach", self.first)
        self.repo.write("src/app.py", "independent synthetic change\n")
        self.repo.commit("diverging synthetic revision")
        report = self.resolve(event(commit=self.second))
        self.assertEqual(report["checkout"]["relation"], "diverged")
        self.assertEqual(report["checkout"]["comparison"], "different")
        self.assertIsNone(report["baseline"])

    def test_replace_refs_cannot_change_bound_source_version(self):
        self.repo.git("replace", self.first, self.second)
        result = VERSIONS.read_source_blob(self.repo.path, self.first, "src/app.py")
        self.assertEqual(result["text"], "def process():\n    return 'old'\n")

    def test_legacy_grafts_cannot_change_real_commit_relationship(self):
        grafts = self.repo.path / ".git/info/grafts"
        grafts.write_text(self.second + "\n", encoding="ascii")
        report = self.resolve(event(commit=self.first))
        self.assertEqual(report["checkout"]["relation"], "ancestor")

    def test_source_path_uses_repository_root_even_when_repo_is_subdirectory(self):
        self.repo.write("app.py", "root synthetic source\n")
        commit = self.repo.commit()
        result = VERSIONS.read_source_blob(self.repo.path / "src", commit, "app.py")
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "root synthetic source\n")
        self.assertEqual(result["oid"], self.repo.git("rev-parse", commit + ":app.py"))

    def test_partial_clone_missing_blob_does_not_execute_promisor_remote(self):
        blob = self.repo.git("rev-parse", self.first + ":src/app.py")
        object_path = self.repo.path / ".git/objects" / blob[:2] / blob[2:]
        object_path.unlink()
        marker = self.repo.path / "remote-marker"
        program = self.repo.write("synthetic-remote.sh", "#!/bin/sh\ntouch '%s'\nexit 1\n" % marker)
        program.chmod(0o755)
        self.repo.git("config", "core.repositoryformatversion", "1")
        self.repo.git("config", "extensions.partialClone", "origin")
        self.repo.git("config", "remote.origin.promisor", "true")
        self.repo.git("config", "remote.origin.url", "ext::" + str(program))
        result = VERSIONS.read_source_blob(self.repo.path, self.first, "src/app.py")
        self.assertFalse(result["ok"])
        self.assertFalse(marker.exists())
        self.assertEqual(result["code"], "git_failed")

    def test_dirty_status_and_reads_preserve_head_index_and_worktree(self):
        self.repo.write("src/app.py", "staged version\n")
        self.repo.git("add", "src/app.py")
        self.repo.write("src/app.py", "unstaged version\n")
        self.repo.write("scratch.txt", "untracked\n")
        before = self.repo.git("status", "--porcelain=v1")
        index_before = (self.repo.path / ".git/index").read_bytes()
        blob = VERSIONS.read_source_blob(self.repo.path, self.first, "src/app.py")
        report = self.resolve(event(commit=self.first))
        self.assertTrue(blob["ok"])
        self.assertEqual(blob["text"], "def process():\n    return 'old'\n")
        self.assertTrue(report["checkout"]["staged"])
        self.assertTrue(report["checkout"]["unstaged"])
        self.assertTrue(report["checkout"]["untracked"])
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), self.second)
        self.assertEqual(self.repo.git("status", "--porcelain=v1"), before)
        self.assertEqual((self.repo.path / ".git/index").read_bytes(), index_before)
        self.assertEqual((self.repo.path / "src/app.py").read_text(), "unstaged version\n")

    def test_release_requires_exact_mapping_and_freezes_annotated_tag(self):
        report = self.resolve(event(release="deploy-1"),
                              release_map=mapping("refs/tags/deployed-v1"))
        self.assertEqual(report["resolution"]["event_commit"], self.first)
        self.assertEqual(report["resolution"]["clues"][0]["source"], "release_map")
        for selected in (event(release="deployed-v1"), event(release="deploy-1", service="worker"),
                         event(release="deploy-1", environment="staging")):
            self.assertEqual(self.resolve(selected, release_map=mapping(self.first))
                             ["resolution"]["status"], "unknown")
        self.assertEqual(self.resolve(event(release=self.first))["resolution"]["event_commit"],
                         self.first)

    def test_null_mapping_fields_match_null_and_not_other_values(self):
        release_map = mapping(self.first, service=None, environment=None, release=None)
        report = self.resolve(event(service=None, environment=None), release_map=release_map)
        self.assertEqual(report["resolution"]["event_commit"], self.first)
        self.assertEqual(self.resolve(event(), release_map=release_map)["resolution"]["status"],
                         "unknown")

    def test_conflicting_commit_mapping_or_release_is_ambiguous(self):
        for selected, release_map in (
            (event(commit=self.second, release="deploy-1"), mapping(self.first)),
            (event(commit=self.second, release=self.first), None),
            (event(release="deploy-1"), {"schema_version": 1, "entries":
             mapping(self.first)["entries"] + mapping(self.second)["entries"]}),
        ):
            report = self.resolve(selected, release_map=release_map)
            self.assertEqual(report["resolution"]["status"], "ambiguous")
            self.assertIsNone(report["resolution"]["event_commit"])
            self.assertEqual(report["checkout"]["comparison"], "unknown")

    def test_missing_clue_does_not_let_other_clue_hide_uncertainty(self):
        report = self.resolve(event(commit="f" * 40, release="deploy-1"),
                              release_map=mapping(self.first))
        self.assertEqual(report["resolution"]["status"], "unknown")
        self.assertIsNone(report["resolution"]["event_commit"])
        self.assertIn("commit_missing", report["resolution"]["diagnostics"])

    def test_revisions_are_not_shell_or_git_expressions(self):
        for revision in ("HEAD", "HEAD~1", self.first + "^{commit}", "--all", "a;touch marker",
                         "refs/heads/main", "refs/tags/../../HEAD", "refs/tags/tag^{commit}"):
            with self.subTest(revision=revision):
                report = self.resolve(event(commit=revision))
                self.assertEqual(report["resolution"]["status"], "unknown")
                self.assertIn("revision_invalid", report["resolution"]["diagnostics"])
                report = self.resolve(event(release="deploy-1"), release_map=mapping(revision))
                self.assertEqual(report["resolution"]["status"], "unknown")
        self.assertFalse((self.repo.path / "marker").exists())

    def test_explicit_baseline_returns_frozen_commit_and_changed_paths(self):
        report = self.resolve(event(commit=self.second),
                              baseline_revision="refs/tags/deployed-v1")
        self.assertEqual(report["baseline"]["status"], "resolved")
        self.assertEqual(report["baseline"]["commit"], self.first)
        self.assertEqual(report["baseline"]["changes"], [{"status": "M", "path": "src/app.py"}])
        self.assertNotIn("revision", report["baseline"])

    def test_non_repository_and_missing_commit_are_safe_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            report = VERSIONS.resolve_incident_version(directory, event(commit=self.first))
            self.assertEqual(report["resolution"]["status"], "unknown")
            self.assertIn("not_git_repository", report["diagnostics"])
        report = self.resolve(event(commit="f" * 40))
        self.assertIn("commit_missing", report["resolution"]["diagnostics"])

    def test_short_collision_is_ambiguous_in_real_object_database(self):
        tree = self.repo.git("rev-parse", "HEAD^{tree}")
        seen = {}
        collision = None
        for number in range(10000):
            body = ("tree %s\nauthor Synthetic <synthetic@example.invalid> 1 +0000\n"
                    "committer Synthetic <synthetic@example.invalid> 1 +0000\n\ncollision %s\n"
                    % (tree, number)).encode()
            oid = hashlib.sha1(b"commit " + str(len(body)).encode() + b"\0" + body).hexdigest()
            prefix = oid[:4]
            if prefix in seen:
                collision = (prefix, seen[prefix], body)
                break
            seen[prefix] = body
        self.assertIsNotNone(collision)
        prefix, left, right = collision
        self.repo.git("hash-object", "-t", "commit", "-w", "--stdin", input=left.decode())
        self.repo.git("hash-object", "-t", "commit", "-w", "--stdin", input=right.decode())
        report = self.resolve(event(commit=prefix))
        self.assertEqual(report["resolution"]["status"], "ambiguous")
        self.assertIn("commit_ambiguous", report["resolution"]["diagnostics"])

    def test_shallow_missing_history_is_reported_without_fetch(self):
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / "shallow"
            subprocess.run(["git", "clone", "-q", "--depth=1", self.repo.path.as_uri(), str(clone)],
                           check=True, capture_output=True, timeout=10)
            report = VERSIONS.resolve_incident_version(clone, event(commit=self.first))
            self.assertTrue(report["checkout"]["shallow"])
            self.assertIn("shallow_history_missing", report["resolution"]["diagnostics"])

    def test_fsmonitor_diff_and_textconv_programs_are_not_executed(self):
        marker = self.repo.path / "marker"
        hook = self.repo.write("malicious-hook.sh", "#!/bin/sh\ntouch '%s'\n" % marker)
        hook.chmod(0o755)
        self.repo.git("config", "core.fsmonitor", str(hook))
        self.repo.git("config", "diff.external", str(hook))
        self.repo.git("config", "diff.synthetic.textconv", str(hook))
        self.repo.write(".gitattributes", "*.py diff=synthetic\n")
        report = self.resolve(event(commit=self.second), baseline_revision=self.first)
        blob = VERSIONS.read_source_blob(self.repo.path, self.first, "src/app.py")
        self.assertEqual(report["resolution"]["status"], "resolved")
        self.assertTrue(blob["ok"])
        self.assertFalse(marker.exists())

    def test_clean_and_process_filters_do_not_execute_during_dirty_status(self):
        for driver, config_key in (("MiXeD.Case-Driver", "clean"), ("SyntheticProcess", "process")):
            with self.subTest(driver=driver):
                with GitRepository() as repo:
                    repo.write("app.py", "old synthetic source\n")
                    repo.write(".gitattributes", "*.py filter=%s\n" % driver)
                    commit = repo.commit()
                    marker = repo.path / "filter-marker"
                    hook = repo.write("synthetic-filter.sh", "#!/bin/sh\ntouch '%s'\ncat\n" % marker)
                    hook.chmod(0o755)
                    repo.git("config", "filter.%s.%s" % (driver, config_key), str(hook))
                    repo.git("config", "filter.%s.required" % driver, "true")
                    repo.write("app.py", "new synthetic source\n")
                    index_before = (repo.path / ".git/index").read_bytes()
                    report = VERSIONS.resolve_incident_version(repo.path, event(commit=commit))
                    self.assertFalse(marker.exists())
                    self.assertEqual(report["resolution"]["status"], "resolved")
                    self.assertTrue(report["checkout"]["unstaged"])
                    self.assertEqual((repo.path / ".git/index").read_bytes(), index_before)

    def test_filter_names_from_included_config_are_case_sensitive_and_disabled(self):
        self.repo.write(".gitattributes", "*.py filter=MiXeD.Included\n")
        commit = self.repo.commit()
        marker = self.repo.path / "included-filter-marker"
        hook = self.repo.write("synthetic-filter.sh", "#!/bin/sh\ntouch '%s'\ncat\n" % marker)
        hook.chmod(0o755)
        included = self.repo.write("included-config", '[filter "MiXeD.Included"]\nclean = %s\n' % hook)
        self.repo.git("config", "include.path", str(included))
        self.repo.write("src/app.py", "def process():\n    return 'old'\n")
        report = self.resolve(event(commit=commit))
        self.assertFalse(marker.exists())
        self.assertTrue(report["checkout"]["unstaged"])

    def test_unsafe_filter_names_fail_closed_without_echoing_configuration(self):
        for driver in ("synthetic=SECRET_TOKEN", "synthetic SECRET_TOKEN"):
            with self.subTest(driver=driver):
                self.repo.git("config", "filter.%s.clean" % driver, "arbitrary program")
                report = self.resolve(event(commit=self.second))
                self.assertEqual(report["resolution"]["status"], "unknown")
                self.assertIn("git_filter_config_invalid", report["diagnostics"])
                self.assertNotIn("SECRET_TOKEN", json.dumps(report))

    def test_source_rejects_symlinks_submodules_and_invalid_paths(self):
        os.symlink("src/app.py", self.repo.path / "link.py")
        os.symlink("src", self.repo.path / "linked-dir")
        self.repo.git("add", "link.py", "linked-dir")
        self.repo.git("update-index", "--add", "--cacheinfo", "160000,%s,module" % self.first)
        self.repo.git("commit", "-q", "-m", "synthetic unsafe paths")
        commit = self.repo.git("rev-parse", "HEAD")
        for path, code in (("link.py", "source_symlink"), ("linked-dir/app.py", "source_symlink"),
                           ("module/app.py", "source_submodule"), ("../src/app.py", "source_path_invalid"),
                           ("/src/app.py", "source_path_invalid"), ("src\\app.py", "source_path_invalid"),
                           ("src/missing.py", "source_missing")):
            with self.subTest(path=path):
                result = VERSIONS.read_source_blob(self.repo.path, commit, path)
                self.assertEqual(result, {"ok": False, "code": code})

    def test_source_size_encoding_and_commit_validation(self):
        self.repo.write("large.py", "x" * (1024 * 1024 + 1))
        (self.repo.path / "bad.py").write_bytes(b"\xff")
        self.repo.write("binary.py", "a\0b")
        commit = self.repo.commit()
        for path, code in (("large.py", "source_too_large"), ("bad.py", "source_encoding_invalid"),
                           ("binary.py", "source_binary")):
            self.assertEqual(VERSIONS.read_source_blob(self.repo.path, commit, path),
                             {"ok": False, "code": code})
        self.assertEqual(VERSIONS.read_source_blob(self.repo.path, "HEAD", "src/app.py"),
                         {"ok": False, "code": "revision_invalid"})

    def test_release_map_file_is_strict_bounded_and_validated_before_git(self):
        target = self.repo.path / "mapping.json"
        target.write_text(json.dumps(mapping(self.first)), encoding="utf-8")
        self.assertEqual(self.resolve(event(release="deploy-1"), release_map=target)
                         ["resolution"]["event_commit"], self.first)
        invalid = (
            '{"schema_version":1,"schema_version":1,"entries":[]}',
            '{"schema_version":1,"entries":[],"unknown":true}',
            '{"schema_version":NaN,"entries":[]}',
            json.dumps(mapping("x" * 4097)),
            '{"schema_version":1,"entries":' + '[' * 33 + ']' * 33 + '}',
            '{"schema_version":1,"entries":[{"service":"\\ud800","environment":null,'
            '"release":null,"revision":"abcd"}]}',
            '{"schema_version":1,"entries":[]} ' + ' ' * (2 * 1024 * 1024),
        )
        for raw in invalid:
            with self.subTest(raw_length=len(raw)):
                target.write_text(raw, encoding="utf-8")
                with patch.object(VERSIONS.subprocess, "Popen", side_effect=AssertionError("Git ran")):
                    report = self.resolve(event(release="deploy-1"), release_map=target)
                self.assertEqual(report["resolution"]["status"], "unknown")
                self.assertTrue(any(code.startswith("release_map_")
                                    for code in report["diagnostics"]))


class GitLimits(unittest.TestCase):
    def setUp(self):
        self.repo = GitRepository()
        self.addCleanup(self.repo.close)
        self.repo.write("app.py", "pass\n")
        self.commit = self.repo.commit()

    def executable(self, body):
        script = self.repo.path / "synthetic-git.py"
        script.write_text("#!%s\n%s\n" % (sys.executable, body), encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_bounded_process_timeout_returns_no_raw_stderr(self):
        executable = self.executable("import sys,time\nsys.stderr.write('SECRET_TOKEN=raw')\n"
                                     "sys.stderr.flush()\ntime.sleep(2)")
        with patch.object(VERSIONS, "GIT_PROCESS_TIMEOUT_SECONDS", 0.05), \
                patch.object(VERSIONS.shutil, "which", return_value=executable):
            result = VERSIONS.GitSession(self.repo.path).run(["rev-parse", "--is-inside-work-tree"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "git_timeout")
        self.assertNotIn("SECRET_TOKEN", json.dumps(result, default=str))

    def test_combined_stdout_stderr_limit_is_enforced_while_reading(self):
        executable = self.executable("import os\nos.write(1,b'a'*600000)\nos.write(2,b'b'*600000)")
        with patch.object(VERSIONS.shutil, "which", return_value=executable):
            result = VERSIONS.GitSession(self.repo.path).run(["rev-parse", "--is-inside-work-tree"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "git_output_limit")
        self.assertEqual(result["stdout"], b"")

    def test_session_budget_counts_time_between_git_calls(self):
        with patch.object(VERSIONS, "GIT_TOTAL_TIMEOUT_SECONDS", 0.05):
            session = VERSIONS.GitSession(self.repo.path)
        time.sleep(0.06)
        result = session.run(["rev-parse", "--is-inside-work-tree"])
        self.assertEqual(result["code"], "git_budget_exceeded")

    def test_runner_uses_safe_flags_environment_and_read_only_command_allowlist(self):
        log = self.repo.path / "argv.json"
        executable = self.executable("import json,os,sys\nif 'config' in sys.argv: sys.exit(1)\n"
                                     "json.dump({'argv':sys.argv[1:],'env':"
                                     "{k:v for k,v in os.environ.items() if k.startswith('GIT_')}},"
                                     "open(%r,'w'))\nprint('true')" % str(log))
        with patch.object(VERSIONS.shutil, "which", return_value=executable):
            session = VERSIONS.GitSession(self.repo.path)
            result = session.run(["rev-parse", "--is-inside-work-tree"])
            for args in (["fetch"], ["checkout", "main"], ["cat-file", "--filters", self.commit],
                         ["diff", "--ext-diff"], ["-c", "core.fsmonitor=evil", "status"]):
                self.assertEqual(session.run(args)["code"], "git_command_forbidden")
        self.assertTrue(result["ok"])
        data = json.loads(log.read_text())
        self.assertIn("core.fsmonitor=false", data["argv"])
        self.assertIn("protocol.allow=never", data["argv"])
        self.assertIn("--no-optional-locks", data["argv"])
        self.assertEqual(data["env"]["GIT_NO_LAZY_FETCH"], "1")
        self.assertEqual(data["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(data["env"]["GIT_ALLOW_PROTOCOL"], "")

    def test_blob_timeout_is_not_reported_as_missing_source(self):
        executable = self.executable("import time\ntime.sleep(2)")
        with patch.object(VERSIONS, "GIT_PROCESS_TIMEOUT_SECONDS", 0.05), \
                patch.object(VERSIONS.shutil, "which", return_value=executable):
            result = VERSIONS.read_source_blob(self.repo.path, self.commit, "app.py")
        self.assertEqual(result, {"ok": False, "code": "git_timeout"})


if __name__ == "__main__":
    unittest.main()
