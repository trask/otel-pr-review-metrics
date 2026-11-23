import argparse
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from string import Template
from subprocess import TimeoutExpired

from archive import DEFAULT_ARCHIVE, Archive, ArchivedGitHub
from assessment import (
    ASSESSMENT_VERSION, DEFAULT_REASONING_EFFORT, KINDS, OUTCOMES, REASONING_EFFORTS, Copilot, Evidence, assess,
)
from github_data import DataError, GitHub
from prepared import digest, prepare
from shared_data import SharedData, judgment_key


REPOS = (
    "open-telemetry/semantic-conventions-genai",
    "open-telemetry/opentelemetry-python-genai",
    "open-telemetry/semantic-conventions-conformance",
    "open-telemetry/opentelemetry-java-instrumentation",
)
BASELINE = (date(2026, 10, 1), date(2026, 11, 15))
COPILOT = {"copilot-pull-request-reviewer", "copilot-pull-request-reviewer[bot]"}
GROUPS = ("copilot", "custom", "human", "outside")
AUTOMATED = ("copilot", "custom")
MAIN_GROUPS = (*AUTOMATED, "human")
UNMEASURED = "N/M"
NO_DENOMINATOR = "N/A"
TEMPLATE = Path(__file__).with_name("scorecard.html")
COMMAND_ERRORS = (DataError, OSError, TimeoutExpired, sqlite3.Error, ValueError)


def identity(author, custom, outside):
    if not author:
        return "outside"
    login = author["login"].lower()
    if login in COPILOT:
        return "copilot"
    if login in custom:
        return "custom"
    if (login in outside or author["__typename"] != "User" or login.endswith("[bot]")):
        return "outside"
    return "human"


@dataclass
class Stats:
    prs: int = 0
    missing: bool = False
    unknown_roots: int = 0
    threads: Counter = field(default_factory=Counter)
    pending: Counter = field(default_factory=Counter)
    outcomes: Counter = field(default_factory=Counter)
    findings: Counter = field(default_factory=Counter)

    def collect(self, pull, rows, custom, other_bots):
        self.prs += 1
        self.unknown_roots += pull["missing_roots"]
        by_id = {row["id"]: row for row in rows} if rows is not None else {}
        for thread in pull["threads"]:
            group = identity(thread["author"], custom, other_bots)
            self.threads[group] += 1
            row = by_id.get(thread["id"])
            if row is None:
                self.pending[group] += 1
                continue
            self.outcomes[group, row["status"]] += 1
            if row["kind"]:
                self.findings[group, row["kind"], row["status"]] += 1

    def __add__(self, other):
        return Stats(
            self.prs + other.prs, self.missing or other.missing,
            self.unknown_roots + other.unknown_roots,
            self.threads + other.threads, self.pending + other.pending,
            self.outcomes + other.outcomes, self.findings + other.findings,
        )

    def total(self, groups):
        if self.missing or self.unknown_roots:
            return None
        return sum(self.threads[g] for g in groups)

    def measured(self, groups):
        return self.total(groups) is not None and not any(self.pending[g] for g in groups)

    def outcome(self, groups, status):
        if self.measured(groups):
            return sum(self.outcomes[g, status] for g in groups)
        return None

    def finding(self, groups, status=None, kind=None):
        if self.measured(groups):
            return sum(
                n for (group, finding_kind, outcome), n in self.findings.items()
                if group in groups and status in (None, outcome) and kind in (None, finding_kind)
            )
        return None

    def rate(self):
        findings = self.finding(MAIN_GROUPS)
        if findings is None:
            return None
        return f"{findings / self.prs:,.2f}" if self.prs else NO_DENOMINATOR


def cell(value):
    if value is None:
        return f'<td class="missing"><abbr title="Not measured">{UNMEASURED}</abbr></td>'
    if value == NO_DENOMINATOR:
        return f'<td class="missing"><abbr title="No merged PRs">{NO_DENOMINATOR}</abbr></td>'
    text = f"{value:,}" if isinstance(value, int) else value
    return f"<td>{escape(text)}</td>"


def row(label, values, css=""):
    return (f'<tr class="{css}"><th scope="row">{escape(label)}</th>'
            + "".join(cell(value) for value in values) + "</tr>")


def table(caption, head, body):
    return (f'<div class="table-wrap" tabindex="0" role="region" aria-label="{escape(caption)}">'
            f"<table><caption>{escape(caption)}</caption><thead>{head}</thead><tbody>{body}</tbody>"
            "</table></div>")


