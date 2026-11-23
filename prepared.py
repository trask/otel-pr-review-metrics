import base64
import json
import sys
from functools import lru_cache
from hashlib import sha256
from pathlib import PurePosixPath
from urllib.parse import quote

from github_data import GitHubError, blob_url, instant
from git_evidence import GitEvidence, GitUnavailable, object_id


PREPARED_VERSION = 1
PATH_LIMIT = 4
MAX_COMMITS = 12


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return sha256(json_bytes(value)).hexdigest()


def bounded(text, limit):
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def patches(files, paths):
    return [
        {
            "path": item["filename"], "previous_path": item.get("previous_filename"),
            "status": item["status"], "patch": bounded(item.get("patch"), 10_000),
        }
        for item in files
        if item["filename"] in paths or item.get("previous_filename") in paths
    ]


class Evidence:
    def __init__(self, github, pull):
        self.github = github
        self.pull = pull
        self.repo = pull["repo"]
        self.merge = object_id(pull["merge"])
        self.head = object_id(pull["head"])
        root = github.git_root
        self.local = GitEvidence(root, self.repo, pull["number"]) if root else None
        self.reported_git_failure = False
        self.commit = lru_cache(maxsize=64)(self._commit)
        self.compare = lru_cache(maxsize=24)(self._compare)
        self.file = lru_cache(maxsize=128)(self._file)

    def fallback(self, error):
        if not self.reported_git_failure:
            print(f"{self.repo}#{self.pull['number']}: {error}; using REST for missing evidence.",
                  file=sys.stderr, flush=True)
            self.reported_git_failure = True

    def get(self, path, **params):
        try:
            return self.github.get(f"repos/{self.repo}/{path}", **params)
        except GitHubError as error:
            if error.not_found:
                return None
            raise

    def _commit(self, revision):
        if not revision:
            return None
        if self.local:
            try:
                return self.local.commit(revision)
            except (GitUnavailable, UnicodeError) as error:
                self.fallback(error)
        return self._rest_commit(revision)

    def _rest_commit(self, revision):
        item = self.get(f"commits/{revision}", per_page=100, page=1)
        if not item:
            return None
        files = item.get("files", [])
        for page in range(2, 31):
            if len(files) < (page - 1) * 100:
                break
            extra = self.get(f"commits/{revision}", per_page=100, page=page)
            if not extra:
                break
            files.extend(extra["files"])
        return item

    def _compare(self, start, end):
        if self.local:
            try:
                return self.local.compare(start, end)
            except (GitUnavailable, UnicodeError) as error:
                self.fallback(error)
        return self._rest_compare(start, end)

    def _rest_compare(self, start, end):
        path = f"compare/{start}...{end}"
        item = self.get(path, per_page=100, page=1)
        if not item:
            return None
        for page in range(2, (item["total_commits"] + 99) // 100 + 1):
            extra = self.get(path, per_page=100, page=page)
            if not extra:
                break
            item["commits"].extend(extra["commits"])
        return item

    def _file(self, revision, path):
        if self.local:
            try:
                content = self.local.file(revision, path)
            except (GitUnavailable, UnicodeError) as error:
                self.fallback(error)
            else:
                return self.file_state(content) if content is not None else {"state": "absent"}
        item = self.get(f"contents/{quote(path)}", ref=revision)
        if item is None:
            return {"state": "absent"}
        if not isinstance(item, dict) or item.get("encoding") != "base64":
            return {"state": "unavailable"}
        try:
            return self.file_state(base64.b64decode(item["content"]))
        except ValueError:
            return {"state": "unavailable"}

    @staticmethod
    def file_state(content):
        try:
            text = content.decode("utf-8")
        except UnicodeError:
            return {"state": "unavailable"}
        if "\0" in text:
            return {"state": "unavailable"}
        return {"state": "text" if len(text) <= 14_000 else "truncated",
                "text": bounded(text, 14_000)}

    def scoped_patches(self, item, paths, rest):
        files = item.get("files", [])
        if "git_range" in item:
            try:
                return patches([
                    dict(file, patch=self.local.patch(*item["git_range"], file["filename"]))
                    for file in files if file["filename"] in paths
                ], paths)
            except (GitUnavailable, UnicodeError) as error:
                self.fallback(error)
                files = (rest() or {}).get("files", [])
        return patches(files, paths)

    def changed_paths(self):
        revisions = set(filter(None, (object_id(t["reviewed"]) for t in self.pull["threads"])))
        if self.local:
            try:
                self.local.prepare({*revisions, self.head, self.merge})
            except GitUnavailable as error:
                self.fallback(error)
        files = list((self.commit(self.merge) or {}).get("files", []))
        if self.head:
            for reviewed in sorted(revisions):
                files += (self.compare(reviewed, self.head) or {}).get("files", [])
        return {path for item in files for path in (item["filename"], item.get("previous_filename")) if path}

    def for_thread(self, thread, paths):
        reviewed = object_id(thread["reviewed"])
        records = {
            f"reply-{i}": {"url": reply["url"]}
            for i, reply in enumerate(thread["replies"])
        }
        compared = self.compare(reviewed, self.head) if reviewed and self.head else None
        later_shas = []
        early_count = 0
        complete = False
        if compared:
            files = compared.get("files", [])
            complete = (
                compared["status"] in {"ahead", "identical"}
                and (compared.get("files_complete") or (len(files) < 300 and "files" in compared))
                and len(compared["commits"]) == compared["total_commits"]
            )
            opened, merged = instant(thread["opened"]), instant(self.pull["merged"])
            for item in compared["commits"]:
                committed = instant(item["commit"]["committer"]["date"])
                if opened < committed <= merged and object_id(item["sha"]):
                    later_shas.append((item["sha"], item["commit"]["committer"]["date"]))
                elif committed <= opened:
                    early_count += 1
                elif committed > merged:
                    complete = False
            scoped = self.scoped_patches(compared, paths, lambda: self._rest_compare(reviewed, self.head))
            changed = {item["filename"] for item in files
                       if item["filename"] in paths or item.get("previous_filename") in paths}
            if changed - {item["path"] for item in scoped if item["patch"]}:
                complete = False
            records["comparison"] = {
                "url": f"https://github.com/{self.repo}/compare/{reviewed}...{self.head}",
                "status": compared["status"],
                "patches": scoped,
                "files_in_comparison": len(files), "complete": complete,
                "commits_dated_at_or_before_publication": early_count,
                "later_commits_not_shown": max(0, len(later_shas) - MAX_COMMITS),
            }
        merge = self.commit(self.merge)
        for name, revision, verified in (("reviewed", reviewed, compared or self.commit(reviewed)),
                                         ("merged", self.merge, merge)):
            if revision and verified:
                for i, path in enumerate(paths):
                    records[f"{name}-{i}"] = {
                        "url": blob_url(self.repo, revision, path), "path": path,
                        **self.file(revision, path),
                    }
        if merge:
            records["merge-commit"] = {
                "url": f"https://github.com/{self.repo}/commit/{self.merge}",
                "patches": self.scoped_patches(merge, paths, lambda: self._rest_commit(self.merge)),
            }
        for i, (revision, timestamp) in enumerate(later_shas[-MAX_COMMITS:]):
            commit = self.commit(revision)
            relevant = self.scoped_patches(commit, paths, lambda: self._rest_commit(revision)) if commit else []
            if any(item["patch"] for item in relevant):
                records[f"later-{i}"] = {
                    "url": f"https://github.com/{self.repo}/commit/{revision}",
                    "committed_at": timestamp, "patches": relevant,
                }
        return records


def evidence_paths(thread, changed):
    path = thread["path"]
    original = PurePosixPath(path)
    text = " ".join([thread["body"], *(reply["body"] for reply in thread["replies"])]).lower()

    def priority(candidate):
        target = PurePosixPath(candidate)
        shared = 0
        for a, b in zip(original.parent.parts, target.parent.parts):
            if a != b:
                break
            shared += 1
        stem = original.stem.lower()
        similar = len(stem) >= 3 and stem in target.stem.lower()
        return (candidate.lower() not in text, not similar, -shared, candidate)

    related = sorted(changed - {path}, key=priority)
    return [path, *related[:PATH_LIMIT - 1]], max(0, len(related) - PATH_LIMIT + 1)


def prepare(pull, evidence):
    changed = evidence.changed_paths()
    records, indexes, threads = [], {}, {}
    for thread in pull["threads"]:
        paths, omitted = evidence_paths(thread, changed)
        refs = {}
        for key, value in evidence.for_thread(thread, paths).items():
            name = digest(value)
            if name not in indexes:
                indexes[name] = len(records)
                records.append(value)
            refs[key] = indexes[name]
        threads[thread["id"]] = {"paths": paths, "omitted_related_paths": omitted, "records": refs}
    return {"version": PREPARED_VERSION, "input": digest(pull), "threads": threads, "records": records}


class PreparedEvidence:
    def __init__(self, bundle):
        self.bundle = bundle

    def paths(self):
        return {key: item["paths"] for key, item in self.bundle["threads"].items()}

    def for_thread(self, thread, paths):
        item = self.bundle["threads"][thread["id"]]
        records = {}
        for key, index in item["records"].items():
            value = self.bundle["records"][index]
            if "path" in value and value["path"] not in paths:
                continue
            if "patches" in value:
                scoped = [patch for patch in value["patches"]
                          if patch["path"] in paths or patch["previous_path"] in paths]
                if key.startswith("later-") and not scoped:
                    continue
                value = dict(value, patches=scoped)
            records[key] = value
        return records
