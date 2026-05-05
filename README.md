# leaderboard

Self-hosted contributor impact leaderboard for any GitHub repo. Generates a
single static HTML page from `git log` + GitHub PR/review data, with
hotspot-weighted lines, identity dedup via `.mailmap`, and time slices
(all-time / 30 days / 7 days). No database, no service, no plugin system —
three scripts and a JSON blob.

## Quick start

```bash
# 1. Copy the example config and edit
cp config/example.env config/myrepo.env
$EDITOR config/myrepo.env       # set REPO_DIR, REPO_OWNER, REPO_NAME

# 2. Build
./refresh.sh --config myrepo

# 3. Open the result
xdg-open out/index.html
```

## Layout

```
config/
  example.env       # template config — committed
  *.env             # per-repo configs — gitignored
  *.json            # identity / bots files — gitignored
scripts/
  pull_all_data.sh  # git log + gh GraphQL → $DATA_DIR
  build_report.py   # parse + aggregate → report_data.json
  render_html.py    # report_data.json → out/index.html
refresh.sh          # entry point
out/                # rendered HTML (gitignored)
```

## Deploying

`refresh.sh --deploy` rsyncs `$OUT_DIR/` to `$DEPLOY_TARGET` (set in your
config). Cron, every 30 minutes:

```
*/30 * * * *  /path/to/leaderboard/refresh.sh --config /path/to/myrepo.env --deploy >> /tmp/leaderboard.log 2>&1
```

## Identity dedup

Author identity is normalised through `git log --use-mailmap`, which reads a
`.mailmap` file at the **target repo's** root. For richer dedup (and to join
git data with PR data, which uses GitHub logins), point `IDENTITIES_FILE` at a
JSON file:

```json
{
  "Display Name": "gh_login",
  "Other Person": "their_login"
}
```

Bots and AI reviewers are excluded via `BOTS_FILE` (a JSON array of names/logins).

## Trigger-bot re-attribution (optional)

If your repo has an automation account that opens PRs in response to issues,
set `TRIGGER_BOT_LOGIN` to its GitHub login and the build will try to
re-attribute each PR to the human who filed the originating issue. Resolution
order: PR body `Requested by @user` → linked-issue body `Reported by @user` →
issue creator.

## Re-implementation flagging (always on)

A `Possible re-implementation pairs` panel lists `(closed PR by A, merged PR by B)`
where the file sets overlap and the merge happened within ±90 days of the close.
Heuristic only — false positives include parallel work, post-merge follow-ups, and
two people both fixing the same hot file. Flag-only by default; doesn't change any
leaderboard number.

## Similarity-based credit delta (opt-in, off by default)

Set `SIMILARITY_CREDIT_DELTA=1` in your config to shift PR credit from the merged
author to the closed author for each flagged pair (where merge came after close).
Shift = file-set Jaccard, capped at `SIMILARITY_DELTA_CAP` (default 0.4) per
merged PR. Rework attribution does not shift. Use sparingly — propagates the
flag's false-positive rate into leaderboard numbers. The page shows a badge in
the header when this is active.

## What's measured

- Commits, lines added/deleted, files touched
- Merged PRs, human reviews given (bots/AI reviewers excluded)
- Hotspot-weighted lines (file weight = 0.5×–2.0× by commit-count percentile)
- Per-package contribution (under `packages/*`, `apps/*`, etc.)
- Per-week activity sparkline

## Methodology and biases

Every metric encodes a value system. The page renders multiple columns
side-by-side and lets the viewer sort — there's no single composite "score."
See the in-page "Reasoning & Methodology" section for the full rundown,
including known blind spots (architecture decisions, design docs, off-platform
work, intra-PR review depth, code criticality beyond churn).

## Requirements

- `git`, `bash`, `python3`, `jq`, `rsync`
- `gh` CLI authenticated against the target repo
- Target repo cloned locally (`refresh.sh` does **not** `git pull` — keep it up
  to date on whatever schedule suits you)

## License

MIT.
