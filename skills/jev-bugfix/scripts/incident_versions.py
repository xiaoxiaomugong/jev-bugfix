"""Offline incident commit resolution and bounded, read-only Git access.

No revision supplied by an event is a Git expression. All source reads use
frozen commit/object IDs, and diagnostics never contain Git stderr or revisions.
Share one GitSession across resolution, related events, and candidate reads.
"""

import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time


GIT_PROCESS_TIMEOUT_SECONDS = 5.0
GIT_TOTAL_TIMEOUT_SECONDS = 20.0
GIT_OUTPUT_LIMIT_BYTES = 1024 * 1024
MAX_SOURCE_BLOB_BYTES = 1024 * 1024
MAX_RELEASE_MAP_BYTES = 2 * 1024 * 1024
MAX_TEXT_BYTES = 4096
MAX_JSON_DEPTH = 32
_HEX = re.compile(r"[0-9a-fA-F]{4,64}\Z")
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_FILTER_QUERY = ("config", "--includes", "--null", "--name-only", "--get-regexp",
                 r"^filter\..*\.(clean|process|required)$")


def _valid_tag(value):
    if not isinstance(value, str) or not value.startswith("refs/tags/"):
        return False
    if (len(value.encode("utf-8", errors="replace")) > MAX_TEXT_BYTES or
            any(ord(char) < 33 or ord(char) == 127 for char in value) or
            any(char in value for char in "~^:?*[\\") or ".." in value or
            "@{" in value or value.endswith(".") or value.endswith("/")):
        return False
    return all(part and not part.startswith(".") and not part.endswith(".lock")
               for part in value.split("/"))


def _full_oid(value):
    return isinstance(value, str) and _OID.fullmatch(value) is not None


def _safe_path(path):
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    try:
        path.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return (not any(ord(char) < 32 or ord(char) == 127 for char in path) and
            not re.match(r"^[A-Za-z]:", path) and
            all(part not in ("", ".", "..") for part in path.split("/")))


def _allowed_command(args):
    """Allow only the exact read forms this module needs, never arbitrary Git."""
    if not isinstance(args, (list, tuple)) or not args or not all(isinstance(x, str) for x in args):
        return False
    if args[0] == "rev-parse":
        if list(args[1:]) in (["--is-inside-work-tree"], ["--is-shallow-repository"],
                             ["--verify", "HEAD"]):
            return True
        if len(args) == 2 and args[1].startswith("--disambiguate="):
            return _HEX.fullmatch(args[1][15:]) is not None
        return (len(args) == 4 and list(args[1:3]) == ["--verify", "--end-of-options"] and
                args[3].endswith("^{commit}") and _valid_tag(args[3][:-9]))
    if args[0] == "cat-file":
        return len(args) == 3 and args[1] in ("-t", "-s", "blob") and _full_oid(args[2])
    if args[0] == "config":
        return tuple(args) == _FILTER_QUERY
    if args[0] == "ls-tree":
        return (len(args) == 6 and list(args[1:3]) == ["--full-tree", "-z"] and
                _full_oid(args[3]) and args[4] == "--" and
                _safe_path(args[5]) and "/" not in args[5])
    if args[0] == "status":
        return list(args[1:]) == ["--porcelain=v1", "-z", "--untracked-files=normal",
                                  "--ignore-submodules=dirty"]
    if args[0] == "merge-base":
        return (len(args) == 4 and args[1] == "--is-ancestor" and
                _full_oid(args[2]) and _full_oid(args[3]))
    if args[0] == "diff":
        return (len(args) == 10 and list(args[1:7]) ==
                ["--name-status", "-z", "--no-ext-diff", "--no-textconv", "--no-renames",
                 "--ignore-submodules=all"] and _full_oid(args[7]) and
                _full_oid(args[8]) and args[9] == "--")
    return False


