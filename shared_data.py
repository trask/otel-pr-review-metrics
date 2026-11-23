import base64
import re
import sqlite3
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from archive import Archive, decode
from github_data import DataError, run_command
from git_evidence import object_id


SHARED_REMOTE = "https://github.com/trask/otel-pr-review-metrics.git"
DATA_REF = "refs/heads/data"
ARCHIVE_NAME = "archive.sqlite3"
MAX_SNAPSHOT = 95_000_000
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
        self.skipped = 0
        self.normalize_name = False

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

    def identity(self, table, row):
        return (table, *(row[i] for i in TABLES[table][1]))

    def fingerprint(self, row):
        return sha256(repr(row[:-1]).encode() + row[-1]).digest() if isinstance(row[-1], bytes) else row

    def load(self):
        tip = self.git("ls-remote", "--exit-code", self.remote, DATA_REF).partition("\t")[0]
        if not object_id(tip):
            raise DataError("Shared data branch did not return a commit ID")
        self.git("fetch", "--quiet", "--no-tags", "--no-write-fetch-head", self.remote, tip)
        listing = self.git("ls-tree", "-z", tip, binary=True)
        entry = re.fullmatch(rb"100644 blob ([0-9a-f]{40})\tarchive\.sqlite3(\r?)\0", listing)
        if not entry:
            raise DataError("Shared data branch must contain only archive.sqlite3")
        oid = entry[1].decode()
        self.normalize_name = bool(entry[2])
        if int(self.git("cat-file", "-s", oid)) > MAX_SNAPSHOT:
            raise DataError("Shared archive exceeds the snapshot size limit")
        blob = self.git("cat-file", "blob", oid, binary=True)
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
                            values = ",".join(["?"] * len(row))
                            self.archive.db.execute(
                                f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({values})", row
                            )
                            self.imported[self.identity(table, row)] = self.fingerprint(row)
        self.tip = tip

    def publish(self):
        if self.tip is None:
            raise DataError("Load the current data branch before publishing")
        changed = False
        self.skipped = 0
        with TemporaryDirectory() as scratch:
            path = Path(scratch) / ARCHIVE_NAME
            with closing(Archive(path)) as snapshot:
                with snapshot.db:
                    for table, (columns, _) in TABLES.items():
                        for row in self.rows(self.archive.db, table):
                            if not self.allowed(table, row):
                                continue
                            new = self.imported.get(self.identity(table, row)) != self.fingerprint(row)
                            if table == "evidence" and decode(row[-1]) is None:
                                continue
                            if new and table != "complete_days" and not safe_payload(row[-1]):
                                self.skipped += 1
                                continue
                            values = ",".join(["?"] * len(row))
                            snapshot.db.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})", row)
                            changed |= new
                    if self.skipped:
                        # Don't advertise coverage if one of the PRs for a day was withheld.
                        raise DataError(
                            f"Shared publication withheld: {self.skipped} record(s) did not pass the public-data check."
                        )
                snapshot.db.execute("PRAGMA journal_mode=DELETE")
            if not (changed or self.normalize_name):
                return None
            if path.stat().st_size > MAX_SNAPSHOT:
                raise DataError("Shared archive exceeds the snapshot size limit")
            blob = self.git("hash-object", "-w", "--no-filters", "--", str(path))
            tree = self.git("mktree", "-z", binary=True,
                            input=f"100644 blob {blob}\t{ARCHIVE_NAME}\0".encode()).decode().strip()
            commit = self.git("commit-tree", tree, "-p", self.tip, "-m", "Update public PR review data",
                              "-m", "Co-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>")
            self.git("push", "--quiet", self.remote, f"{commit}:{DATA_REF}")
            return commit
