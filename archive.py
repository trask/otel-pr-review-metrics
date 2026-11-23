import gzip
import json
import sqlite3
from datetime import timedelta, timezone
from pathlib import Path

from github_data import DataError, GitHubError, endpoint, instant
from prepared import PREPARED_VERSION, digest


DEFAULT_ARCHIVE = Path.home() / ".otel-pr-review-metrics" / "archive.sqlite3"
DAY = timedelta(days=1)


def days(start, end):
    return (start + i * DAY for i in range((end - start).days + 1))


def windows(dates):
    start = previous = None
    for day in dates:
        if previous is None:
            start = day
        elif day != previous + DAY or (day - start).days == 46:
            yield start, previous
            start = day
        previous = day
    if previous is not None:
        yield start, previous


def encode(value):
    return gzip.compress(json.dumps(value, ensure_ascii=False).encode(), compresslevel=3)


def decode(value):
    return json.loads(gzip.decompress(value))


class Archive:
    def __init__(self, path, readonly=False):
        if readonly:
            if not path.is_file():
                raise DataError("Archive does not exist. Run `python scorecard.py capture` first.")
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pulls (
                repo TEXT, number INTEGER, merged_day TEXT, merged TEXT, payload BLOB,
                PRIMARY KEY (repo, number)
            );
            CREATE INDEX IF NOT EXISTS pulls_by_day ON pulls(repo, merged_day, number);
            CREATE TABLE IF NOT EXISTS complete_days (
                repo TEXT, day TEXT, PRIMARY KEY (repo, day)
            );
            CREATE TABLE IF NOT EXISTS evidence (
                request TEXT PRIMARY KEY, payload BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prepared_evidence (
                repo TEXT, number INTEGER, version INTEGER, input_digest TEXT, payload BLOB NOT NULL,
                PRIMARY KEY (repo, number, version)
            );
            CREATE TABLE IF NOT EXISTS judgments (
                repo TEXT, number INTEGER, stage TEXT, fingerprint TEXT, payload BLOB,
                PRIMARY KEY (repo, number, stage, fingerprint)
            );
        """)

    def close(self):
        self.db.close()

    def pending_days(self, repo, start, end):
        complete = {row[0] for row in self.db.execute(
            "SELECT day FROM complete_days WHERE repo = ? AND day BETWEEN ? AND ?",
            (repo, start.isoformat(), end.isoformat()),
        )}
        return [day for day in days(start, end) if day.isoformat() not in complete]

    def pulls(self, repo, start, end):
        for (payload,) in self.db.execute(
            "SELECT payload FROM pulls WHERE repo = ? AND merged_day BETWEEN ? AND ? ORDER BY number",
            (repo, start.isoformat(), end.isoformat()),
        ):
            yield decode(payload)

    def capture(self, github, repo, start, end, today):
        added = 0
        for first, last in windows(self.pending_days(repo, start, end)):
            expected = github.merged(repo, first, last)
            saved = dict(self.db.execute(
                "SELECT number, merged FROM pulls WHERE repo = ? AND merged_day BETWEEN ? AND ?",
                (repo, first.isoformat(), last.isoformat()),
            ))
            if any(n not in expected or instant(merged) != instant(expected[n])
                   for n, merged in saved.items()):
                raise DataError(f"Search disagrees with archived merges in {repo} for {first} to {last}; retry later")
            pending = {n: merged for n, merged in expected.items() if n not in saved}
            remaining = set(pending)
            for pull in github.pulls(repo, pending):
                n, merged = pull["number"], pull["merged"]
                if n not in remaining or instant(merged) != instant(pending[n]):
                    raise DataError(f"Unexpected archive record for {repo}#{n}")
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO pulls VALUES (?, ?, ?, ?, ?)",
                                    (repo, n, instant(merged).astimezone(timezone.utc).date().isoformat(),
                                     merged, encode(pull)))
                added += 1
                remaining.remove(n)
            if remaining:
                raise DataError(f"Missing pull request records in {repo} for {first} to {last}")
            # Search indexing can lag. Recheck today and yesterday on the next capture.
            with self.db:
                self.db.executemany(
                    "INSERT OR IGNORE INTO complete_days VALUES (?, ?)",
                    ((repo, day.isoformat()) for day in days(first, min(last, today - 2 * DAY))),
                )
        return added

    def get_prepared(self, pull):
        exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prepared_evidence'"
        ).fetchone()
        if not exists:
            return None
        row = self.db.execute(
            "SELECT input_digest, payload FROM prepared_evidence WHERE repo = ? AND number = ? AND version = ?",
            (pull["repo"], pull["number"], PREPARED_VERSION),
        ).fetchone()
        return decode(row[1]) if row and row[0] == digest(pull) else None

    def put_prepared(self, repo, number, bundle):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO prepared_evidence VALUES (?, ?, ?, ?, ?)",
                            (repo, number, bundle["version"], bundle["input"], encode(bundle)))

    def get_judgment(self, repo, number, stage, fingerprint):
        row = self.db.execute(
            "SELECT payload FROM judgments WHERE repo = ? AND number = ? AND stage = ? AND fingerprint = ?",
            (repo, number, stage, fingerprint),
        ).fetchone()
        return decode(row[0]) if row else None

    def put_judgment(self, repo, number, stage, fingerprint, value):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO judgments VALUES (?, ?, ?, ?, ?)",
                            (repo, number, stage, fingerprint, encode(value)))


class ArchivedGitHub:
    """Cache immutable revision requests for one verified public repository."""

    def __init__(self, archive, github, repo, git_root=None):
        self.archive = archive
        self.github = github
        self.repo = repo
        self.git_root = git_root
        self.verified = False

    def get(self, path, **params):
        if not path.startswith(f"repos/{self.repo}/"):
            raise DataError("Evidence request is outside the archived repository")
        key = endpoint(path, **dict(sorted(params.items())))
        row = self.archive.db.execute("SELECT payload FROM evidence WHERE request = ?", (key,)).fetchone()
        if row:
            return decode(row[0])
        if not self.verified:
            self.github.public(self.repo)
            self.verified = True
        try:
            item = self.github.get(path, **params)
        except GitHubError as error:
            if not error.not_found or not path.startswith(f"repos/{self.repo}/contents/"):
                raise
            item = None
        with self.archive.db:
            self.archive.db.execute("INSERT OR IGNORE INTO evidence VALUES (?, ?)", (key, encode(item)))
        return item