class GitSession:
    """A preparation-wide wall clock budget; initialization never starts Git."""

    def __init__(self, repo):
        self.repo = Path(repo)
        self.deadline = time.monotonic() + GIT_TOTAL_TIMEOUT_SECONDS
        self.executable = shutil.which("git")
        self.diagnostics = []

    def _failure(self, code, returncode=None):
        if code not in self.diagnostics:
            self.diagnostics.append(code)
        return {"ok": False, "stdout": b"", "returncode": returncode, "code": code}

    def run(self, args):
        if not _allowed_command(args):
            return self._failure("git_command_forbidden")
        # Git status can invoke clean/process conversion even with diff and
        # fsmonitor disabled. Query only driver names (never command values),
        # preserving case-sensitive subsections and NUL-delimited boundaries.
        # Refresh before each command, including repeated associated-event
        # checks, rather than trusting a configuration snapshot for the session.
        if tuple(args) == _FILTER_QUERY:
            return self._run(args, acceptable_nonzero=(1,))
        configured = self._run(_FILTER_QUERY, acceptable_nonzero=(1,))
        if not configured["ok"]:
            return configured
        raw = configured["stdout"]
        if raw and not raw.endswith(b"\0"):
            return self._failure("git_filter_config_invalid")
        drivers = set()
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                key = record.decode("utf-8", errors="strict")
            except UnicodeError:
                return self._failure("git_filter_config_invalid")
            match = re.fullmatch(r"filter\.(.+)\.(?:clean|process|required)", key)
            if not match:
                return self._failure("git_filter_config_invalid")
            driver = match.group(1)
            # '=' would change -c's key/value boundary. Whitespace/control
            # characters and overlong names are also rejected, without echoing
            # the key. Arguments remain individual argv entries throughout.
            if (len(driver.encode("utf-8")) > MAX_TEXT_BYTES or "=" in driver or
                    any(not char.isprintable() or char.isspace() for char in driver)):
                return self._failure("git_filter_config_invalid")
            drivers.add(driver)
        overrides = []
        for driver in sorted(drivers):
            for suffix, value in (("clean", ""), ("process", ""), ("required", "false")):
                overrides.extend(["-c", "filter." + driver + "." + suffix + "=" + value])
        return self._run(args, overrides=overrides)

    def _run(self, args, overrides=(), acceptable_nonzero=()):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return self._failure("git_budget_exceeded")
        if self.executable is None:
            return self._failure("git_unavailable")
        timeout = min(GIT_PROCESS_TIMEOUT_SECONDS, remaining)
        expiry = time.monotonic() + timeout
        timeout_code = "git_budget_exceeded" if remaining <= GIT_PROCESS_TIMEOUT_SECONDS else "git_timeout"
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
                    "GIT_GRAFT_FILE": os.devnull, "GIT_ALLOW_PROTOCOL": "", "LC_ALL": "C"})
        command = [self.executable, "--no-optional-locks", "--literal-pathspecs",
                   "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                   "-c", "core.hooksPath=" + os.devnull,
                   "-c", "diff.external=", "-c", "status.submoduleSummary=false",
                   "-c", "submodule.recurse=false", "-c", "protocol.allow=never",
                   "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never",
                   "-c", "credential.helper=", "-c", "maintenance.auto=false",
                   "-c", "gc.auto=0", *overrides, "-C", str(self.repo), *args]
        process = None
        selector = selectors.DefaultSelector()
        output = bytearray()
        byte_count = 0
        error_code = None
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env, start_new_session=True)
            for stream, is_stdout in ((process.stdout, True), (process.stderr, False)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, is_stdout)
            while selector.get_map():
                remaining_time = expiry - time.monotonic()
                if remaining_time <= 0:
                    error_code = timeout_code
                    break
                for key, _ in selector.select(remaining_time):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    byte_count += len(chunk)
                    if byte_count > GIT_OUTPUT_LIMIT_BYTES:
                        error_code = "git_output_limit"
                        break
                    if key.data:
                        output.extend(chunk)
                if error_code:
                    break
            if error_code is None:
                try:
                    process.wait(timeout=max(0.001, expiry - time.monotonic()))
                except subprocess.TimeoutExpired:
                    error_code = timeout_code
            if error_code:
                return self._failure(error_code)
            if process.returncode != 0 and process.returncode not in acceptable_nonzero:
                return self._failure("git_failed", process.returncode)
            return {"ok": True, "stdout": bytes(output), "returncode": process.returncode, "code": None}
        except (OSError, ValueError, subprocess.SubprocessError):
            return self._failure("git_io_error")
        finally:
            selector.close()
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                    process.wait()
                process.stdout.close()
                process.stderr.close()


def _unique(values):
    return list(dict.fromkeys(values))


class _MapError(ValueError):
    pass


