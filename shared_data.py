import base64
import gzip
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from archive import decode, encode
from github_data import DataError, instant, run_command
from git_evidence import object_id
from prepared import PREPARED_VERSION, digest, json_bytes


SHARED_REMOTE = "https://github.com/trask/otel-pr-review-metrics.git"
DATA_REF = "refs/heads/data"
ARCHIVE_NAME = "archive.sqlite3"
MAX_LEGACY_SNAPSHOT = 95_000_000
MANIFEST = "schema.json"
FORMAT = {"format": 1}
PR_FILE = re.compile(r"(\d{4}-\d{2})/([1-9]\d*)(\.json|\.evidence\.json\.gz)")
MAX_RECORD_BYTES = 16_000_000
TABLES = {
    "pulls": ("repo, number, merged_day, merged, payload", (0, 1)),
    "complete_days": ("repo, day", (0, 1)),
    "evidence": ("request, payload", (0,)),
}
PROBABLE_SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s+>]*[A-Za-z0-9+/]{24,}"
    rb"|\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{70,}|"
    rb"(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[0-9A-Za-z-]{16,}|AIza[0-9A-Za-z_-]{35})\b"
    rb"|(?i:\bbearer )[A-Za-z0-9._~-]{32,}"
)
URL_AUTH = re.compile(rb"""https?://[^\s:/"\\@]+:([^\s/"\\@]+)@""")
PLACEHOLDER = re.compile(rb"(?i)(?:pass(?:word)?|token|secret|xxx+|\$\{?[\w.-]+\}?|\*+)")


def public_revision_request(key, repositories):
    url = urlsplit(key)
    fields = url.path.split("/", 4)
    if url.scheme or url.netloc or url.fragment or len(fields) != 5 or fields[0] != "repos":
        return False
    if "/".join(fields[1:3]) not in repositories:
        return False
    query = parse_qs(url.query)
    kind, resource = fields[3:]
    if kind == "contents":
        return bool(resource and query.keys() == {"ref"} and len(query["ref"]) == 1
                    and object_id(query["ref"][0]))
    if query.keys() != {"page", "per_page"} or any(
        len(values) != 1 or not re.fullmatch(r"[1-9][0-9]{0,5}", values[0])
        for values in query.values()
    ):
        return False
    if kind == "commits":
        return bool(object_id(resource))
    if kind == "compare":
        ids = resource.split("...")
        return len(ids) == 2 and all(map(object_id, ids))
    return False


def safe_payload(payload):
    data = decode(payload)
    if data is None:
        return False

    def strings(value):
        if isinstance(value, str):
            yield value.encode("utf-8")
        elif isinstance(value, dict):
            for child in value.values():
                yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    def safe(text):
        return not PROBABLE_SECRET.search(text) and all(
            PLACEHOLDER.fullmatch(match.group(1)) for match in URL_AUTH.finditer(text)
        )

    if isinstance(data, dict) and data.get("encoding") == "base64":
        content = data.get("content")
        if not isinstance(content, str) or not safe(base64.b64decode(content)):
            return False
    return all(map(safe, strings(data)))


