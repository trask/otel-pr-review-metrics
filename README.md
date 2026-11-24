# otel-pr-review-metrics

Measure inline review feedback across four public OpenTelemetry repositories. The scorecard does not run the custom reviewer, write to source repositories, or update Azure DevOps. Sharing data writes only to this repository's `data` branch.

## Run the scorecard

Use Python 3.10 or newer. Load the shared data and use GitHub CLI authenticated to `github.com` to check PR authors. The report then runs offline and uses the published baseline findings without calling Copilot.

```sh
python scorecard.py load-data
python scorecard.py capture-authors --through 2026-11-15
python scorecard.py report
```

`capture` archives merged PRs and their inline review data from October 1, 2026 through today in UTC. Rerun it to pick up new merges. It saves every completed PR as it goes, skips finalized days, and rechecks today and yesterday to allow for GitHub search indexing delays. It does not fetch threads again for PRs already saved. If the rate limit will reset within 60 seconds, capture waits and retries; otherwise it stops with a retry time and keeps its progress. Set `--max-rate-wait SECONDS` to change the wait, `--from` to backfill an earlier start, or `--through` to cap the backfill.

The local archive defaults to `~/.otel-pr-review-metrics/archive.sqlite3`, outside the worktree. Use `--archive PATH` to use another local file. `load-data` downloads the public `data` branch over Git without calling the GitHub API. The branch stores a coverage file per repository and, by UTC merge month, a JSON file per merged PR plus a compressed evidence file where one has been prepared.

Capture records GitHub's additions and deletions for new PRs. Publishing stores them in each repository's `line-changes.json`, keyed by PR number. They are kept separate from review and assessment inputs.

Capture also records each new PR's creation time and final readiness. The interval to merge starts at the last Ready for review event before merge, or at creation if the PR opened ready and stayed ready. Publishing stores `created_at` and `ready_at` in `merge-timing.json`, keyed by PR number. A `null` `ready_at` means GitHub did not establish readiness. Older PRs may have no timing record. These timestamps do not change prepared evidence or saved assessments.

`capture-authors` checks only the PR authors missing from the local archive. It uses GitHub without fetching review threads or invalidating saved assessments. Run it after loading new PRs from `data`, or use `capture`, which also checks authors. Use the same `--from` and `--through` dates as the report if they fall outside the default capture window.

```sh
python scorecard.py capture --with-evidence --publish-data
```

`capture --publish-data` loads the branch and pushes updated inputs as a new commit. It leaves existing shared assessments in place without publishing new ones. Local Git repositories stay off the branch. If another run updates `data` before the push, rerun to load its latest snapshot.

`capture --with-evidence` fetches the reviewed, head and merge commits into local Git repositories. It finds PR commits that are not reachable from the merge commit's first parent, shortlists 24 candidates per thread by path and date, and ranks their patches by overlap with the opener. It retains up to 100 KB of ranked change records per thread rather than a fixed number of commits. It selects up to three related files using named repository paths and changed code. It does not depend on commit hashes in replies. If Git cannot establish the PR's history, the judge can still compare the reviewed and merged snapshots. Use `--max-evidence-prs 25` to prepare a batch and resume on the next run. Capture makes no model calls. Rerun evidence capture to prepare bundles for a new evidence version; old bundles do not qualify for new assessments.

`report` reads the archive offline and writes `.scorecard/scorecard.html`. Its default window is October 1 through November 15, 2026. To report on another range, use inclusive UTC merge dates:

```sh
python scorecard.py report --window 2026-10-15 2026-11-20
```

Use `--repo OWNER/NAME` to include only that repository. Repeat `--repo` to include several. Without it, the report includes all four.

The report excludes PRs opened by GitHub `Bot` actors, logins ending in `[bot]`, or `opentelemetrybot` (case-insensitive). It still counts other PRs, including those with no available author, those opened before the window, and those with no inline threads. Bot reviews on included PRs still count. Dates capture has not finalized are distinct from days with zero merges.

The headline and the separate Time to merge by repository table count the same eligible merged PRs as the findings. The median, p75, and p90 use only PRs with valid timing. The overall row pools individual PR durations across repositories. Percentiles interpolate between sorted durations before rounding to days. Missing or invalid timestamps are marked as incomplete timing.

For both Automated and Human findings, the report shows original findings per included merged PR and the percentages of findings that are unaddressed or unclear. Each rate shows its numerator and denominator. The percentages use all findings in the row, and each finding type uses the same merged PR denominator. If capture is incomplete, the report labels counts and rates from archived PRs as provisional. It withholds rates that depend on unassessed or missing thread openers. If neither reviewer group has a rate across all repositories, the headline and Findings by type use the same labeled subset of repositories with complete assessments, when available.

To compare with a later window, supply the custom reviewer's exact GitHub login:

```sh
python scorecard.py report --follow-up YYYY-MM-DD YYYY-MM-DD --custom-reviewer 'your-reviewer[bot]'
```

Repeat `--custom-reviewer` for aliases. Use `--other-bot LOGIN` for unrelated automation using a GitHub User account. GitHub's Bot actor type and `[bot]` logins identify other automation automatically. Remaining User accounts count as Human. If the baseline is run without a custom login, its Automated total uses Copilot only and the Custom reviewer row is marked not measured.

## Assess new PRs

The report does not assess new PRs. To assess a follow-up locally with the baseline method, use Python 3.11 or newer and install the Copilot SDK:

```sh
python -m pip install -r requirements.txt
python -m copilot download-runtime
```

Set `COPILOT_GITHUB_TOKEN` to a token with Copilot access. The runner also accepts `GH_TOKEN` or `GITHUB_TOKEN`. Prepare evidence and check authors with `capture --with-evidence` for the new dates, then assess the follow-up:

```sh
python scorecard.py assess --retrieve-evidence --window START END --custom-reviewer 'your-reviewer[bot]'
```

Only `Unclear` outcomes can trigger a retry with read-only Git lookups. The default report reads these results from the archive. They do not publish automatically. After reviewing the results, publish saved decisions without calling Copilot:

```sh
python scorecard.py assess --retrieve-evidence --publish-data --max-prs 0
```

The exporter replays Git lookups to verify citations outside the prepared evidence. Published decisions contain thread IDs, finding types, outcomes, duplicate references, and public evidence links. Model explanations and lookup transcripts stay local.

## How the agent makes its calls

The classifier sends only the inline thread openers and the IDs, times, and commented paths. It classifies them and links clear duplicates to their originals. If a PR exceeds the input budget, it classifies the openers in batches, then checks the finding summaries for duplicates across the whole PR. The outcome pass examines each original finding separately using public GitHub patches and file snapshots from the reviewed revision, later commits and the merged revision. The program checks that evidence links come from those inputs. One Copilot SDK client handles the run, with a fresh session per request, a task-specific system prompt that replaces the default agent prompt, and no tools. Only public evidence goes to Copilot.

A thread opens when GitHub publishes it, not when it was drafted. The scorecard asks whether the issue shown in the reviewed revision is gone at merge. A rewrite can address a finding without keeping the same patch. If the issue returns before merge, the outcome is Unaddressed and the audit can record that it regressed. The audit records a separate timing result if a patch shows the fix. That result compares the commit's date with publication; it does not prove when the change was pushed or what caused it. Unclear means the code evidence does not settle the merge outcome. GitHub does not expose deleted comments, so the report only counts observable thread openers.

Completed judgments are saved as the run proceeds. Local audit files beside the archive record the decision, reasoning, and evidence links. The aggregate report contains no raw comments or individual assessments.