def _map_validate(value):
    if not isinstance(value, dict) or set(value) != {"schema_version", "entries"}:
        raise _MapError("release_map_schema_invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or not isinstance(value["entries"], list):
        raise _MapError("release_map_schema_invalid")
    for entry in value["entries"]:
        if not isinstance(entry, dict) or set(entry) != {"service", "environment", "release", "revision"}:
            raise _MapError("release_map_schema_invalid")
        for key in ("service", "environment", "release", "revision"):
            text = entry[key]
            if text is None and key != "revision":
                continue
            if not isinstance(text, str) or (key == "revision" and not text):
                raise _MapError("release_map_schema_invalid")
            try:
                size = len(text.encode("utf-8", errors="strict"))
            except UnicodeError:
                raise _MapError("release_map_encoding_invalid")
            if size > MAX_TEXT_BYTES:
                raise _MapError("release_map_text_limit")
    # Bound dictionaries supplied programmatically just as file input is bounded.
    try:
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RELEASE_MAP_BYTES:
            raise _MapError("release_map_size_limit")
    except (UnicodeError, ValueError, RecursionError):
        raise _MapError("release_map_schema_invalid")
    return value


def _map_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _MapError("release_map_duplicate_key")
        result[key] = value
    return result


def _map_depth(text):
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise _MapError("release_map_depth_limit")
        elif char in "]}":
            depth -= 1


def load_release_map(path):
    """Load one strict local mapping; raw JSON/error text never enters results."""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_RELEASE_MAP_BYTES + 1)
        if len(raw) > MAX_RELEASE_MAP_BYTES:
            raise _MapError("release_map_size_limit")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeError:
            raise _MapError("release_map_encoding_invalid")
        _map_depth(text)
        def no_constant(_value):
            raise _MapError("release_map_nonfinite")
        value = json.loads(text, object_pairs_hook=_map_pairs, parse_constant=no_constant)
        return {"ok": True, "release_map": _map_validate(value), "diagnostics": []}
    except _MapError as exc:
        return {"ok": False, "release_map": None, "diagnostics": [str(exc)]}
    except (OSError, TypeError, ValueError, RecursionError):
        return {"ok": False, "release_map": None, "diagnostics": ["release_map_read_invalid"]}


def _clue(git, revision, source, allow_tag=False, shallow=False):
    result = {"source": source, "status": "unknown", "commit": None, "diagnostic": None}
    if isinstance(revision, str) and _HEX.fullmatch(revision):
        response = git.run(["rev-parse", "--disambiguate=" + revision.lower()])
        if not response["ok"]:
            result["diagnostic"] = response["code"]
            return result
        identifiers = response["stdout"].decode("ascii", errors="replace").splitlines()
        if len(identifiers) > 1:
            result.update(status="ambiguous", diagnostic="commit_ambiguous")
            return result
        if not identifiers:
            result["diagnostic"] = "shallow_history_missing" if shallow else "commit_missing"
            return result
        if not _full_oid(identifiers[0]):
            result["diagnostic"] = "git_output_invalid"
            return result
        identifier = identifiers[0]
        response = git.run(["cat-file", "-t", identifier])
        if not response["ok"]:
            result["diagnostic"] = response["code"]
        elif response["stdout"].strip() != b"commit":
            result["diagnostic"] = "revision_not_commit"
        else:
            result.update(status="resolved", commit=identifier)
        return result
    if allow_tag and _valid_tag(revision):
        response = git.run(["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"])
        if response["ok"]:
            identifier = response["stdout"].decode("ascii", errors="replace").strip()
            if _full_oid(identifier):
                result.update(status="resolved", commit=identifier)
            else:
                result["diagnostic"] = "git_output_invalid"
        else:
            result["diagnostic"] = "tag_missing" if response["code"] == "git_failed" else response["code"]
        return result
    result["diagnostic"] = "revision_invalid"
    return result


def _empty_report():
    return {"resolution": {"status": "unknown", "event_commit": None, "clues": [], "diagnostics": []},
            "checkout": {"head": None, "staged": None, "unstaged": None, "untracked": None,
                         "comparison": "unknown", "relation": "unknown", "shallow": None},
            "baseline": None, "diagnostics": []}


