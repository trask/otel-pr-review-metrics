import os
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from archive import Archive, ArchivedGitHub, encode
from assessment import AssessmentError, Copilot, classify, judge
from github_data import DataError, GitHub, RateLimitError, run_command
from git_evidence import GitEvidence
from prepared import PREPARED_VERSION, Evidence, digest, prepare
from scorecard import AUTOMATED, BASELINE, GROUPS, REPOS, Cohort, Stats, assess_cached, identity, main, render
from shared_data import SharedData, judgment_key, public_assessment


def git_at(path, *args, **kwargs):
    result = run_command(["git", "--no-pager", *args], cwd=path, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def shared_remote(root):
    repo_path, remote = root / "work", root / "remote.git"
    repo_path.mkdir()
    git_at(repo_path, "init", "--quiet")
    git_at(repo_path, "config", "user.name", "Scorecard fixture")
    git_at(repo_path, "config", "user.email", "fixture@example.test")
    git_at(repo_path, "init", "--quiet", "--bare", str(remote))
    (repo_path / "schema.json").write_text('{"format": 1}\n', encoding="utf-8")
    git_at(repo_path, "add", "schema.json")
    git_at(repo_path, "commit", "--quiet", "-m", "Initialize shared data")
    git_at(repo_path, "push", "--quiet", str(remote), "HEAD:refs/heads/data")
    return repo_path, remote


def pilot_cases(root):
    git_at(root, "init", "--quiet")
    git_at(root, "config", "user.name", "Scorecard fixture")
    git_at(root, "config", "user.email", "fixture@example.test")

    def commit(minute, text):
        (root / "adult.py").write_text(f"def is_adult(age):\n    return age {text} 18\n", encoding="utf-8")
        git_at(root, "add", "--", "adult.py")
        time = f"2026-10-01T13:{minute:02}:00Z"
        git_at(root, "commit", "--quiet", "-m", "Update boundary check",
               env=dict(os.environ, GIT_AUTHOR_DATE=time, GIT_COMMITTER_DATE=time))
        return git_at(root, "rev-parse", "HEAD")

    reviewed, fixed, reverted = commit(0, ">"), commit(20, ">="), commit(40, ">")
    for label, opened, head, expected in (
        ("delayed draft", "2026-10-01T13:30:00Z", fixed, "unclear"),
        ("reverted fix", "2026-10-01T13:10:00Z", reverted, "unaddressed"),
    ):
        thread = {"id": "thread", "url": "https://example.test/thread", "path": "adult.py",
                  "created": "2026-10-01T13:01:00Z", "opened": opened,
                  "reviewed": reviewed, "replies": [],
                  "author": {"login": "person", "__typename": "User"},
                  "body": "This rejects 18-year-olds. The minimum age is inclusive.",
                  "hunk": "+    return age > 18"}
        pull = {"repo": REPOS[0], "number": 1, "head": head, "merge": head,
                "merged": "2026-10-01T13:50:00Z", "threads": [thread], "missing_roots": 0}
        evidence = Evidence(Mock(), pull)
        evidence.local = GitEvidence(root, REPOS[0], 1)
        evidence.local.path = root / ".git"
        evidence.local.ready = True
        yield label, pull, thread, evidence, expected


class FakeModel:
    def __init__(self, result):
        self.result = result

    def ask(self, prompt, evidence):
        return self.result


def connection(nodes, next_cursor=None):
    return {"nodes": nodes, "pageInfo": {
        "hasNextPage": next_cursor is not None, "endCursor": next_cursor,
    }}


class CollectionTests(unittest.TestCase):
    def test_shared_data_round_trip_uses_only_public_tables(self):
        with TemporaryDirectory() as work:
            root = Path(work)
            repo_path, remote = shared_remote(root)
            with closing(Archive(root / "writer.sqlite3")) as writer:
                shared = SharedData(writer, REPOS, remote=str(remote), cwd=repo_path)
                shared.load()
                self.assertIsNotNone(shared.publish())
                shared.load()
                self.assertIsNone(shared.publish())
                pull = {"repo": REPOS[0], "number": 1, "merged": "2026-10-01T10:00:00Z",
                        "threads": [], "missing_roots": 0}
                bundle = {"version": PREPARED_VERSION, "input": digest(pull), "threads": {}, "records": []}
                with writer.db:
                    writer.db.execute("INSERT INTO complete_days VALUES (?, ?)", (REPOS[0], "2026-10-01"))
                    writer.db.execute("INSERT INTO pulls VALUES (?, ?, ?, ?, ?)",
                                      (REPOS[0], 1, "2026-10-01", pull["merged"], encode(pull)))
                writer.put_prepared(REPOS[0], 1, bundle)
                writer.put_judgment(REPOS[0], 1, "judge", "test", {"status": "addressed"})
                updated = shared.publish()
                files = git_at(repo_path, "ls-tree", "-r", "--name-only", updated).splitlines()
                self.assertIn(f"{REPOS[0]}/coverage.json", files)
                self.assertIn(f"{REPOS[0]}/2026-10/1.json", files)
                self.assertIn(f"{REPOS[0]}/2026-10/1.evidence.json.gz", files)
                self.assertNotIn("archive.sqlite3", files)
            with closing(Archive(root / "reader.sqlite3")) as reader:
                SharedData(reader, REPOS, remote=str(remote), cwd=repo_path).load()
                self.assertEqual(reader.pending_days(REPOS[0], BASELINE[0], BASELINE[0]), [])
                self.assertEqual(reader.get_prepared(pull), bundle)
                self.assertIsNone(reader.get_judgment(REPOS[0], 1, "judge", "test"))

    def test_published_results_are_versioned_and_reusable_without_a_model(self):
        repo = REPOS[0]
        day = "2026-10-01"
        link = f"https://github.com/{repo}/commit/{'c' * 40}"
        pull = {"repo": repo, "number": 1, "merged": f"{day}T12:00:00Z", "missing_roots": 0,
                "threads": [
                    {"id": "first", "opened": f"{day}T09:00:00Z",
                     "author": {"login": "copilot-pull-request-reviewer[bot]", "__typename": "Bot"}},
                    {"id": "duplicate", "opened": f"{day}T09:30:00Z",
                     "author": {"login": "person", "__typename": "User"}},
                ]}
        bundle = {"version": PREPARED_VERSION, "input": digest(pull), "records": [{"url": link}],
                  "threads": {"first": {"records": {"merge-commit": 0}}, "duplicate": {"records": {}}}}
        published_version = "83e7535cd200dbab733f135ce12a8fa9b38789b2f77e6530066135a137b19313"
        model = "test-model"
        with TemporaryDirectory() as work:
            root = Path(work)
            repo_path, remote = shared_remote(root)

            def shared(archive, repos):
                return SharedData(archive, repos, remote=str(remote), cwd=repo_path)

            local, fresh, html = (root / name for name in ("local.sqlite3", "fresh.sqlite3", "scorecard.html"))
            with closing(Archive(local)) as writer:
                publisher = shared(writer, REPOS)
                publisher.load()
                with writer.db:
                    writer.db.executemany("INSERT INTO complete_days VALUES (?, ?)", ((r, day) for r in REPOS))
                    writer.db.execute("INSERT INTO pulls VALUES (?, ?, ?, ?, ?)",
                                      (repo, 1, day, pull["merged"], encode(pull)))
                writer.put_prepared(repo, 1, bundle)
                for effort, status in (("medium", "unclear"), ("high", "addressed")):
                    key = judgment_key(model, [published_version, pull, digest(bundle)], effort)
                    result = {"assessment_version": published_version, "model": model,
                              "reasoning_effort": effort, "evidence_digest": digest(bundle),
                              "threads": [
                                  {"id": "first", "kind": "bug", "status": status, "evidence": [link],
                                   "reason": "Do not publish this model explanation"},
                                  {"id": "duplicate", "kind": None, "status": "duplicate", "original": "first"},
                              ]}
                    writer.put_judgment(repo, 1, "complete", key, result)
                writer.put_judgment(repo, 1, "judge", "partial", {"outcome_reason": "Private intermediate output"})
                before = publisher.publish()
                self.assertNotIn(".assessment.", git_at(repo_path, "ls-tree", "-r", "--name-only", before))

            options = ["--window", day, day, "--model", model, "--output", str(html)]
            with (patch("scorecard.SharedData", side_effect=shared),
                  patch.object(Copilot, "ask", side_effect=AssertionError("no model calls"))):
                self.assertEqual(main(["assess", "--archive", str(local), "--publish-data", "--max-prs", "0",
                                       *options]), 0)
                self.assertEqual(main(["load-data", "--archive", str(fresh)]), 0)
                self.assertEqual(main(["report", "--archive", str(fresh), *options]), 0)
                self.assertIn("<td>1</td><td>1.00</td><td>1</td><td>0</td><td>0</td><td>0</td>",
                              html.read_text(encoding="utf-8"))
                self.assertEqual(main(["report", "--archive", str(fresh), "--reasoning-effort", "medium", *options]), 0)
                self.assertIn("<td>1</td><td>1.00</td><td>0</td><td>0</td><td>0</td><td>0</td>",
                              html.read_text(encoding="utf-8"))
                self.assertIn("test-model (medium reasoning effort)", html.read_text(encoding="utf-8"))

            with closing(Archive(fresh)) as reader:
                publisher = shared(reader, REPOS)
                publisher.load()
                self.assertIsNone(publisher.publish())
                self.assertEqual(list(reader.db.execute("SELECT stage, count(*) FROM judgments GROUP BY stage")),
                                 [("published", 2)])
                self.assertEqual(reader.get_judgment(repo, 1, "published", key)["reasoning_effort"], "high")
                self.assertEqual(reader.get_judgment(repo, 1, "published", key)["threads"], [
                    {"id": "first", "kind": "bug", "status": "addressed", "evidence": [link]},
                    {"id": "duplicate", "kind": None, "status": "duplicate", "original": "first"},
                ])

            result["threads"][0]["evidence"] = [f"https://github.com/{repo}/commit/{'d' * 40}"]
            with self.assertRaises(DataError):
                public_assessment(pull, result, key, bundle=bundle)

    def test_capture_resumes_after_limit_and_reads_reports_from_disk(self):
        repo = REPOS[0]
        first, empty, recent = date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)
        merged = "2026-10-01T12:00:00Z"
        pull = {"repo": repo, "number": 1, "merged": merged, "missing_roots": 0, "threads": []}
        github = Mock()
        github.merged.return_value = {1: merged, 2: merged}

        def interrupted(repo, pending):
            yield pull
            raise RateLimitError("Try later")

        github.pulls.side_effect = interrupted
        with TemporaryDirectory() as work:
            path = Path(work) / "archive.sqlite3"
            with closing(Archive(path)) as archive:
                with self.assertRaises(RateLimitError):
                    archive.capture(github, repo, first, empty, recent)
                self.assertEqual(archive.pending_days(repo, first, empty), [first, empty])
            github.pulls.side_effect = None
            github.pulls.return_value = iter([dict(pull, number=2)])
            with closing(Archive(path)) as archive:
                self.assertEqual(archive.capture(github, repo, first, empty, recent), 1)
                github.pulls.assert_called_with(repo, {2: merged})
                github.merged.return_value = {}
                github.pulls.return_value = iter([])
                archive.capture(github, repo, first, empty, date(2026, 10, 4))
                github.merged.assert_called_with(repo, empty, empty)
                github.reset_mock()
                archive.capture(github, repo, first, empty, date(2026, 10, 4))
                github.merged.assert_not_called()
            with closing(Archive(path, readonly=True)) as archive:
                self.assertEqual([pr["number"] for pr in archive.pulls(repo, first, empty)], [1, 2])
                self.assertEqual(archive.pending_days(repo, first, empty), [])
            output = Path(work) / "scorecard.html"
            with patch("scorecard.GitHub", side_effect=AssertionError("report must not contact GitHub")):
                self.assertEqual(main(["report", "--archive", str(path), "--output", str(output),
                                       "--window", first.isoformat(), empty.isoformat()]), 0)
            self.assertIn('<th scope="row">semantic-conventions-genai</th><td>2</td>',
                          output.read_text(encoding="utf-8"))

    def test_reuses_code_evidence_in_later_runs(self):
        path = f"repos/{REPOS[0]}/commits/revision"
        github = Mock()
        github.get.return_value = {"files": []}
        with TemporaryDirectory() as work:
            db = Path(work) / "archive.sqlite3"
            with closing(Archive(db)) as archive:
                self.assertEqual(ArchivedGitHub(archive, github, REPOS[0]).get(path), {"files": []})
            github.reset_mock()
            with closing(Archive(db)) as archive:
                self.assertEqual(ArchivedGitHub(archive, github, REPOS[0]).get(path), {"files": []})
            github.get.assert_not_called()
            github.public.assert_not_called()

    def test_rate_limit_with_retry_after_stops_when_wait_is_too_long(self):
        result = SimpleNamespace(returncode=1, stderr="gh: secondary rate limit (HTTP 403)",
                                 stdout="HTTP/2 403 Forbidden\nRetry-After: 180\n\n{}")
        with patch("github_data.run_command", return_value=result), patch("github_data.sleep") as sleep:
            with self.assertRaisesRegex(RateLimitError, "180s"):
                GitHub(max_rate_wait=60).get("search/issues")
            sleep.assert_not_called()

    def test_search_splits_and_collects_every_page_by_merge_date(self):
        def results(path, q, page, **kwargs):
            def item(n, day):
                return {"number": n, "created_at": "2026-09-01T00:00:00Z",
                        "pull_request": {"merged_at": f"2026-10-{day:02}T00:00:00Z"}}

            if "2026-10-01..2026-10-02" in q:
                return {"total_count": 1001, "incomplete_results": False, "items": []}
            if "2026-10-01..2026-10-01" in q:
                return {
                    "total_count": 101, "incomplete_results": False,
                    "items": [item(n, 1) for n in (range(1, 101) if page == 1 else [101])],
                }
            return {"total_count": 1, "incomplete_results": False, "items": [item(102, 2)]}

        github = GitHub()
        with patch.object(github, "get", side_effect=results):
            cohort = github._merged(REPOS[0], date(2026, 10, 1), date(2026, 10, 2))
        self.assertEqual(list(cohort), list(range(1, 103)))
        self.assertEqual(cohort[102], "2026-10-02T00:00:00Z")

    def test_collects_more_thread_and_reply_pages_without_counting_replies(self):
        def comment(id_, published, reply=False):
            return {
                "id": id_, "url": f"https://example.test/{id_}", "createdAt": "2026-09-01T00:00:00Z",
                "publishedAt": published, "replyTo": {"id": "root"} if reply else None,
                "author": {"login": "person", "__typename": "User"}, "body": "", "path": "app.py",
                "diffHunk": "", "originalCommit": {"oid": "reviewed"}, "pullRequestReview": None,
            }

        before, merged, after = ("2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z")
        pr = {
            "number": 1, "mergedAt": merged, "headRefOid": "head", "mergeCommit": {"oid": "merge"},
            "reviewThreads": connection([{
                "id": "t1", "comments": connection([comment("root", before)], "replies"),
            }], "threads"),
        }
        github = GitHub()
        with patch.object(github, "query", side_effect=[
            {"node": {"comments": connection([comment("reply", before, reply=True)])}},
            {"repository": {"visibility": "PUBLIC", "pullRequest": {"reviewThreads": connection([
                {"id": "t2", "comments": connection([comment("at-merge", merged)])},
                {"id": "t3", "comments": connection([comment("post-merge", after)])},
                {"id": "t4", "comments": connection([comment("pending", None)])},
            ])}}},
        ]):
            pull = github._pull(REPOS[0], *REPOS[0].split("/"), pr)
        self.assertEqual([thread["id"] for thread in pull["threads"]], ["t1"])
        self.assertEqual(pull["threads"][0]["replies"][0]["url"], "https://example.test/reply")
        self.assertEqual(pull["threads"][0]["reviewed"], "reviewed")


