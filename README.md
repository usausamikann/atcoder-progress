# atcoder-progress

Archive and summarize the **public** [AtCoder](https://atcoder.jp/users/usausamikann) submission history of `usausamikann`. This repository is updated by GitHub Actions, using the unofficial [AtCoder Problems API](https://github.com/kenkoooo/AtCoderProblems/blob/main/doc/api.md).

It is deliberately **not** a repository of source-code submissions: only verdicts, submission IDs, timestamps, runtime and the other public API fields are stored.

## Data contract

| File | Purpose |
| --- | --- |
| `data/summary.json` | **Start here:** counts, ABC A–H attempts/AC, recent distinct AC problems, recent daily activity |
| `data/recent.json` | 100 latest submissions with task/submission links |
| `data/problems.json` | One row per **attempted** problem, including ever-AC, submission attempts, first AC, last verdict, estimated difficulty and title |
| `data/catalog.json` | Metadata for attempted problems only, sourced from AtCoder Problems resources |
| `data/submissions/YYYY.json` | Complete raw submissions for the given **UTC** calendar year, sorted by `(epoch_second, id)` |

All files are UTF-8 JSON with deterministic formatting. Missing `data/` files before the first successful run are normal.

**Important semantics:** `solved` means at least one AC has ever been recorded, even if a later submission was WA. `unresolved` means *attempted but never AC*; it does **not** include problems never submitted to AtCoder. The `difficulty` field is an **estimate** published by AtCoder Problems, not an official AtCoder score, and may be `null`. The available API does not tell whether a problem was solved locally without a submission. The API can lag behind actual AtCoder submissions.

The summary's `recently_solved_problems` includes the 30 problems with the newest *first AC* timestamps. `submissions_by_jst_day_recent_90` reports the latest 90 active days (not necessarily the past 90 calendar days).

## Sync design

- The first run fetches all historical submissions in batches of up to **500**. Subsequent runs overlap the latest second for safety and deduplicate by submission ID.
- Overlapping pages are necessary because multiple submissions can share one second. If the API has 500+ submissions with the same timestamp, the API has no offset mechanism that can guarantee completeness, so the sync **fails** rather than silently dropping a submission.
- Queries are spaced at least 1.3 seconds apart, with bounded exponential retries for transient errors. Failed fetches never write partial data.
- Metadata is refreshed when new submissions arrive, and separately every Sunday. Storing only metadata for attempted problems keeps the repository compact.
- Sharding raw records by UTC year avoids large single-file reads. Views are derived artifacts; raw submissions remain the canonical source.
- Files are only rewritten when their content actually changes. The scheduled Action makes no commit if there is no change.
- GitHub Actions update runs are serialized. The sync workflow is the only workflow needing `contents: write`; PR tests have read-only permissions. No personal access token is needed.

## Automation

[Sync workflow](../../actions/workflows/sync.yml) runs at 03:17, 09:17, 15:17 and 21:17 JST. The weekly metadata refresh runs Sunday 11:47 JST. GitHub schedules are best-effort and may run late or skip runs.

### First-time setup (repository owner)

1. Merge the implementation pull request into `main`. This also triggers an **initial sync** because the workflow or script changed. Scheduled workflows only run from the default branch.
2. In **Settings → Actions → General**, confirm Actions are enabled. Under **Workflow permissions**, allow **Read and write permissions** for `GITHUB_TOKEN` if the repository's default setting blocks pushes. The workflow itself requests only `contents: write`.
3. In the **Actions** tab, check the automatically started **Sync AtCoder submissions** run after merging. To retry manually, choose **Run workflow** on branch `main`.
4. Confirm that the run succeeds and the generated `data/summary.json` exists on `main`. The initial history fetch can take longer than later updates.
5. If `git push` fails with permission denied or branch protection, inspect that job's log. Configure access appropriately rather than adding an unnecessary PAT.

### Local usage

Requires **Python 3.12+**, with no third-party packages.

```sh
python -m unittest discover -s tests -v
python scripts/sync.py
python scripts/sync.py --refresh-catalog
```

Running `sync.py` locally reads/writes the local `data/` directory only. It never pushes to GitHub on its own.

## Reading progress from ChatGPT

Once the connected GitHub app has access to this repository, ask ChatGPT to read:

- `usausamikann/atcoder-progress` → `data/summary.json` for the latest high-level snapshot;
- `data/problems.json` to find attempted-but-not-AC problems or filter difficulty/index;
- `data/recent.json` for latest verdicts; or
- `data/submissions/YYYY.json` for full historical submission-level analysis.

**Freshness:** these are snapshots, not a live query to AtCoder. When answering, check the latest submission timestamp in `summary.json` and the last successful GitHub Action run. The chat connector reads files when asked; it does not automatically receive push notifications.

## Maintenance

AtCoder Problems is an unofficial API, and its format or availability may change. Its maintainers ask clients not to call it more than once per second; the updater respects that limit. If a scheduled Action fails, inspect logs before re-running. GitHub may automatically disable schedules for inactive public repositories; re-enable them if that happens.

No secrets are used. Avoid committing credentials or private study notes to this public repository.