def _checkout(git, checkout, diagnostics):
    response = git.run(["rev-parse", "--verify", "HEAD"])
    if response["ok"]:
        identifier = response["stdout"].decode("ascii", errors="replace").strip()
        if _full_oid(identifier):
            checkout["head"] = identifier
        else:
            diagnostics.append("git_output_invalid")
    else:
        diagnostics.append("checkout_head_unknown" if response["code"] == "git_failed" else response["code"])
    response = git.run(["rev-parse", "--is-shallow-repository"])
    if response["ok"] and response["stdout"].strip() in (b"true", b"false"):
        checkout["shallow"] = response["stdout"].strip() == b"true"
    else:
        diagnostics.append(response["code"] or "git_output_invalid")
    response = git.run(["status", "--porcelain=v1", "-z", "--untracked-files=normal",
                        "--ignore-submodules=dirty"])
    if not response["ok"]:
        diagnostics.append(response["code"])
        return
    staged = unstaged = untracked = False
    records = response["stdout"].split(b"\0")
    skip_rename = False
    for record in records:
        if not record:
            continue
        if skip_rename:
            skip_rename = False
            continue
        if len(record) < 3 or record[2:3] != b" ":
            diagnostics.append("git_output_invalid")
            return
        status = record[:2]
        if status == b"??":
            untracked = True
        elif status != b"!!":
            staged = staged or status[0:1] != b" "
            unstaged = unstaged or status[1:2] != b" "
            skip_rename = b"R" in status or b"C" in status
    checkout.update(staged=staged, unstaged=unstaged, untracked=untracked)


def _relationship(git, checkout, commit, diagnostics):
    head = checkout["head"]
    if head is None or commit is None:
        return
    checkout["comparison"] = "same" if head == commit else "different"
    if head == commit:
        checkout["relation"] = "same"
        return
    response = git.run(["merge-base", "--is-ancestor", commit, head])
    if response["ok"]:
        checkout["relation"] = "ancestor"
        return
    if response["returncode"] != 1:
        diagnostics.append(response["code"])
        return
    response = git.run(["merge-base", "--is-ancestor", head, commit])
    if response["ok"]:
        checkout["relation"] = "descendant"
    elif response["returncode"] == 1:
        if checkout["shallow"]:
            diagnostics.append("shallow_relationship_unknown")
        else:
            checkout["relation"] = "diverged"
    else:
        diagnostics.append(response["code"])


def _baseline(git, revision, event_commit, shallow):
    clue = _clue(git, revision, "baseline", allow_tag=True, shallow=shallow)
    baseline = {"status": clue["status"], "commit": clue["commit"], "changes": [],
                "diagnostics": [clue["diagnostic"]] if clue["diagnostic"] else []}
    if clue["status"] != "resolved" or event_commit is None:
        return baseline
    response = git.run(["diff", "--name-status", "-z", "--no-ext-diff", "--no-textconv",
                        "--no-renames", "--ignore-submodules=all", clue["commit"], event_commit, "--"])
    if not response["ok"]:
        baseline["diagnostics"].append(response["code"])
        return baseline
    items = response["stdout"].split(b"\0")
    if items and not items[-1]:
        items.pop()
    if len(items) % 2:
        baseline["diagnostics"].append("git_output_invalid")
        return baseline
    changes = []
    for index in range(0, len(items), 2):
        try:
            status = items[index].decode("ascii", errors="strict")
            path = items[index + 1].decode("utf-8", errors="strict")
        except UnicodeError:
            baseline["diagnostics"].append("changed_path_invalid")
            continue
        if status not in ("A", "C", "D", "M", "R", "T", "U", "X", "B") or not _safe_path(path):
            baseline["diagnostics"].append("changed_path_invalid")
            continue
        changes.append({"status": status, "path": path})
    baseline["changes"] = changes
    baseline["diagnostics"] = _unique(baseline["diagnostics"])
    return baseline


