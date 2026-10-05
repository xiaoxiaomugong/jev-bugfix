"""Real temporary Git repositories for offline incident tests."""

import os
from pathlib import Path
import subprocess
import tempfile


class GitRepository:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="incident-git-")
        self.path = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args, input=None, check=True):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        result = subprocess.run(["git", "-C", str(self.path), *args],
                                input=input, capture_output=True, text=True,
                                env=env, timeout=10, check=check)
        return result.stdout.strip()

    def write(self, path, text):
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def commit(self, message="synthetic change"):
        self.git("add", "--all")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def close(self):
        self.tmp.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