class AssessmentTests(unittest.TestCase):
    def test_assess_runs_from_prepared_data_without_git_or_github(self):
        with TemporaryDirectory() as work:
            root = Path(work)
            git = root / "git"
            git.mkdir()
            _, pull, _, evidence, _ = next(pilot_cases(git))
            bundle = prepare(pull, evidence)
            db = root / "archive.sqlite3"
            with closing(Archive(db)) as archive:
                with archive.db:
                    archive.db.execute("INSERT INTO pulls VALUES (?, ?, ?, ?, ?)",
                                       (pull["repo"], 1, "2026-10-01", pull["merged"], encode(pull)))
                archive.put_prepared(pull["repo"], 1, bundle)
            model = Mock(model="test-model", reasoning_effort="high")
            model.ask.side_effect = [
                [{"id": "thread", "kind": "bug", "paths": [], "reason": "Rejects adults."}],
                {"status": "unclear", "evidence": ["comparison"], "reason": "Timing is unclear."},
            ]
            with patch("scorecard.GitHub", side_effect=AssertionError("assessment must not call REST")), \
                    patch("prepared.GitEvidence", side_effect=AssertionError("assessment must not fetch Git")), \
                    patch("scorecard.Copilot", return_value=model):
                self.assertEqual(main(["assess", "--archive", str(db),
                                       "--output", str(root / "scorecard.html")]), 0)
            self.assertEqual(model.ask.call_count, 2)
            audit, = (root / "audit").glob("**/*.json")
            self.assertIn('"status": "unclear"', audit.read_text(encoding="utf-8"))

    def test_git_evidence_separates_a_draft_cutoff_and_a_reverted_fix(self):
        with TemporaryDirectory() as work:
            for label, pull, thread, evidence, _ in pilot_cases(Path(work)):
                records = evidence.for_thread(thread, ["adult.py"])
                evidence.github.get.assert_not_called()
                self.assertIn("return age > 18", records["reviewed-0"]["text"])
                if label == "delayed draft":
                    self.assertEqual(records["comparison"]["commits_dated_at_or_before_publication"], 1)
                    self.assertNotIn("later-0", records)
                    self.assertIn("return age >= 18", records["merged-0"]["text"])
                else:
                    self.assertIn("later-0", records)
                    self.assertIn("later-1", records)
                    self.assertIn("return age > 18", records["merged-0"]["text"])

    def test_changing_outcome_version_keeps_classification_and_old_result(self):
        pull = {"repo": REPOS[0], "number": 1, "merge": "revision"}
        model = Copilot("model")
        classify_once, judge_v1, judge_v2 = Mock(return_value="bug"), Mock(return_value="unclear"), Mock(
            return_value="addressed"
        )

        def run(judge_version, evaluator):
            def fake_assess(github, model, pull, checkpoint):
                return [checkpoint("classify", ["stable-prompt"], classify_once),
                        checkpoint("judge", [judge_version], evaluator)]
            return fake_assess

        with TemporaryDirectory() as work:
            root = Path(work)
            with closing(Archive(root / "archive.sqlite3")) as archive:
                with patch("scorecard.assess", side_effect=run("old-prompt", judge_v1)):
                    self.assertEqual(assess_cached(archive, {}, model, pull, root, True, "old")[0],
                                     ["bug", "unclear"])
                with patch("scorecard.assess", side_effect=run("new-prompt", judge_v2)):
                    self.assertEqual(assess_cached(archive, {}, model, pull, root, True, "new")[0],
                                     ["bug", "addressed"])
                self.assertEqual(assess_cached(archive, {}, model, pull, None, False, "old"),
                                 (["bug", "unclear"], False))
                medium = Copilot("model", "medium")
                self.assertEqual(assess_cached(archive, {}, medium, pull, None, False, "new"), (None, False))
                with patch("scorecard.assess", side_effect=run("new-prompt", judge_v2)):
                    self.assertEqual(assess_cached(archive, {}, medium, pull, root, True, "new")[0],
                                     ["bug", "addressed"])
            self.assertEqual(classify_once.call_count, 2)
            judge_v1.assert_called_once()
            self.assertEqual(judge_v2.call_count, 2)

    def test_duplicate_only_links_earlier_original_and_is_not_a_finding(self):
        threads = [
            {"id": "a", "url": "original-url", "path": "test.py",
             "opened": "2026-10-01T00:00:00Z", "created": "2026-10-01T00:00:00Z"},
            {"id": "b", "url": "duplicate-url", "path": "test.py",
             "opened": "2026-10-01T01:00:00Z", "created": "2026-10-01T01:00:00Z"},
        ]
        answers = [
            {"id": "a", "kind": "test coverage gap", "paths": [], "reason": "Missing test."},
            {"id": "b", "kind": "duplicate", "original": "a", "reason": "Same missing test."},
        ]
        rows = classify(FakeModel(answers), {"threads": threads}, {"a": [], "b": []})
        self.assertEqual((rows[1]["kind"], rows[1]["status"], rows[1]["original_url"]),
                         (None, "duplicate", "original-url"))
        threads[1]["opened"] = threads[0]["opened"]
        with self.assertRaises(AssessmentError):
            classify(FakeModel(answers), {"threads": threads}, {"a": [], "b": []})

    def test_addressed_needs_a_matching_later_patch_and_merge_state(self):
        records = {
            "reviewed-0": {"url": "reviewed-url", "state": "text", "path": "code.py"},
            "merged-0": {"url": "merged-url", "state": "text", "path": "code.py"},
            "later-0": {"url": "later-url"},
        }
        reply = {"status": "addressed", "reason": "Matching code is kept.",
                 "evidence": ["reviewed-0", "merged-0"]}
        args = ({"merged": "time"}, {}, {"paths": ["code.py"]}, records)
        self.assertEqual(judge(FakeModel(reply), *args)["status"], "unclear")
        reply["evidence"].append("later-0")
        self.assertEqual(judge(FakeModel(reply), *args)["evidence"],
                         ["reviewed-url", "merged-url", "later-url"])

    def test_json_events_preserve_long_model_answers_and_disable_tools(self):
        stdout = ('{"type":"session.info","data":{}}\n'
                  '{"type":"assistant.message","data":{"content":"{\\"ok\\":true}"}}\n')
        with patch("github_data.subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout=stdout,
        )) as run:
            for model, effort in ((Copilot("model"), "high"), (Copilot("model", "medium"), "medium")):
                self.assertEqual(model.ask("Classify", {}), {"ok": True})
                args = run.call_args.args[0]
                self.assertEqual(args[args.index("--reasoning-effort") + 1], effort)
                self.assertIn("--available-tools=scorecard_no_tools", args)
                self.assertIn("creationflags", run.call_args.kwargs)

    def test_missing_comparison_cannot_establish_an_unaddressed_finding(self):
        records = {
            "reviewed-0": {"url": "reviewed-url", "state": "text", "path": "code.py"},
            "merged-0": {"url": "merged-url", "state": "text", "path": "code.py"},
            "comparison": {"url": "compare-url", "complete": False},
        }
        reply = {"status": "unaddressed", "reason": "No matching change.",
                 "evidence": list(records)}
        args = ({"merged": "time"}, {}, {"paths": ["code.py"]}, records)
        self.assertEqual(judge(FakeModel(reply), *args)["status"], "unclear")
        records["comparison"]["complete"] = True
        self.assertEqual(judge(FakeModel(reply), *args)["status"], "unaddressed")


