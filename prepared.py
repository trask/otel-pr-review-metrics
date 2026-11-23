import json
from hashlib import sha256
from pathlib import PurePosixPath


PREPARED_VERSION = 1
PATH_LIMIT = 4


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value):
    return sha256(json_bytes(value)).hexdigest()


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
