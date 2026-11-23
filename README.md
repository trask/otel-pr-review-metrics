# otel-pr-review-metrics

Measure inline review feedback across four public OpenTelemetry repositories. The scorecard does not run the custom reviewer, write to source repositories, or update Azure DevOps. Sharing data writes only to this repository's `data` branch.

## Run the scorecard

Use Python 3.10 or newer. Capture needs Git and a GitHub CLI authenticated to `github.com`. Reporting needs only Python.

```sh
python scorecard.py load-data
python scorecard.py report
```

`capture` archives merged PRs and their inline review data from October 1, 2026 through today in UTC. Rerun it to pick up new merges. It saves every completed PR as it goes, skips finalized days, and rechecks today and yesterday to allow for GitHub search indexing delays. It does not fetch threads again for PRs already saved. If the rate limit will reset within 60 seconds, capture waits and retries; otherwise it stops with a retry time and keeps its progress. Set `--max-rate-wait SECONDS` to change the wait, `--from` to backfill an earlier start, or `--through` to cap the backfill.

The local archive defaults to `~/.otel-pr-review-metrics/archive.sqlite3`, outside the worktree. Use `--archive PATH` to use another local file. `load-data` downloads the public `data` branch over Git without calling the GitHub API. The branch stores a coverage file per repository and, by UTC merge month, a JSON file per merged PR plus a compressed evidence file where one has been prepared.

```sh
python scorecard.py capture --with-evidence --publish-data
```

`capture --publish-data` loads the branch and pushes updated inputs as a new commit. It leaves existing shared assessments in place without publishing new ones. Local Git repositories stay off the branch. If another run updates `data` before the push, rerun to load its latest snapshot.

`capture --with-evidence` fetches the reviewed, head and merge commits into local Git repositories. It finds PR commits that are not reachable from the merge commit's first parent, shortlists 24 candidates per thread by path and date, and ranks their patches by overlap with the opener. It retains up to 100 KB of ranked change records per thread rather than a fixed number of commits. It selects up to three related files using named repository paths and changed code. It does not depend on commit hashes in replies. If Git cannot establish the PR's history, the judge can still compare the reviewed and merged snapshots. Use `--max-evidence-prs 25` to prepare a batch and resume on the next run. Capture makes no model calls. Rerun evidence capture to prepare bundles for a new evidence version; old bundles do not qualify for new assessments.

`report` reads the archive offline and writes `.scorecard/scorecard.html`. Its default window is October 1 through November 15, 2026. To report on another range, use inclusive UTC merge dates:

```sh
python scorecard.py report --window 2026-10-15 2026-11-20
```

The report includes PRs opened before the window and PRs with no inline threads. Dates capture has not finalized are distinct from days with zero merges. To compare with a later window, supply the custom reviewer's exact GitHub login:

```sh
python scorecard.py report --follow-up YYYY-MM-DD YYYY-MM-DD --custom-reviewer 'your-reviewer[bot]'
```

Repeat `--custom-reviewer` for aliases. Use `--other-bot LOGIN` for unrelated automation using a GitHub User account. GitHub's Bot actor type and `[bot]` logins identify other automation automatically. Remaining User accounts count as Human. If the baseline is run without a custom login, its Automated total uses Copilot only and the Custom reviewer row is marked not measured.

Assessment needs Python 3.11 or newer and the Copilot SDK. Install it and download its runtime:

```sh
python -m pip install -r requirements.txt
python -m copilot download-runtime
```

Set `COPILOT_GITHUB_TOKEN` to a token with Copilot access. The runner also accepts `GH_TOKEN` or `GITHUB_TOKEN` if `COPILOT_GITHUB_TOKEN` is unset. Run `python scorecard.py assess` to measure findings and render the report. Assessment reads the prepared local inputs and calls Copilot without fetching code or PR data. It defaults to GPT-6.2 Sol (`gpt-6.2-sol`) with `high` reasoning effort. Use `--reasoning-effort medium` for a separate run at medium effort. PRs without a prepared bundle are not measured. If the required evidence is missing or exceeds the input budget, the outcome is Unclear. Use the same model, effort, and assessment version across both windows.

To pilot large PRs before the full run, put PRs that require more than one classification request first:

```sh
python scorecard.py assess --window 2026-10-01 2026-11-21 --max-prs 25 --prioritize-oversized
```

Add `--retrieve-evidence` to retry only `Unclear` outcomes locally. The first pass uses prepared evidence alone and reuses matching static judgments. A retry starts with a smaller evidence bundle and can make up to two read-only Git lookups. It can search PR-changed paths, read a file in the exact reviewed and merged trees, or inspect PR-only diffs for a named path. An empty lookup does not prove there was no fix. The audit keeps the first outcome and records any lookup requests and returned evidence IDs. The classifier stays tool-free. You cannot combine this mode with `--publish-data`.

Use `python scorecard.py assess --publish-data` to load the shared inputs and previously published results, assess missing PRs, and publish completed decisions to `data`. The run saves locally after each PR and pushes every 25 new assessments and at the end. Add `--max-prs 0` to publish eligible cached results without calling Copilot. The shared files contain thread IDs, finding types, outcomes, duplicate references, and public evidence links. They omit raw review text, code, model explanations, and intermediate judgments.

On another machine, run `python scorecard.py load-data` and then `report --window START END` to use those results without calling Copilot. Use the saved `assessment_version`, model ID, and reasoning effort to report on an older run with the same inputs.

## How the agent makes its calls

The classifier sends only the inline thread openers and the IDs, times, and commented paths. It classifies them and links clear duplicates to their originals. If a PR exceeds the input budget, it classifies the openers in batches, then checks the finding summaries for duplicates across the whole PR. The outcome pass examines each original finding separately using public GitHub patches and file snapshots from the reviewed revision, later commits and the merged revision. The program checks that evidence links come from those inputs. One Copilot SDK client handles the run, with a fresh session per request, a task-specific system prompt that replaces the default agent prompt, and no tools. Only public evidence goes to Copilot.

A thread opens when GitHub publishes it, not when it was drafted. The scorecard asks whether the issue shown in the reviewed revision is gone at merge. A rewrite can address a finding without keeping the same patch. If the issue returns before merge, the outcome is Unaddressed and the audit can record that it regressed. The audit records a separate timing result if a patch shows the fix. That result compares the commit's date with publication; it does not prove when the change was pushed or what caused it. Unclear means the code evidence does not settle the merge outcome. N/M means no assessment is available. GitHub does not expose deleted comments, so the report only counts observable thread openers.

Classification and outcome judgments have separate cache keys based on their prompts, model, reasoning effort, and inputs. Completed judgments are saved even if a later judgment fails. Editing the outcome prompt does not invalidate classifications. Each completed assessment has a versioned JSON audit file under `audit` next to the database, with reasoning and evidence links. Use `report --assessment-version HASH` to render a previous version using the full `assessment_version` in its audit file. The aggregate HTML contains no raw comments or individual assessments.

When changing the prompts or assessment rules, set a new `ASSESSMENT_VERSION` in `assessment.py`. Changes to what capture collects already change the evidence digest if they produce a different bundle.