def headers(labels):
    return "".join(f'<th scope="col">{escape(label)}</th>' for label in labels)


def findings_header(lead):
    return (
        "<tr>" + "".join(f'<th scope="col" rowspan="2">{escape(label)}</th>' for label in lead)
        + '<th scope="colgroup" colspan="2" class="automated">Automated findings</th>'
        + '<th scope="colgroup" colspan="2">Human findings</th></tr><tr>'
        + headers(["Addressed", "Unaddressed"] * 2) + "</tr>"
    )


def main_table(label, repos, total):
    def values(stats):
        return [None if stats.missing else stats.prs, stats.rate()] + [
            stats.finding(groups, status)
            for groups in (AUTOMATED, ("human",)) for status in OUTCOMES[:2]
        ]

    body = "".join(row(repo.split("/")[1], values(stats)) for repo, stats in repos.items())
    return table(label, findings_header(["Repository", "Merged PRs", "Findings per merged PR"]),
                 body + row("All repositories", values(total), "total"))


def types_table(label, total):
    body = "".join(row(kind.capitalize(), [
        total.finding(groups, status, kind)
        for groups in (AUTOMATED, ("human",)) for status in OUTCOMES[:2]
    ]) for kind in KINDS)
    return table(label, findings_header(["Finding type"]), body)


def threads_table(label, total, custom):
    def values(groups):
        return [total.outcome(groups, status) for status in OUTCOMES] + [total.total(groups)]

    body = (
        row("Automated total", values(AUTOMATED), "automated total")
        + row("GitHub Copilot", values(("copilot",)), "child")
        + row("Custom reviewer", values(("custom",)) if custom else [None] * 6, "child")
        + row("Human", values(("human",)))
    )
    if total.threads["outside"] or total.total(GROUPS) is None:
        body += row("Other automation / unknown authors", values(("outside",)))
    body += row("All inline review threads", values(GROUPS), "total")
    return table(label, "<tr>" + headers(["Reviewer", *(item.capitalize() for item in OUTCOMES), "Total"])
                 + "</tr>", body)


@dataclass
class Cohort:
    name: str
    dates: tuple[date, date]
    repos: dict
    note: str = ""

    @property
    def total(self):
        return sum(self.repos.values(), start=Stats())

    @property
    def label(self):
        return f"{self.name} | {self.dates[0]} to {self.dates[1]} UTC"


def render(cohorts, custom, model, version=ASSESSMENT_VERSION, reasoning_effort=DEFAULT_REASONING_EFFORT):
    overview, main, types, threads = [], [], [], []
    for window in cohorts:
        stats = window.total
        counted = sum(stats.threads.values())
        assessed = counted - sum(stats.pending.values())
        note = window.note or f"{assessed:,} of {counted:,} visible thread openers assessed."
        if not window.note and stats.missing:
            note += " This window includes days that capture has not finalized."
        if not window.note and stats.unknown_roots:
            note += " GitHub did not return some thread openers."
        overview.append(f'<article class="window"><h2>{escape(window.name)}</h2>'
                        f'<p class="dates">{window.dates[0]} to {window.dates[1]} UTC</p>'
                        f"<p>{escape(note)}</p></article>")
        main.append(main_table(window.label, window.repos, stats))
        types.append(types_table(window.label, stats))
        threads.append(threads_table(window.label, stats, custom))
    identity_note = ("Custom reviewer logins: " + ", ".join(sorted(custom)) + "."
                     if custom else "No custom reviewer was configured for this baseline. "
                     "The Automated total uses GitHub Copilot only.")
    return Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        overview="".join(overview), main="".join(main), types="".join(types),
        threads="".join(threads), identity_note=escape(identity_note),
        model=escape(f"{model} ({reasoning_effort or 'unpinned'} reasoning effort)"),
        version=escape(version[:12]),
    )


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def assess_cached(archive, bundle, model, pull, audit, allow_model, version):
    repo, number = pull["repo"], pull["number"]

    def fingerprint(inputs):
        return judgment_key(model.model, inputs, model.reasoning_effort)

    key = fingerprint([version, pull, digest(bundle)])
    saved = (archive.get_judgment(repo, number, "published", key)
             or archive.get_judgment(repo, number, "complete", key))
    if saved is None and not allow_model and model.reasoning_effort is None and version != ASSESSMENT_VERSION:
        saved = archive.get_judgment(repo, number, "complete", fingerprint([version, pull]))
    if saved is not None:
        return saved["threads"], False
    if not allow_model or bundle is None:
        return None, False

    def checkpoint(stage, inputs, evaluate):
        digest = fingerprint(inputs)
        result = archive.get_judgment(repo, number, stage, digest)
        if result is None:
            result = evaluate()
            archive.put_judgment(repo, number, stage, digest, result)
        return result

    rows = assess(bundle, model, pull, checkpoint)
    result = {
        "input_digest": key, "assessment_version": version,
        "model": model.model, "reasoning_effort": model.reasoning_effort,
        "merge": pull["merge"], "evidence_digest": digest(bundle), "threads": rows,
    }
    write_atomic(audit / repo / str(number) / f"{key}.json", json.dumps(result, indent=2) + "\n")
    archive.put_judgment(repo, number, "complete", key, result)
    return rows, True