class ReportTests(unittest.TestCase):
    def test_account_groups_include_bots_and_configured_users_without_merging_unknowns(self):
        self.assertEqual(identity({"login": "copilot-pull-request-reviewer", "__typename": "Bot"},
                                  set(), set()), "copilot")
        self.assertEqual(identity({"login": "LooksHuman", "__typename": "User"},
                                  {"lookshuman"}, set()), "custom")
        self.assertEqual(identity({"login": "LooksHuman", "__typename": "User"},
                                  set(), {"lookshuman"}), "outside")
        self.assertEqual(identity(None, set(), set()), "outside")

    def test_summed_rate_thread_buckets_and_followup_missing_are_distinct(self):
        stats = Stats()
        authors = [
            {"login": "copilot-pull-request-reviewer", "__typename": "Bot"},
            {"login": "person", "__typename": "User"},
            {"login": "custom", "__typename": "Bot"},
        ]
        stats.collect(
            {"missing_roots": 0, "threads": [{"id": str(i), "author": a} for i, a in enumerate(authors)]},
            [{"id": "0", "status": "addressed", "kind": "bug"},
             {"id": "1", "status": "duplicate", "kind": None},
             {"id": "2", "status": "unclear", "kind": "improvement"}],
            {"custom"}, set(),
        )
        baseline = Cohort("Baseline", BASELINE, {REPOS[0]: stats, REPOS[1]: Stats(prs=3)})
        total = baseline.total
        self.assertEqual(total.rate(), "0.50")
        self.assertEqual(total.finding(AUTOMATED, "addressed"), 1)
        self.assertEqual(total.finding(AUTOMATED, "unaddressed"), 0)
        self.assertEqual(total.total(GROUPS), sum(total.outcomes.values()))
        follow_up = Cohort("Follow-up", (date(2026, 11, 16), date(2026, 11, 17)),
                           {REPOS[0]: Stats(missing=True)})
        html = render([baseline, follow_up], {"custom"}, "model")
        self.assertEqual(html.count("<table>"), 6)
        self.assertLess(html.index("Automated findings"), html.index("Human findings"))
        self.assertIn('<th scope="row">All repositories</th><td>4</td><td>0.50</td>', html)
        self.assertIn('title="Not measured">N/M</abbr>', html)
        self.assertIn('<th scope="row">All inline review threads</th><td>1</td><td>0</td>'
                      '<td>1</td><td>1</td><td>0</td><td>3</td>', html)


if __name__ == "__main__":
    unittest.main()