def resolve_incident_version(repo, selected_event, release_map=None, baseline_revision=None, git=None):
    """Resolve every supplied clue consistently, without borrowing checkout HEAD."""
    report = _empty_report()
    if release_map is not None:
        if isinstance(release_map, (str, os.PathLike)):
            loaded = load_release_map(release_map)
        else:
            try:
                loaded = {"ok": True, "release_map": _map_validate(release_map), "diagnostics": []}
            except _MapError as exc:
                loaded = {"ok": False, "diagnostics": [str(exc)]}
        if not loaded["ok"]:
            report["diagnostics"] = loaded["diagnostics"]
            return report
        release_map = loaded["release_map"]
    selected = selected_event.get("event", selected_event)
    git = git if git is not None else GitSession(repo)
    response = git.run(["rev-parse", "--is-inside-work-tree"])
    if not response["ok"] or response["stdout"].strip() != b"true":
        report["diagnostics"].append("not_git_repository" if response["code"] == "git_failed"
                                     else response["code"] or "not_git_repository")
        return report
    _checkout(git, report["checkout"], report["diagnostics"])
    clues = []
    shallow = report["checkout"]["shallow"] is True
    commit = selected.get("commit")
    release = selected.get("release")
    if commit is not None:
        clues.append(_clue(git, commit, "event_commit", shallow=shallow))
    if release_map is not None:
        key = (selected.get("service"), selected.get("environment"), release)
        entries = [entry for entry in release_map["entries"] if
                   (entry["service"], entry["environment"], entry["release"]) == key]
        for entry in entries:
            clues.append(_clue(git, entry["revision"], "release_map", allow_tag=True, shallow=shallow))
        if not entries and release is not None:
            report["resolution"]["diagnostics"].append("release_mapping_missing")
    if isinstance(release, str) and _HEX.fullmatch(release):
        clues.append(_clue(git, release, "release_commit", shallow=shallow))
    resolution = report["resolution"]
    resolution["clues"] = clues
    diagnostics = resolution["diagnostics"]
    diagnostics.extend(clue["diagnostic"] for clue in clues if clue["diagnostic"])
    commits = {clue["commit"] for clue in clues if clue["status"] == "resolved"}
    if any(clue["status"] == "ambiguous" for clue in clues) or len(commits) > 1:
        resolution["status"] = "ambiguous"
        if len(commits) > 1:
            diagnostics.append("version_clues_conflict")
    elif clues and all(clue["status"] == "resolved" for clue in clues):
        resolution.update(status="resolved", event_commit=next(iter(commits)))
    elif not clues:
        diagnostics.append("version_clue_missing")
    resolution["diagnostics"] = _unique(diagnostics)
    _relationship(git, report["checkout"], resolution["event_commit"], report["diagnostics"])
    if baseline_revision is not None:
        report["baseline"] = _baseline(git, baseline_revision, resolution["event_commit"], shallow)
    report["diagnostics"] = _unique(report["diagnostics"])
    return report


def read_source_blob(repo, commit, path, git=None):
    """Read only a UTF-8 ordinary blob after checking each tree path segment."""
    if not _full_oid(commit):
        return {"ok": False, "code": "revision_invalid"}
    if not _safe_path(path):
        return {"ok": False, "code": "source_path_invalid"}
    git = git if git is not None else GitSession(repo)
    response = git.run(["cat-file", "-t", commit])
    if not response["ok"]:
        return {"ok": False, "code": "commit_missing" if response["code"] == "git_failed" else response["code"]}
    if response["stdout"].strip() != b"commit":
        return {"ok": False, "code": "revision_not_commit"}
    tree = commit
    parts = path.split("/")
    mode = None
    for index, part in enumerate(parts):
        response = git.run(["ls-tree", "--full-tree", "-z", tree, "--", part])
        if not response["ok"]:
            return {"ok": False, "code": response["code"]}
        records = [record for record in response["stdout"].split(b"\0") if record]
        if not records:
            return {"ok": False, "code": "source_missing"}
        if len(records) != 1:
            return {"ok": False, "code": "git_output_invalid"}
        try:
            metadata, name = records[0].split(b"\t", 1)
            mode_bytes, kind, oid_bytes = metadata.split(b" ")
            mode = mode_bytes.decode("ascii", errors="strict")
            oid = oid_bytes.decode("ascii", errors="strict")
        except (ValueError, UnicodeError):
            return {"ok": False, "code": "git_output_invalid"}
        if name != part.encode("utf-8") or not _full_oid(oid):
            return {"ok": False, "code": "git_output_invalid"}
        if mode == "120000":
            return {"ok": False, "code": "source_symlink"}
        if mode == "160000":
            return {"ok": False, "code": "source_submodule"}
        if index != len(parts) - 1:
            if mode != "040000" or kind != b"tree":
                return {"ok": False, "code": "source_not_directory"}
            tree = oid
        elif mode not in ("100644", "100755") or kind != b"blob":
            return {"ok": False, "code": "source_not_regular_blob"}
    response = git.run(["cat-file", "-s", oid])
    if not response["ok"]:
        return {"ok": False, "code": response["code"]}
    try:
        size = int(response["stdout"].strip())
    except ValueError:
        return {"ok": False, "code": "git_output_invalid"}
    if size < 0 or size > MAX_SOURCE_BLOB_BYTES:
        return {"ok": False, "code": "source_too_large"}
    response = git.run(["cat-file", "blob", oid])
    if not response["ok"]:
        return {"ok": False, "code": response["code"]}
    raw = response["stdout"]
    if len(raw) != size:
        return {"ok": False, "code": "git_output_invalid"}
    if b"\0" in raw:
        return {"ok": False, "code": "source_binary"}
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return {"ok": False, "code": "source_encoding_invalid"}
    return {"ok": True, "oid": oid, "text": text, "mode": mode}
