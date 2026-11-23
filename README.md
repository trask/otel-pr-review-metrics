# otel-pr-review-metrics

Measure inline review feedback across four public OpenTelemetry repositories. The scorecard does not run the custom reviewer, write to source repositories, or update Azure DevOps. Sharing data writes only to this repository's `data` branch.

## Run the scorecard

Use Python 3.10 or newer. Capture needs Git and a GitHub CLI authenticated to `github.com`. Assessment needs an authenticated GitHub Copilot CLI. Reporting needs only Python.

```sh
python scorecard.py load-data
python scorecard.py report
```

`capture` archives merged PRs and their inline review data from October 1, 2026 through today in UTC. Rerun it to pick up new merges. It saves every completed PR as it goes, skips finalized days, and rechecks today and yesterday to allow for GitHub search indexing delays. It does not fetch threads again for PRs already saved. If the rate limit will reset within 60 seconds, capture waits and retries; otherwise it stops with a retry time and keeps its progress. Set `--max-rate-wait SECONDS` to change the wait, `--from` to backfill an earlier start, or `--through` to cap the backfill.

The local archive defaults to `~/.otel-pr-review-metrics/archive.sqlite3`, outside the worktree. Use `--archive PATH` to use another local file. `load-data` downloads the public `data` branch over Git without calling the GitHub API. The branch stores a coverage file per repository and, by UTC merge month, a JSON file per merged PR plus a compressed evidence file where one has been prepared.

```sh
python scorecard.py capture --with-evidence --publish-data
```

`--shared` loads the branch before capture without pushing. `capture --publish-data` loads it and pushes updated inputs as a new commit. It leaves existing shared assessments in place without publishing new ones. Local Git repositories and API response caches stay off the branch. If another run updates `data` before the push, rerun to load its latest snapshot.

`capture --with-evidence` fetches the reviewed, head and merge commits into local Git repositories. It saves patches and file snapshots for the commented path and up to three related changed paths per thread. It uses public REST evidence only when Git cannot supply it. The bundle records which evidence was unavailable or limited. Use `--max-evidence-prs 25` to prepare a batch and resume on the next run. Capture makes no model calls.

`report` reads the archive offline and writes `.scorecard/scorecard.html`. Its default window is October 1 through November 15, 2026. To report on another range, use inclusive UTC merge dates:

```sh
python scorecard.py report --window 2026-10-15 2026-11-20
```

The report includes PRs opened before the window and PRs with no inline threads. Dates capture has not finalized are distinct from days with zero merges. To compare with a later window, supply the custom reviewer's exact GitHub login:

```sh
python scorecard.py report --follow-up YYYY-MM-DD YYYY-MM-DD --custom-reviewer 'your-reviewer[bot]'
```

Repeat `--custom-reviewer` for aliases. Use `--other-bot LOGIN` for unrelated automation using a GitHub User account. GitHub's Bot actor type and `[bot]` logins identify other automation automatically. Remaining User accounts count as Human. If the baseline is run without a custom login, its Automated total uses Copilot only and the Custom reviewer row is marked not measured.

Run `python scorecard.py assess` to measure findings and render the report. Assessment reads the prepared local inputs and calls Copilot without fetching code or PR data. The default model is GPT-6.2 Sol (`gpt-6.2-sol`). PRs without a prepared bundle are not measured. If a bundle exists but the required evidence is missing, the outcome is Unclear. Use `--max-prs 5` for a bounded trial. Use the same `--model` and assessment version across both windows.

Use `python scorecard.py assess --publish-data` to load the shared inputs and previously published results, assess missing PRs, and publish completed decisions to `data`. The run saves locally after each PR and pushes every 25 new assessments and at the end. Add `--max-prs 0` to publish eligible cached results without calling Copilot. The shared files contain thread IDs, finding types, outcomes, duplicate references, and public evidence links. They omit raw review text, code, model explanations, and intermediate judgments.

On another machine, run `python scorecard.py load-data` and then `report --window START END` to use those results without calling Copilot. Versioned files keep earlier published results when prompts change. Use the saved `assessment_version` and model ID to report on an older version with the same inputs.

## How the agent makes its calls

The first model pass reads the inline thread openers on a PR, classifies what they flag, links clear duplicates to their originals, and selects relevant files. The second pass examines each original finding using public GitHub patches and file snapshots from the reviewed revision, later commits and the merged revision. The program checks that evidence links come from those inputs. The model has no tools; the collector alone makes read-only GitHub requests. Only public evidence goes to Copilot.

A thread opens when GitHub publishes it, not when it was drafted. A commit timestamp is not proof of when a change was pushed or why it was made. The agent uses Unclear if a matching change cannot be placed before or after publication, or if the evidence is otherwise insufficient. The scorecard marks missing assessments as N/M. GitHub does not expose deleted comments, so the report can only measure threads whose openers remain observable.

Classification and outcome judgments have separate cache keys based on their prompts, model, and inputs. Completed judgments are saved even if a later judgment fails. Editing the outcome prompt does not invalidate classifications. Each completed assessment has a versioned JSON audit file under `audit` next to the database, with reasoning and evidence links. Use `report --assessment-version HASH` to render a previous version using the full `assessment_version` in its audit file and the same model ID. The aggregate HTML contains no raw comments or individual assessments.
