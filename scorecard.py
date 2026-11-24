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

from archive import DEFAULT_ARCHIVE, Archive
from assessment import (ASSESSMENT_VERSION, KINDS, REASONING_EFFORTS, RETRIEVAL_VERSION, assess,
                        needs_classification_batches)
from copilot_model import DEFAULT_REASONING_EFFORT, AssessmentSetupError, Copilot
from github_data import DataError, GitHub
from outcome_evidence import OutcomeEvidence
from prepared import Evidence, SUPPORTED_PREPARED_VERSIONS, digest, prepare
from shared_data import SharedData, judgment_key


REPOS = (
    "open-telemetry/semantic-conventions-genai",
    "open-telemetry/opentelemetry-python-genai",
    "open-telemetry/semantic-conventions-conformance",
    "open-telemetry/opentelemetry-java-instrumentation",
)
BASELINE = (date(2026, 10, 1), date(2026, 11, 15))
COPILOT = {"copilot-pull-request-reviewer", "copilot-pull-request-reviewer[bot]"}
BOT_PR_AUTHORS = {"opentelemetrybot"}
AUTOMATED = ("copilot", "custom")
NO_DENOMINATOR = object()
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


def is_bot_pr_author(login, kind):
    login = (login or "").lower()
    return kind == "Bot" or login.endswith("[bot]") or login in BOT_PR_AUTHORS


@dataclass
class Stats:
    prs: int = 0
    missing: bool = False
    unknown_roots: int = 0
    threads: Counter = field(default_factory=Counter)
    pending: Counter = field(default_factory=Counter)
    findings: Counter = field(default_factory=Counter)
    excluded_bot_prs: int = 0

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
            if row["kind"]:
                self.findings[group, row["kind"], row["status"]] += 1

    def __add__(self, other):
        return Stats(
            self.prs + other.prs, self.missing or other.missing,
            self.unknown_roots + other.unknown_roots,
            self.threads + other.threads, self.pending + other.pending,
            self.findings + other.findings,
            self.excluded_bot_prs + other.excluded_bot_prs,
        )

    def measured(self, groups):
        return not self.missing and not self.unknown_roots and not any(self.pending[g] for g in groups)

    def finding(self, groups, status=None, kind=None):
        if self.measured(groups):
            return sum(
                n for (group, finding_kind, outcome), n in self.findings.items()
                if group in groups and status in (None, outcome) and kind in (None, finding_kind)
            )
        return None

    def findings_per_pr(self, groups, kind=None):
        findings = self.finding(groups, kind=kind)
        if findings is None:
            return None
        return f"{findings / self.prs:,.2f}" if self.prs else NO_DENOMINATOR

    def outcome_pct(self, groups, status, kind=None):
        findings = self.finding(groups, kind=kind)
        if findings is None:
            return None
        return f"{self.finding(groups, status, kind) / findings:.1%}" if findings else NO_DENOMINATOR


def cell(value):
    if value is None or value is NO_DENOMINATOR:
        reason = "Not measured" if value is None else "No denominator for this rate"
        return f'<td class="missing" aria-label="{reason}" title="{reason}">&mdash;</td>'
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
        + '<th scope="colgroup" colspan="3" class="automated">Automated findings</th>'
        + '<th scope="colgroup" colspan="3">Human findings</th></tr><tr>'
        + headers(["Findings per merged PR", "% unaddressed", "% unclear"] * 2) + "</tr>"
    )


def finding_rates(stats, kind=None):
    return [
        value for groups in (AUTOMATED, ("human",))
        for value in (stats.findings_per_pr(groups, kind), stats.outcome_pct(groups, "unaddressed", kind),
                      stats.outcome_pct(groups, "unclear", kind))
    ]


def main_table(label, repos, total):
    def values(stats):
        return [None if stats.missing else stats.prs, *finding_rates(stats)]

    body = "".join(row(repo.split("/")[1], values(stats)) for repo, stats in repos.items())
    head = findings_header(["Repository", "Merged PRs"])
    return table(label, head, body + row("All repositories", values(total), "total"))


