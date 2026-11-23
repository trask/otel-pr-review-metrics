import json
from functools import lru_cache
from hashlib import sha256
from pathlib import PurePosixPath

from github_data import blob_url, instant
from git_evidence import GitEvidence, GitUnavailable, object_id


PREPARED_VERSION = 1
MAX_PATHS = 4
MAX_COMMITS = 12
UNREADABLE = (GitUnavailable, UnicodeError)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return sha256(json_bytes(value)).hexdigest()


def bounded(text, limit):
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


class Evidence:
    def __init__(self, git_root, pull):
        self.pull = pull
        self.repo = pull["repo"]
        self.merge = object_id(pull["merge"])
        self.head = object_id(pull["head"])
        self.git = GitEvidence(git_root, self.repo, pull["number"])
        self.commit = lru_cache(maxsize=64)(self._commit)
        self.compare = lru_cache(maxsize=24)(self._compare)
        self.file = lru_cache(maxsize=128)(self._file)

    def _commit(self, revision):
        if not revision:
            return None
        try:
            return self.git.commit(revision)
        except UNREADABLE:
            return None

    def _compare(self, start, end):
        try:
            return self.git.compare(start, end)
        except UNREADABLE:
            return None

    def _file(self, revision, path):
        try:
            content = self.git.file(revision, path)
        except UNREADABLE:
            return {"state": "unavailable"}
        return self.file_state(content) if content is not None else {"state": "absent"}

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

    def scoped_patches(self, item, paths):
        def patch(path):
            try:
                return bounded(self.git.patch(*item["git_range"], path), 10_000)
            except UNREADABLE:
                return None

        return [
            {"path": file["filename"], "previous_path": None, "status": file["status"],
             "patch": patch(file["filename"])}
            for file in item["files"] if file["filename"] in paths
        ]

    def changed_paths(self):
        revisions = set(filter(None, (object_id(t["reviewed"]) for t in self.pull["threads"])))
        self.git.prepare({*revisions, self.head, self.merge})
        files = list((self.commit(self.merge) or {}).get("files", []))
        if self.head:
            for reviewed in sorted(revisions):
                files += (self.compare(reviewed, self.head) or {}).get("files", [])
        return {item["filename"] for item in files}

    def for_thread(self, thread, paths):
        reviewed = object_id(thread["reviewed"])
        records = {
            f"reply-{i}": {"url": reply["url"]}
            for i, reply in enumerate(thread["replies"])
        }
        compared = self.compare(reviewed, self.head) if reviewed and self.head else None
        later_shas = []
        early_count = 0
        if compared:
            complete = compared["status"] in {"ahead", "identical"}
            opened, merged = instant(thread["opened"]), instant(self.pull["merged"])
            for item in compared["commits"]:
                committed = instant(item["commit"]["committer"]["date"])
                if opened < committed <= merged and object_id(item["sha"]):
                    later_shas.append((item["sha"], item["commit"]["committer"]["date"]))
                elif committed <= opened:
                    early_count += 1
                elif committed > merged:
                    complete = False
            scoped = self.scoped_patches(compared, paths)
            if any(not item["patch"] for item in scoped):
                complete = False
            records["comparison"] = {
                "url": f"https://github.com/{self.repo}/compare/{reviewed}...{self.head}",
                "status": compared["status"],
                "patches": scoped,
                "files_in_comparison": len(compared["files"]), "complete": complete,
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
                "patches": self.scoped_patches(merge, paths),
            }
        for i, (revision, timestamp) in enumerate(later_shas[-MAX_COMMITS:]):
            commit = self.commit(revision)
            relevant = self.scoped_patches(commit, paths) if commit else []
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
    return [path, *related[:MAX_PATHS - 1]], max(0, len(related) - MAX_PATHS + 1)


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