def log(message):
    print(message, file=sys.stderr, flush=True)


def load_shared(archive):
    shared = SharedData(archive, REPOS)
    shared.load()
    log(f"Loaded shared data from {shared.tip[:12]}")
    return shared


def publish_shared(shared, *, include_assessments=False):
    commit = shared.publish(include_assessments=include_assessments)
    log(f"Published shared data as {commit[:12]}" if commit else "Shared data is already up to date")


def collect(archive, args, model, windows, shared=None):
    cohorts, failures, runs = [], 0, 0
    completed = 0
    for label, dates in windows:
        repos = {}
        for repo in REPOS:
            stats = repos[repo] = Stats(missing=bool(archive.pending_days(repo, *dates)))
            for pull in archive.pulls(repo, *dates):
                rows = []
                if pull["threads"]:
                    allow = args.command == "assess" and (args.max_prs is None or runs < args.max_prs)
                    try:
                        rows, ran = assess_cached(
                            archive, archive.get_prepared(pull), model, pull, args.audit_dir,
                            allow, args.assessment_version,
                        )
                    except (DataError, OSError, TimeoutExpired, ValueError) as error:
                        log(f"{repo}#{pull['number']}: assessment failed: {error}")
                        rows = None
                        failures += 1
                        runs += 1
                    else:
                        if ran:
                            runs += 1
                            completed += 1
                            if shared is not None and completed % 25 == 0:
                                publish_shared(shared, include_assessments=True)
                stats.collect(pull, rows, args.custom_reviewer, args.other_bot)
            log(f"{label} | {repo}: {sum(stats.threads.values())} visible threads, "
                f"{sum(stats.pending.values())} not assessed, {stats.unknown_roots} missing roots")
        cohorts.append(Cohort(label, dates, repos))
    return cohorts, failures


def load_data(args, _parser):
    try:
        with closing(Archive(args.archive)) as archive:
            load_shared(archive)
    except COMMAND_ERRORS as error:
        log(f"Load stopped: {error}")
        return 1
    return 0


def capture_data(args, parser):
    today = datetime.now(timezone.utc).date()
    if args.max_rate_wait < 0:
        parser.error("--max-rate-wait cannot be negative")
    if args.max_evidence_prs is not None and (not args.with_evidence or args.max_evidence_prs < 1):
        parser.error("--max-evidence-prs requires --with-evidence and a positive limit")
    through = args.through or today
    if not args.start <= through <= today:
        parser.error("capture dates must be in order and no later than today UTC")
    github = GitHub(args.max_rate_wait)
    try:
        with closing(Archive(args.archive)) as archive:
            shared = load_shared(archive) if args.shared or args.publish_data else None
            for repo in REPOS:
                log(f"Capturing {repo}")
                added = archive.capture(github, repo, args.start, through, today)
                log(f"{repo}: {added} new PRs archived")
            if args.with_evidence:
                total_prepared = 0
                for repo in REPOS:
                    count = 0
                    evidence = ArchivedGitHub(archive, github, repo, args.archive.parent / "git")
                    for pull in archive.pulls(repo, args.start, through):
                        if not pull["threads"] or archive.get_prepared(pull) is not None:
                            continue
                        if args.max_evidence_prs is not None and total_prepared >= args.max_evidence_prs:
                            break
                        bundle = prepare(pull, Evidence(evidence, pull))
                        archive.put_prepared(repo, pull["number"], bundle)
                        count += 1
                        total_prepared += 1
                        log(f"Prepared {repo}#{pull['number']}")
                    log(f"{repo}: {count} new evidence bundles")
            if args.publish_data:
                publish_shared(shared)
    except COMMAND_ERRORS as error:
        log(f"Capture stopped: {error}")
        return 1
    print(args.archive)
    return 0