def types_table(label, total):
    body = "".join(row(kind.capitalize(), finding_rates(total, kind)) for kind in KINDS)
    return table(label, findings_header(["Finding type"]), body)


@dataclass
class Cohort:
    name: str
    dates: tuple[date, date]
    repos: dict

    @property
    def total(self):
        return sum(self.repos.values(), start=Stats())

    @property
    def label(self):
        return f"{self.name} | {self.dates[0]} to {self.dates[1]} UTC"


def render(cohorts, custom, model, version=ASSESSMENT_VERSION, reasoning_effort=DEFAULT_REASONING_EFFORT):
    overview, main, types = [], [], []
    for window in cohorts:
        stats = window.total
        counted = sum(stats.threads.values())
        assessed = counted - sum(stats.pending.values())
        note = f"{assessed:,} of {counted:,} visible thread openers assessed."
        if stats.excluded_bot_prs:
            note += f" Excluded {stats.excluded_bot_prs:,} bot-authored PRs."
        if stats.missing:
            note += " This window includes days that capture has not finalized."
        if stats.unknown_roots:
            note += " GitHub did not return some thread openers."
        overview.append(f'<article class="window"><h2>{escape(window.name)}</h2>'
                        f'<p class="dates">{window.dates[0]} to {window.dates[1]} UTC</p>'
                        f"<p>{escape(note)}</p></article>")
        main.append(main_table(window.label, window.repos, stats))
        types.append(types_table(window.label, stats))
    custom_reviewers = f" Configured: {', '.join(sorted(custom))}." if custom else ""
    legacy = version == digest("compact-sdk-v1") or version == (
        "83e7535cd200dbab733f135ce12a8fa9b38789b2f77e6530066135a137b19313")
    return Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        overview="".join(overview), main="".join(main), types="".join(types),
        custom_reviewers=escape(custom_reviewers),
        addressed_definition=("A matching change was made after the review opened and survived at merge."
                              if legacy else "The issue is gone at merge."),
        unaddressed_definition=("No matching change was found." if legacy else "The issue is still present at merge."),
        model=escape(f"{model} ({reasoning_effort} reasoning effort)"),
        version=escape(version[:12]),
    )


def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def assess_cached(archive, bundle, model, pull, audit, allow_model, version, *, git_root=None):
    repo, number = pull["repo"], pull["number"]

    def fingerprint(inputs):
        return judgment_key(model.model, inputs, model.reasoning_effort)

    bundles = [bundle]
    if not allow_model and version != ASSESSMENT_VERSION:
        bundles.extend(archive.get_prepared(pull, v) for v in reversed(SUPPORTED_PREPARED_VERSIONS[:-1]))
    for saved_bundle in bundles:
        key = fingerprint([version, pull, digest(saved_bundle)])
        saved = (archive.get_judgment(repo, number, "published", key)
                 or archive.get_judgment(repo, number, "complete", key))
        if saved is not None:
            return saved["threads"], False
    if not allow_model or bundle is None:
        return None, False
    key = fingerprint([version, pull, digest(bundle)])

    def checkpoint(stage, inputs, evaluate):
        stage_key = fingerprint(inputs)
        result = archive.get_judgment(repo, number, stage, stage_key)
        if result is None:
            result = evaluate()
            archive.put_judgment(repo, number, stage, stage_key, result)
        return result

    if git_root is None:
        rows = assess(bundle, model, pull, checkpoint)
    else:
        rows = assess(bundle, model, pull, checkpoint, retrieval=OutcomeEvidence(git_root, pull))
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


def assessment_order(archive, dates, prioritize_oversized):
    remaining = []
    for repo in REPOS:
        for pull in archive.pulls(repo, *dates):
            if not prioritize_oversized:
                yield pull, None
                continue
            bundle = archive.get_prepared(pull) if pull["threads"] else None
            if bundle is not None and needs_classification_batches(pull):
                log(f"Prioritizing split classification for {repo}#{pull['number']}")
                yield pull, bundle
            else:
                remaining.append(pull)
    for pull in remaining:
        yield pull, None


