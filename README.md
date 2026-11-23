# otel-pr-review-metrics

Measure inline review feedback across four public OpenTelemetry repositories. The scorecard evaluates reviews already on GitHub. It does not run the custom reviewer or write to GitHub or Azure DevOps.

## Run the scorecard

Use Python 3.10 or newer, Git, and a GitHub CLI authenticated to `github.com`. Assessment also needs an authenticated GitHub Copilot CLI. Offline reporting needs only Python.

```sh
python scorecard.py capture
python scorecard.py report
```

`capture` archives merged PRs and their inline review data from October 1, 2026 through today in UTC. Rerun it to pick up new merges. It saves every completed PR as it goes, skips finalized days, and rechecks today and yesterday to allow for GitHub search indexing delays. It does not fetch threads again for PRs already saved. If the rate limit will reset within 60 seconds, capture waits and retries; otherwise it stops with a retry time and keeps its progress. Set `--max-rate-wait SECONDS` to change the wait, `--from` to backfill an earlier start, or `--through` to cap the backfill.

The archive defaults to `~/.otel-pr-review-metrics/archive.sqlite3`, outside the worktree. Use `--archive PATH` to use another local file. Start from the public `data` branch to avoid downloading PRs or reusable API evidence again:

```sh
python scorecard.py capture --shared
python scorecard.py capture --publish-data
```

`--shared` reads the branch without updating it. `--publish-data` loads it, captures new data, and pushes a new snapshot if there are changes. Both flags also work with `assess`. Only public PR data and supported REST fallback responses go into the snapshot. Local Git repositories and model judgments stay off the branch. If another run updates `data` before the push, rerun to load its latest snapshot.

The assessor fetches the reviewed, head and merge commits into local bare Git repositories alongside the archive. It builds patches and reads file snapshots from Git first, and uses cached or live REST responses when Git cannot supply an item. `capture --with-evidence` prepares code evidence for the paths reviewers commented on. Neither capture mode makes model calls.

`report` reads the archive offline and writes `.scorecard/scorecard.html`. Its default window is October 1 through November 15, 2026. To report on another range, use inclusive UTC merge dates:

```sh
python scorecard.py report --window 2026-10-15 2026-11-20
```

The report includes PRs opened before the window and PRs with no inline threads. Dates capture has not finalized are distinct from days with zero merges. To compare with a later window, supply the custom reviewer's exact GitHub login:

```sh
python scorecard.py report --follow-up YYYY-MM-DD YYYY-MM-DD --custom-reviewer 'your-reviewer[bot]'
```

Repeat `--custom-reviewer` for aliases. Use `--other-bot LOGIN` for unrelated automation using a GitHub User account. GitHub's Bot actor type and `[bot]` logins identify other automation automatically. Remaining User accounts count as Human. If the baseline is run without a custom login, its Automated total uses Copilot only and the Custom reviewer row is marked not measured.

Run `python scorecard.py assess` to measure findings and render the report. Use `--max-prs 5` to try it on a few PRs first. Expect one model request per uncached PR with threads and one more per uncached original finding. Model requests can consume Copilot credits. `assess` accepts the same window and reviewer options as `report`. Use the same `--model` and assessment version across both windows.

## How the agent makes its calls

The first model pass reads the inline thread openers on a PR, classifies what they flag, links clear duplicates to their originals, and selects relevant files. The second pass examines each original finding using public GitHub patches and file snapshots from the reviewed revision, later commits and the merged revision. The program checks that evidence links come from those inputs. The model has no tools; the collector alone makes read-only GitHub requests. Only public evidence goes to Copilot.

A thread opens when GitHub publishes it, not when it was drafted. A commit timestamp is not proof of when a change was pushed or why it was made. The agent uses Unclear if a matching change cannot be placed before or after publication, or if the evidence is otherwise insufficient. The scorecard marks missing assessments as N/M. GitHub does not expose deleted comments, so the report can only measure threads whose openers remain observable.

Classification and outcome judgments have separate cache keys based on their prompts, model, and inputs. Completed judgments are saved even if a later judgment fails. Editing the outcome prompt does not invalidate classifications. Each completed assessment has a versioned JSON audit file under `audit` next to the database, with reasoning and evidence links. Use `report --assessment-version HASH` to render a previous version using the full `assessment_version` in its audit file and the same model ID. The aggregate HTML contains no raw comments or individual assessments.