def build_report(args, parser):
    assessing = args.command == "assess"
    args.custom_reviewer = {login.lower() for login in args.custom_reviewer}
    args.other_bot = {login.lower() for login in args.other_bot}
    if assessing:
        args.audit_dir = args.audit_dir or args.archive.parent / "audit"
    if args.custom_reviewer & (args.other_bot | COPILOT):
        parser.error("a custom reviewer login cannot also be another bot or GitHub Copilot")
    if assessing and args.max_prs is not None and args.max_prs < 0:
        parser.error("--max-prs cannot be negative")
    dates = tuple(args.window)
    if dates[0] > dates[1]:
        parser.error("window dates must be in order")
    windows = [("Baseline" if dates == BASELINE else "Selected window", dates)]
    if args.follow_up:
        start, end = args.follow_up
        if start <= dates[1] or start > end:
            parser.error("follow-up must start after the first window and end on or after its start date")
        if not args.custom_reviewer:
            parser.error("--follow-up requires --custom-reviewer LOGIN")
        windows.append(("Follow-up", (start, end)))
    try:
        publish = assessing and args.publish_data
        effort = None if args.reasoning_effort == "unpinned" else args.reasoning_effort
        if not args.archive.is_file() and not publish:
            raise DataError("Archive does not exist. Run `python scorecard.py load-data` or `capture` first.")
        with closing(Archive(args.archive, readonly=not assessing)) as archive:
            shared = load_shared(archive) if publish else None
            cohorts, failures = collect(archive, args, Copilot(args.model, effort), windows, shared)
            if shared is not None:
                publish_shared(shared, include_assessments=True)
    except COMMAND_ERRORS as error:
        log(str(error))
        return 1
    write_atomic(args.output, render(cohorts, args.custom_reviewer, args.model, args.assessment_version, effort))
    print(args.output)
    return int(bool(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build an aggregate scorecard from public GitHub PRs.")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="update the local archive without using a model")
    load = sub.add_parser("load-data", help="download the shared data branch without calling the GitHub API")
    assessment = sub.add_parser("assess", help="assess archived threads and render a report")
    report = sub.add_parser("report", help="render a report offline from the archive")
    for command in (capture, load, assessment, report):
        command.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)

    load.set_defaults(run=load_data)
    capture.set_defaults(run=capture_data)
    capture.add_argument("--from", dest="start", type=date.fromisoformat, default=BASELINE[0])
    capture.add_argument("--through", type=date.fromisoformat, help="last UTC day to capture (default: today)")
    capture.add_argument("--with-evidence", action="store_true", help="prepare PR evidence for offline assessment")
    capture.add_argument("--max-evidence-prs", type=int, help="cap new PR evidence bundles in this capture")
    capture.add_argument("--publish-data", action="store_true",
                         help="load and publish captured inputs on the public data branch")
    capture.add_argument("--max-rate-wait", type=int, default=60, metavar="SECONDS")
    capture.add_argument("--shared", action="store_true", help="seed the local archive from the data branch")

    for command, efforts in ((assessment, REASONING_EFFORTS), (report, (*REASONING_EFFORTS, "unpinned"))):
        command.set_defaults(run=build_report, assessment_version=ASSESSMENT_VERSION, audit_dir=None)
        command.add_argument("--window", nargs=2, type=date.fromisoformat, metavar=("START", "END"),
                             default=BASELINE, help="inclusive UTC merge dates (default: baseline)")
        command.add_argument("--follow-up", nargs=2, type=date.fromisoformat, metavar=("START", "END"))
        command.add_argument("--custom-reviewer", action="append", default=[], metavar="LOGIN")
        command.add_argument("--other-bot", action="append", default=[], metavar="LOGIN")
        command.add_argument("--model", default="gpt-6.2-sol", help="GitHub Copilot CLI model ID (default: %(default)s)")
        command.add_argument("--reasoning-effort", choices=efforts, default=DEFAULT_REASONING_EFFORT,
                             help="reasoning effort for the assessment (default: %(default)s)")
        command.add_argument("--output", type=Path, default=Path(".scorecard", "scorecard.html"))
    assessment.add_argument("--publish-data", action="store_true", help="load data and publish completed assessments")
    assessment.add_argument("--audit-dir", type=Path)
    assessment.add_argument("--max-prs", type=int, help="limit new assessments to this many PRs")
    report.add_argument("--assessment-version", help="full assessment_version from a saved audit file")

    args = parser.parse_args(argv)
    return args.run(args, parser)


if __name__ == "__main__":
    sys.exit(main())