def collect(archive, args, model, windows, shared=None):
    cohorts, failures, runs = [], 0, 0
    completed = 0
    authors_by_window = [
        {repo: archive.pull_authors(repo, *dates) for repo in REPOS} for _, dates in windows
    ]
    for (label, dates), authors in zip(windows, authors_by_window, strict=True):
        repos = {repo: Stats(missing=bool(archive.pending_days(repo, *dates))) for repo in REPOS}
        for pull, bundle in assessment_order(archive, dates, args.prioritize_oversized):
            repo, number = pull["repo"], pull["number"]
            author = authors[repo].get(number)
            if author is None:
                raise DataError(f"{repo}#{number}: PR was added during report generation. "
                                "Run `python scorecard.py capture-authors` and retry.")
            if is_bot_pr_author(*author):
                repos[repo].excluded_bot_prs += 1
                continue
            rows = []
            if pull["threads"]:
                allow = args.command == "assess" and (args.max_prs is None or runs < args.max_prs)
                try:
                    rows, ran = assess_cached(
                        archive, bundle if bundle is not None else archive.get_prepared(pull),
                        model, pull, args.audit_dir, allow, args.assessment_version,
                        git_root=args.archive.parent / "git" if args.retrieve_evidence else None,
                    )
                except AssessmentSetupError:
                    raise
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
            repos[repo].collect(pull, rows, args.custom_reviewer, args.other_bot)
        for repo, stats in repos.items():
            log(f"{label} | {repo}: {sum(stats.threads.values())} visible threads, "
                f"{sum(stats.pending.values())} not assessed, {stats.unknown_roots} missing roots, "
                f"{stats.excluded_bot_prs} bot-authored PRs excluded")
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


def capture_window(args, parser):
    today = datetime.now(timezone.utc).date()
    if args.max_rate_wait < 0:
        parser.error("--max-rate-wait cannot be negative")
    through = args.through or today
    if not args.start <= through <= today:
        parser.error("capture dates must be in order and no later than today UTC")
    return through, today


def capture_authors_data(args, parser):
    through, _ = capture_window(args, parser)
    github = GitHub(args.max_rate_wait)
    try:
        if not args.archive.is_file():
            raise DataError("Archive does not exist. Run `python scorecard.py load-data` or `capture` first.")
        with closing(Archive(args.archive)) as archive:
            for repo in REPOS:
                checked = archive.capture_authors(github, repo, args.start, through)
                log(f"{repo}: checked authors for {checked:,} archived PRs")
    except COMMAND_ERRORS as error:
        log(f"Author capture stopped: {error}")
        return 1
    return 0