class SharedData:
    def __init__(self, archive, repositories, *, remote=SHARED_REMOTE, cwd=None):
        self.archive = archive
        self.repositories = set(repositories)
        self.remote = remote
        self.cwd = cwd
        self.tip = None
        self.imported = {}
        self.legacy = False

    def git(self, *args, binary=False, input=None):
        result = run_command(["git", "--no-pager", *args], binary=binary, input=input, cwd=self.cwd)
        if result.returncode:
            error = result.stderr.decode("utf-8", errors="replace") if binary else result.stderr
            raise DataError(f"Shared data branch: git {args[0]} failed: {error.strip()}")
        return result.stdout if binary else result.stdout.strip()

    def rows(self, db, table):
        columns, keys = TABLES[table]
        key_columns = columns.split(", ")
        ordering = ", ".join(key_columns[i] for i in keys)
        yield from db.execute(f"SELECT {columns} FROM {table} ORDER BY {ordering}")

    def allowed(self, table, row):
        if table == "evidence":
            return public_revision_request(row[0], self.repositories)
        return row[0] in self.repositories

    def blobs(self, entries):
        for offset in range(0, len(entries), 32):
            batch = entries[offset:offset + 32]
            request = "".join(f"{oid}\n" for _, oid in batch).encode()
            output = self.git("cat-file", "--batch", binary=True, input=request)
            position = 0
            for path, oid in batch:
                boundary = output.find(b"\n", position)
                header = output[position:boundary].decode().split()
                if boundary < 0 or len(header) != 3 or header[:2] != [oid, "blob"]:
                    raise DataError(f"Unexpected Git blob for {path}")
                size = int(header[2])
                position = boundary + 1
                end = position + size
                if size > MAX_RECORD_BYTES or output[end:end + 1] != b"\n":
                    raise DataError(f"Invalid shared record size for {path}")
                yield path, output[position:end]
                position = end + 1
            if position != len(output):
                raise DataError("Unexpected extra data from Git")

    def load(self):
        self.imported = {}
        tip = self.git("ls-remote", "--exit-code", self.remote, DATA_REF).partition("\t")[0]
        if not object_id(tip):
            raise DataError("Shared data branch did not return a commit ID")
        self.git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", self.remote, tip)
        listing = self.git("ls-tree", "-rlz", tip, binary=True)
        entries = {}
        for line in listing.rstrip(b"\0").split(b"\0"):
            attributes, _, path = line.partition(b"\t")
            fields = attributes.decode().split()
            if len(fields) != 4 or fields[:2] != ["100644", "blob"] or not object_id(fields[2]):
                raise DataError("Shared data branch contains an unexpected Git entry")
            entries[path.decode()] = (fields[2], int(fields[3]))
        self.legacy = len(entries) == 1 and any(name in entries for name in (ARCHIVE_NAME, ARCHIVE_NAME + "\r"))
        if self.legacy:
            oid, size = next(iter(entries.values()))
            if size > MAX_LEGACY_SNAPSHOT:
                raise DataError("Legacy shared archive exceeds the snapshot size limit")
            self.load_legacy(self.git("cat-file", "blob", oid, binary=True))
        else:
            if MANIFEST not in entries or any(size > MAX_RECORD_BYTES for _, size in entries.values()):
                raise DataError("Shared data branch is missing a manifest or contains an oversized file")
            ordered = sorted(entries, key=lambda path: (path != MANIFEST, path.endswith(".gz"), path))
            with self.archive.db:
                for path, blob in self.blobs([(path, entries[path][0]) for path in ordered]):
                    self.load_record(path, blob)
                    self.imported[path] = sha256(blob).digest()
        self.tip = tip

    @staticmethod
    def pull_file(repo, day, number):
        return f"{repo}/{day[:7]}/{number}.json"

    @staticmethod
    def text_record(value):
        return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def load_legacy(self, blob):
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / ARCHIVE_NAME
            path.write_bytes(blob)
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as incoming:
                incoming.execute("PRAGMA trusted_schema=OFF")
                with self.archive.db:
                    for table, (columns, _) in TABLES.items():
                        for row in self.rows(incoming, table):
                            if not self.allowed(table, row):
                                raise DataError(f"Unexpected repository or endpoint in shared {table}")
                            if table != "complete_days":
                                decode(row[-1])
                            if table == "pulls":
                                name = self.pull_file(row[0], row[2], row[1])
                                self.imported[name] = sha256(self.text_record(decode(row[-1]))).digest()
                            values = ",".join(["?"] * len(row))
                            self.archive.db.execute(
                                f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({values})", row
                            )

    def load_record(self, path, blob):
        if path == MANIFEST:
            if json.loads(blob) != FORMAT:
                raise DataError("Unsupported shared data format")
            return
        parts = path.split("/", 2)
        if len(parts) != 3:
            raise DataError(f"Unexpected shared data file: {path}")
        owner, name, relative = parts
        repo = f"{owner}/{name}"
        if repo not in self.repositories:
            raise DataError(f"Unexpected repository in shared data: {path}")
        if relative == "coverage.json":
            value = json.loads(blob)
            if not isinstance(value, list) or any(date.fromisoformat(day).isoformat() != day for day in value):
                raise DataError(f"Invalid date coverage: {path}")
            self.archive.db.executemany("INSERT OR IGNORE INTO complete_days VALUES (?, ?)",
                                       ((repo, day) for day in value))
            return
        parsed = PR_FILE.fullmatch(relative)
        if not parsed:
            raise DataError(f"Unexpected shared data file: {path}")
        month, number, suffix = parsed.group(1), int(parsed.group(2)), parsed.group(3)
        if suffix == ".json":
            pull = json.loads(blob)
            merged = instant(pull["merged"]).astimezone(timezone.utc).date().isoformat()
            if pull["repo"] != repo or pull["number"] != number or not merged.startswith(month + "-"):
                raise DataError(f"Inconsistent shared pull request: {path}")
            self.archive.db.execute("INSERT OR REPLACE INTO pulls VALUES (?, ?, ?, ?, ?)",
                                    (repo, number, merged, pull["merged"], encode(pull)))
            return
        row = self.archive.db.execute("SELECT merged_day, payload FROM pulls WHERE repo = ? AND number = ?",
                                     (repo, number)).fetchone()
        with gzip.GzipFile(fileobj=BytesIO(blob)) as contents:
            payload = contents.read(MAX_RECORD_BYTES + 1)
        if len(payload) > MAX_RECORD_BYTES:
            raise DataError(f"Oversized prepared evidence: {path}")
        bundle = json.loads(payload)
        if bundle["version"] != PREPARED_VERSION:
            raise DataError(f"Unsupported prepared evidence version: {path}")
        if not row or not row[0].startswith(month + "-") or digest(decode(row[1])) != bundle["input"]:
            raise DataError(f"Prepared evidence doesn't match its pull request: {path}")
        self.archive.db.execute("INSERT OR REPLACE INTO prepared_evidence VALUES (?, ?, ?, ?, ?)",
                                (repo, number, bundle["version"], bundle["input"], encode(bundle)))

    def local_records(self):
        yield MANIFEST, self.text_record(FORMAT), None
        for repo in sorted(self.repositories):
            complete = self.archive.db.execute("SELECT day FROM complete_days WHERE repo = ? ORDER BY day",
                                              (repo,))
            yield f"{repo}/coverage.json", self.text_record([day for (day,) in complete]), None
            rows = self.archive.db.execute("""
                SELECT p.number, p.merged_day, p.payload, e.input_digest, e.payload
                FROM pulls p LEFT JOIN prepared_evidence e
                 ON e.repo = p.repo AND e.number = p.number AND e.version = ?
                WHERE p.repo = ? ORDER BY p.merged_day, p.number
            """, (PREPARED_VERSION, repo))
            for number, day, raw, input_digest, bundle in rows:
                pull = decode(raw)
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or number < 1:
                    raise DataError("Invalid local PR identity")
                name = self.pull_file(repo, day, number)
                yield name, self.text_record(pull), raw
                if bundle and input_digest == digest(pull):
                    data = json_bytes(decode(bundle))
                    if len(data) > MAX_RECORD_BYTES:
                        raise DataError(f"Prepared evidence is too large for {repo}#{number}")
                    yield name.removesuffix(".json") + ".evidence.json.gz", gzip.compress(data, mtime=0), bundle

    def publish(self):
        if self.tip is None:
            raise DataError("Load the current data branch before publishing")
        ref = f"refs/scorecard/data-{uuid4().hex}"
        identity = self.git("var", "GIT_COMMITTER_IDENT")
        message = (b"Update public PR review data\n\n"
                   b"Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>\n")
        stream = bytearray(f"commit {ref}\ncommitter {identity}\ndata {len(message)}\n".encode())
        stream.extend(message)
        stream.extend(f"\nfrom {self.tip}\n".encode())
        if self.legacy:
            stream.extend(b"deleteall\n")
        seen = {}
        for path, blob, payload in self.local_records():
            seen[path] = sha256(blob).digest()
            if len(blob) > MAX_RECORD_BYTES:
                raise DataError(f"Shared record is too large: {path}")
            changed = seen[path] != self.imported.get(path)
            if payload is not None and changed and not safe_payload(payload):
                raise DataError(f"Shared publication withheld: {path} did not pass the public-data check.")
            if not (self.legacy or changed):
                continue
            stream.extend(f"M 100644 inline {path}\ndata {len(blob)}\n".encode())
            stream.extend(blob)
            stream.extend(b"\n")
        if not self.legacy and seen == self.imported:
            return None
        if not self.legacy:
            for path in sorted(self.imported.keys() - seen.keys()):
                stream.extend(f"D {path}\n".encode())
        stream.extend(b"\ndone\n")
        try:
            self.git("fast-import", "--quiet", "--done", binary=True, input=bytes(stream))
            commit = self.git("rev-parse", ref)
            self.git("push", "--quiet", self.remote, f"{commit}:{DATA_REF}")
            return commit
        finally:
            self.git("update-ref", "-d", ref)
