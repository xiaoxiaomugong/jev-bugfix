"""Small offline Git reconstruction and incident handoff regression coverage."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PILOT = ROOT / "tests" / "evidence" / "production-next" / "pilot"
SCRIPTS = ROOT / "skills" / "jev-bugfix" / "scripts"
BROKEN = "eb110f9f84ca0bdc0d9dac833d61859e15a3d527"
FIXED = "60264f0eae8b171ac46c5ecbf487d1304c3570ab"


class OSSFixtureRebuildTests(unittest.TestCase):
    def run_command(self, command, env=None):
        if env is None:
            env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
            env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        return subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=30)

    def rebuild(self, output, env=None, script=None):
        result = self.run_command([sys.executable, "-B", str(script or PILOT / "rebuild_repo.py"),
                                   "--output", str(output)], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def git(self, repo, *arguments):
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        result = self.run_command(["git", "-c", "core.hooksPath=" + os.devnull,
                                   "-c", "core.fsmonitor=false", "-C", str(repo), *arguments], env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_rebuild_is_deterministic_and_ignores_caller_git_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            hooks = directory / "hooks"
            hooks.mkdir()
            marker = directory / "hook-executed"
            hook = hooks / "post-checkout"
            hook.write_text("#!/bin/sh\n: > '" + str(marker) + "'\n", encoding="utf-8")
            hook.chmod(0o755)
            config = directory / "gitconfig"
            config.write_text("[core]\n\thooksPath = " + str(hooks) +
                              "\n\tautocrlf = true\n[commit]\n\tgpgsign = true\n", encoding="utf-8")
            env = dict(os.environ)
            env.update({"GIT_CONFIG_GLOBAL": str(config), "GIT_DIR": str(directory / "wrong-repo"),
                        "GIT_AUTHOR_NAME": "unexpected", "GIT_AUTHOR_EMAIL": "unexpected@example.invalid"})
            first = self.rebuild(directory / "first", env)
            second = self.rebuild(directory / "second")
            self.assertEqual(first["fixture_revisions"], {"broken": BROKEN, "fixed": FIXED})
            self.assertEqual(second["fixture_revisions"], first["fixture_revisions"])
            self.assertEqual(self.git(directory / "first", "rev-parse", "HEAD"), FIXED)
            self.assertEqual(self.git(directory / "first", "rev-list", "--count", "HEAD"), "2")
            self.assertEqual(self.git(directory / "first", "ls-tree", "-r", "--name-only", BROKEN),
                             "boltons/iterutils.py")
            source = directory / "first" / "boltons" / "iterutils.py"
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                             "77ec0e8d53b060f236d06f7c3c8ed107d34818dd8cf1b2a6b986bdea1167effe")
            self.assertFalse(marker.exists())

    def test_changed_source_is_rejected_before_output_is_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            copied = directory / "pilot"
            shutil.copytree(PILOT, copied)
            source = copied / "source" / "broken" / "iterutils.py"
            source.write_bytes(source.read_bytes() + b"\n# tampered\n")
            output = directory / "repo"
            result = self.run_command([sys.executable, "-B", str(copied / "rebuild_repo.py"),
                                       "--output", str(output)])
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["diagnostics"], ["source_hash_mismatch"])
            self.assertFalse(output.exists())

    def test_existing_output_is_preserved_and_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            output = directory / "repo"
            output.mkdir()
            marker = output / "keep.txt"
            marker.write_text("original", encoding="utf-8")
            result = self.run_command([sys.executable, "-B", str(PILOT / "rebuild_repo.py"),
                                       "--output", str(output)])
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["diagnostics"], ["output_exists"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(output.iterdir()), [marker])

    def test_rebuilt_history_supports_event_mapping_and_dynamic_context_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            repo = directory / "repo"
            self.rebuild(repo)
            expected = json.loads((PILOT / "expected.json").read_text(encoding="utf-8"))
            spec = importlib.util.spec_from_file_location("fixture_replay_driver", ROOT / "tests" / "oss_incident_replay.py")
            driver = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(driver)
            for role, revision in (("broken", BROKEN), ("fixed", FIXED)):
                raw = self.run_command(["git", "-c", "core.hooksPath=" + os.devnull,
                                        "-c", "core.fsmonitor=false", "-C", str(repo),
                                        "show", revision + ":boltons/iterutils.py"])
                self.assertEqual(raw.returncode, 0, raw.stderr)
                source = directory / (role + ".py")
                source.write_text(raw.stdout, encoding="utf-8")
                observed = driver.observe_source(source)
                outcomes = {item["case"]: {"output": item["output"],
                            "exception_type": item["exception"]["type"] if item["exception"] else None}
                            for item in observed["observations"]}
                self.assertEqual(outcomes, expected["outcomes"][role])
            output = directory / "output"
            prepare = self.run_command([sys.executable, "-B", str(SCRIPTS / "prepare_incident.py"),
                                        "--input", str(PILOT / "sentry-api-event.json"),
                                        "--input-format", "sentry-api-event/v1", "--repo", str(repo),
                                        "--release-map", str(PILOT / "release-map.json"),
                                        "--output-dir", str(output)])
            self.assertEqual(prepare.returncode, 0, prepare.stdout + prepare.stderr)
            self.assertEqual(json.loads(prepare.stdout)["status"], "ready")
            case = json.loads((output / "case.json").read_text(encoding="utf-8"))
            self.assertEqual(len(case["candidates"]), 1)
            candidate_id = case["candidates"][0]["id"]
            context = directory / "context.py"
            inspect = self.run_command([sys.executable, "-B", str(SCRIPTS / "inspect_incident.py"),
                                        "--bundle", str(output / "bundle.json"), "--repo", str(repo),
                                        "--candidate-id", candidate_id, "--context-lines", "20",
                                        "--source-output", str(context)])
            self.assertEqual(inspect.returncode, 0, inspect.stdout + inspect.stderr)
            result = json.loads(inspect.stdout)
            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["version"]["event_commit"], BROKEN)
            self.assertEqual(result["version"]["checkout_head"], FIXED)
            self.assertFalse(result["version"]["reviewed_for_secrets"])
            self.assertEqual(result["context"]["status"], "written")
            self.assertEqual(hashlib.sha256(context.read_bytes()).hexdigest(),
                             "10d270331fbb637349aff0475ec34d835aa2c192bef71a8df12a9c210cf6342d")


if __name__ == "__main__":
    unittest.main()