def capture_data(args, parser):
    through, today = capture_window(args, parser)
    if args.max_evidence_prs is not None and (not args.with_evidence or args.max_evidence_prs < 1):
        parser.error("--max-evidence-prs requires --with-evidence and a positive limit")
    github = GitHub(args.max_rate_wait)
    try:
        with closing(Archive(args.archive)) as archive:
            shared = load_shared(archive) if args.publish_data else None
            for repo in REPOS:
                log(f"Capturing {repo}")
                added = archive.capture(github, repo, args.start, through, today)
                log(f"{repo}: {added} new PRs archived")
                checked = archive.capture_authors(github, repo, args.start, through)
                log(f"{repo}: checked authors for {checked:,} archived PRs")
            if args.with_evidence:
                total_prepared = 0
                git_root = args.archive.parent / "git"
                for repo in REPOS:
                    count = 0
                    for pull in archive.pulls(repo, args.start, through):
                        if not pull["threads"] or archive.get_prepared(pull) is not None:
                            continue
                        if args.max_evidence_prs is not None and total_prepared >= args.max_evidence_prs:
                            break
                        bundle = prepare(pull, Evidence(git_root, pull))
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
        if args.retrieve_evidence:
            if args.publish_data:
                parser.error("--retrieve-evidence is local only and cannot be used with --publish-data")
            args.assessment_version = RETRIEVAL_VERSION
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
        if not args.archive.is_file() and not publish:
            raise DataError("Archive does not exist. Run `python scorecard.py load-data` or `capture` first.")
        with closing(Archive(args.archive, readonly=not assessing)) as archive, \
                closing(Copilot(args.model, args.reasoning_effort)) as model:
            shared = load_shared(archive) if publish else None
            cohorts, failures = collect(archive, args, model, windows, shared)
            if shared is not None:
                publish_shared(shared, include_assessments=True)
    except COMMAND_ERRORS as error:
        log(str(error))
        return 1
    write_atomic(args.output, render(cohorts, args.custom_reviewer, args.model, args.assessment_version,
                                     args.reasoning_effort))
    print(args.output)
    return int(bool(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build an aggregate scorecard from public GitHub PRs.")
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="update the local archive without using a model")
    authors = sub.add_parser("capture-authors", help="check authors of archived PRs without recapturing them")
    load = sub.add_parser("load-data", help="download the shared data branch without calling the GitHub API")
    assessment = sub.add_parser("assess", help="assess archived threads and render a report")
    report = sub.add_parser("report", help="render a report offline from the archive")
    for command in (capture, authors, load, assessment, report):
        command.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)

    load.set_defaults(run=load_data)
    capture.set_defaults(run=capture_data)
    authors.set_defaults(run=capture_authors_data)
    for command in (capture, authors):
        command.add_argument("--from", dest="start", type=date.fromisoformat, default=BASELINE[0])
        command.add_argument("--through", type=date.fromisoformat, help="last UTC day to capture (default: today)")
        command.add_argument("--max-rate-wait", type=int, default=60, metavar="SECONDS")
    capture.add_argument("--with-evidence", action="store_true", help="prepare PR evidence for offline assessment")
    capture.add_argument("--max-evidence-prs", type=int, help="cap new PR evidence bundles in this capture")
    capture.add_argument("--publish-data", action="store_true",
                         help="load and publish captured inputs on the public data branch")

    for command in (assessment, report):
        command.set_defaults(run=build_report, assessment_version=ASSESSMENT_VERSION, audit_dir=None,
                             prioritize_oversized=False, retrieve_evidence=False)
        command.add_argument("--window", nargs=2, type=date.fromisoformat, metavar=("START", "END"),
                             default=BASELINE, help="inclusive UTC merge dates (default: baseline)")
        command.add_argument("--follow-up", nargs=2, type=date.fromisoformat, metavar=("START", "END"))
        command.add_argument("--custom-reviewer", action="append", default=[], metavar="LOGIN")
        command.add_argument("--other-bot", action="append", default=[], metavar="LOGIN")
        command.add_argument("--model", default="gpt-6.2-sol", help="GitHub Copilot CLI model ID (default: %(default)s)")
        command.add_argument("--reasoning-effort", choices=REASONING_EFFORTS, default=DEFAULT_REASONING_EFFORT,
                             help="reasoning effort for the assessment (default: %(default)s)")
        command.add_argument("--output", type=Path, default=Path(".scorecard", "scorecard.html"))
    assessment.add_argument("--publish-data", action="store_true", help="load data and publish completed assessments")
    assessment.add_argument("--audit-dir", type=Path)
    assessment.add_argument("--max-prs", type=int, help="limit new assessments to this many PRs")
    assessment.add_argument("--prioritize-oversized", action="store_true",
                            help="assess PRs that need classification batches first in each window")
    assessment.add_argument("--retrieve-evidence", action="store_true",
                            help="retry unclear outcomes with bounded read-only Git lookups (local only)")
    report.add_argument("--assessment-version", help="full assessment_version from a saved audit file")

    args = parser.parse_args(argv)
    return args.run(args, parser)


if __name__ == "__main__":
    sys.exit(main())
