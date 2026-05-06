#!/usr/bin/env python3
"""Process all data sources into a JSON blob + render the HTML report."""

import json, math, os, re, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/_data"))
REPO_FIRST_COMMIT = os.environ.get("REPO_FIRST_COMMIT", "")

# ─── Feature flags (all default OFF) ──────────────────────────────────────────
# These gate the contested / opinionated signals. Teams opt in via config.
def _flag(name: str) -> bool:
    return os.environ.get(name, "0") == "1"

SHOW_ITERATION  = _flag("SHOW_ITERATION")    # per-author iteration column
SHOW_SIMILARITY = _flag("SHOW_SIMILARITY")   # re-implementation suspects panel
SHOW_STREAK     = _flag("SHOW_STREAK")       # per-author streak + active weeks columns
SHOW_WEEKLY_CHART = _flag("SHOW_WEEKLY_CHART")    # stacked weekly contribution chart
SHOW_REVIEW_IMPACT = _flag("SHOW_REVIEW_IMPACT")  # per-author review_impact column
SHOW_COMPOSITE_IMPACT = _flag("SHOW_COMPOSITE_IMPACT")   # weighted_lines + α·review_impact
WEIGHT_REVIEWS_BY_DEPTH = _flag("WEIGHT_REVIEWS_BY_DEPTH")  # depth-weight reviews

# Reviewer credit shift: each non-self-author reviewer earns this fraction of
# the PR's credit (and weighted_lines) — pulled away from the PR's authors.
# Set to 0 to disable (default). Capped per-PR by REVIEWER_CREDIT_CAP.
REVIEWER_CREDIT_RATE = float(os.environ.get("REVIEWER_CREDIT_RATE", "0"))
REVIEWER_CREDIT_CAP  = float(os.environ.get("REVIEWER_CREDIT_CAP",  "0.3"))
# Composite impact = weighted_lines + COMPOSITE_REVIEW_ALPHA × review_impact.
COMPOSITE_REVIEW_ALPHA = float(os.environ.get("COMPOSITE_REVIEW_ALPHA", "0.1"))

# Recover authorship lost at repo migration: if a PR was merged into a
# predecessor repo and the imported codebase squashed everyone's work into
# a single root commit, the rightful authors are unrecoverable from git
# alone. When this is on, lines/weighted-lines/files for PRs merged BEFORE
# the repo's first commit are credited via PR data (additions, files, and
# commit-author shares) instead. Default off because it changes semantics.
INCLUDE_PREHISTORY_PRS = _flag("INCLUDE_PREHISTORY_PRS")

# Weekly chart configuration (only consumed if SHOW_WEEKLY_CHART is on).
WEEKLY_CHART_METRIC = os.environ.get("WEEKLY_CHART_METRIC", "weighted_lines")
# valid: commits | lines_added | weighted_lines | prs | reviews
WEEKLY_CHART_POSITION = os.environ.get("WEEKLY_CHART_POSITION", "before_leaderboard")
# valid: before_leaderboard | after_leaderboard | after_packages | after_standouts | after_hotspots
WEEKLY_CHART_TOP_N = int(os.environ.get("WEEKLY_CHART_TOP_N", "10"))

# Extra time slices to include in the toggle. Comma-separated codes from:
#   q90  = last 90 days   (quarter)
#   h180 = last 180 days  (half-year)
#   y365 = last 365 days  (year)
# Default: none (toggle stays at all/30d/7d).
EXTRA_SLICES_REQUESTED = [s.strip() for s in os.environ.get("EXTRA_SLICES","").split(",") if s.strip()]

# ─── Config ────────────────────────────────────────────────────────────────────

def _load_json(env_var, default):
    path = os.environ.get(env_var)
    if not path:
        return default
    p = Path(path)
    if not p.exists():
        print(f"WARN: {env_var}={path} not found; using empty default", file=sys.stderr)
        return default
    return json.loads(p.read_text())

# Canonical name -> github login (joins git author names with PR author logins).
# Format: {"Display Name": "gh_login", ...}.
NAME_TO_GH: dict[str, str] = _load_json("IDENTITIES_FILE", {})
GH_TO_NAME = {v: k for k, v in NAME_TO_GH.items()}

# Names/logins to exclude entirely (bots, AI reviewers, etc.)
BOTS: set[str] = set(_load_json("BOTS_FILE", []))

# Optional: re-attribute PRs opened by an automation account to the human who
# filed the originating issue. The trigger map is built by pull_all_data.sh.
TRIGGER_BOT_LOGIN = os.environ.get("TRIGGER_BOT_LOGIN", "")
TRIGGER_BOT_TRIGGERS: dict[int, str] = {}
if TRIGGER_BOT_LOGIN:
    try:
        with open(DATA_DIR / "trigger_bot_triggers.tsv") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 2 and parts[0].isdigit():
                    TRIGGER_BOT_TRIGGERS[int(parts[0])] = parts[1]
    except FileNotFoundError:
        pass

NOW = datetime.now(timezone.utc)

# Slice ordering matters for the toggle UI (longest → shortest).
SLICE_BASE = [
    ("all",  None,                 "All-time"),
    ("d30",  timedelta(days=30),   "Last 30 days"),
    ("d7",   timedelta(days=7),    "Last 7 days"),
]
SLICE_EXTRAS = {
    "d60":  (timedelta(days=60),   "Last 60 days"),
    "q90":  (timedelta(days=90),   "Last 90 days"),
    "h180": (timedelta(days=180),  "Last 6 months"),
    "y365": (timedelta(days=365),  "Last year"),
}
# Insert extras after "all" in length order: y365, h180, q90, d60, then d30, d7
EXTRAS_IN_ORDER = ["y365", "h180", "q90", "d60"]
slices_ordered = [SLICE_BASE[0]]
for code in EXTRAS_IN_ORDER:
    if code in EXTRA_SLICES_REQUESTED:
        delta, label = SLICE_EXTRAS[code]
        slices_ordered.append((code, delta, label))
slices_ordered.extend(SLICE_BASE[1:])

SLICES = {code: delta for code, delta, _ in slices_ordered}
SLICE_LABELS = {code: label for code, _, label in slices_ordered}

def in_slice(ts: datetime, name: str) -> bool:
    delta = SLICES[name]
    return delta is None or (NOW - ts) <= delta

# ─── 1. Parse commits + numstat ────────────────────────────────────────────────

# State per slice: name -> dict
def empty_stats():
    return {"commits": 0, "add": 0, "del": 0, "files_touched": set()}

# slices[slice_name][canonical_name] = stats
slices_data = {s: defaultdict(empty_stats) for s in SLICES}

# per-file per-author lines (all-time, used for ownership table + hotspot weighting)
per_file_author_add = defaultdict(lambda: defaultdict(int))  # file -> name -> add
per_author_file_add = defaultdict(lambda: defaultdict(int))  # name -> file -> add

# Same, but bucketed per time slice so the W. Lines column can be slice-aware.
# slice_name -> name -> file -> add
per_author_file_add_slice = {s: defaultdict(lambda: defaultdict(int)) for s in SLICES}

# Commits per week per author (for sparkline)
commits_per_week = defaultdict(lambda: defaultdict(int))  # name -> iso-week -> count
weeks_seen = set()

# Per-author set of ISO weeks where they had ANY activity (commit, PR merge, or
# review). Used by the optional streak/active-weeks columns.
weekly_activity: dict[str, set[str]] = defaultdict(set)
earliest_activity_ts: datetime | None = None
# Earliest git commit timestamp on main — anchors the cutoff for "pre-history"
# PR detection. Set during commit parsing.
first_main_commit_ts: datetime | None = None

# Per-week per-author counters for the optional weekly chart. Keyed by
# display-name to match the contributor record.
lines_added_per_week:    dict = defaultdict(lambda: defaultdict(int))    # name -> week -> add
prs_per_week:            dict = defaultdict(lambda: defaultdict(int))    # name -> week -> count
reviews_per_week:        dict = defaultdict(lambda: defaultdict(int))    # name -> week -> count
# Per-week per-author per-file lines, used to compute weighted_lines per week
# AFTER hotspot weights are derived (we don't have weights yet at commit-parse).
file_lines_per_week:     dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
# name -> week -> file -> add

# Identify the migration-root SHA (the chronologically OLDEST commit with no
# parent) so we can skip it when INCLUDE_PREHISTORY_PRS is on. Skipping every
# orphan would silently zero out unrelated branches that were grafted later.
migration_root_sha: str | None = None
if INCLUDE_PREHISTORY_PRS:
    earliest_orphan_ts = None
    with open(DATA_DIR / "commits_numstat.tsv") as f:
        for line in f:
            if not line.startswith("COMMIT\t"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            sha, ts_str, _author, parents = parts[1], parts[2], parts[3], parts[4]
            if parents.strip():        # has at least one parent — not orphan
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            if earliest_orphan_ts is None or ts < earliest_orphan_ts:
                earliest_orphan_ts = ts
                migration_root_sha = sha

# Parse the raw git log
current_author = None
current_ts = None
current_sha = None
seen_commit = set()  # to count commits once per author

with open(DATA_DIR / "commits_numstat.tsv") as f:
    for line in f:
        line = line.rstrip("\n")
        if line.startswith("COMMIT\t"):
            parts = line.split("\t")
            current_sha = parts[1]
            current_ts = datetime.fromisoformat(parts[2])
            current_author = parts[3]
            # Skip the migration root commit when pre-history PR recovery is on.
            # Its 80K+ lines would double-count: pre-history PRs already credit
            # those lines to their actual authors via PR data. We only skip the
            # one true migration root (oldest orphan), not every orphan.
            if INCLUDE_PREHISTORY_PRS and current_sha == migration_root_sha:
                print(f"Skipping migration root {current_sha[:8]} ({current_author}): "
                      f"its lines are credited via pre-history PRs", file=sys.stderr)
                current_author = None
                continue
            if current_author in BOTS:
                current_author = None
                continue
            week = current_ts.strftime("%G-W%V")
            weeks_seen.add(week)
            commits_per_week[current_author][week] += 1
            weekly_activity[current_author].add(week)
            if earliest_activity_ts is None or current_ts < earliest_activity_ts:
                earliest_activity_ts = current_ts
            if first_main_commit_ts is None or current_ts < first_main_commit_ts:
                first_main_commit_ts = current_ts
            for s in SLICES:
                if in_slice(current_ts, s):
                    if current_sha not in seen_commit or True:
                        # Count commit once per slice
                        slices_data[s][current_author]["commits"] += 1
            seen_commit.add(current_sha)
        elif current_author and "\t" in line:
            parts = line.split("\t")
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                add, dele, path = int(parts[0]), int(parts[1]), parts[2]
                # Skip lockfiles and generated
                if any(skip in path for skip in ["pnpm-lock.yaml", "package-lock.json", ".snap"]):
                    continue
                for s in SLICES:
                    if in_slice(current_ts, s):
                        slices_data[s][current_author]["add"] += add
                        slices_data[s][current_author]["del"] += dele
                        slices_data[s][current_author]["files_touched"].add(path)
                        per_author_file_add_slice[s][current_author][path] += add
                # All-time per-file per-author tracking
                per_file_author_add[path][current_author] += add
                per_author_file_add[current_author][path] += add
                # Per-week per-author tracking (for the weekly chart, when on)
                if SHOW_WEEKLY_CHART:
                    lines_added_per_week[current_author][week] += add
                    file_lines_per_week[current_author][week][path] += add

# files_touched stays a set through PR parsing (pre-history augmentation needs
# to .add() to it). It's converted to a count when contributor records are
# built (section 10 handles either form).

print(f"Parsed git data: {len(slices_data['all'])} authors all-time, "
      f"{len(slices_data['d30'])} active in last 30d", file=sys.stderr)

# ─── 2. Parse PR data + reviews ────────────────────────────────────────────────

# Load all PRs into memory (small — typically a few hundred to low thousands).
# GitHub's cursor pagination can emit the same PR on adjacent pages when the
# sort key (createdAt) ties; dedupe by PR number to keep counts honest.
_seen_pr_nums: set[int] = set()
all_prs = []
with open(DATA_DIR / "prs.jsonl") as f:
    for line in f:
        pr = json.loads(line)
        n = pr.get("number")
        if n in _seen_pr_nums:
            continue
        _seen_pr_nums.add(n)
        all_prs.append(pr)

# Detect "iteration": a PR is iterated-on if either
#   (a) a later merged Revert PR points at it, OR
#   (b) a later merged "fix"-titled PR by a different author touches ≥ N of its
#       files within the iteration window (default 30 days).
# Skipped entirely when SHOW_ITERATION is off.
REVERT_TITLE_RE = re.compile(r'^\s*revert\b', re.IGNORECASE)
TITLE_HASH_RE   = re.compile(r'\(#(\d+)\)')
TITLE_PR_RE     = re.compile(r'\bPR\s*#(\d+)', re.IGNORECASE)
BODY_REVERTS_RE = re.compile(r'(?im)\breverts?\s+(?:[\w.-]+/[\w.-]+)?#(\d+)')
FIX_TITLE_RE    = re.compile(r'^\s*fix\b', re.IGNORECASE)

ITERATION_WINDOW_DAYS = int(os.environ.get("ITERATION_WINDOW_DAYS", "30"))
ITERATION_MIN_SHARED  = int(os.environ.get("ITERATION_MIN_SHARED",  "2"))

# Always load file lists if available — the similarity scan needs them too.
# pr_file_nodes retains additions per file for pre-history line recovery.
files_by_pr:    dict[int, set] = {}
pr_file_nodes:  dict[int, list] = {}
try:
    with open(DATA_DIR / "pr_files.jsonl") as f:
        for line in f:
            p = json.loads(line)
            nodes = [n for n in p.get("files", {}).get("nodes", []) if n.get("path")]
            files_by_pr[p["number"]]    = set(n["path"] for n in nodes)
            pr_file_nodes[p["number"]]  = nodes
except FileNotFoundError:
    pass

iterated_pr_numbers: set[int] = set()
fix_followed_by: dict[int, list[tuple[int, str]]] = defaultdict(list)
if SHOW_ITERATION:
    # (a) Strict reverts via title/body cross-reference. Track the chain so we
    # can un-revert when a reverter was itself reverted (net effect: original
    # PR's code is back, so it shouldn't count as iterated).
    reverted_by: dict[int, int] = {}     # target_pr_number -> reverter_pr_number
    for pr in all_prs:
        title = pr.get("title") or ""
        body  = pr.get("body")  or ""
        if not REVERT_TITLE_RE.search(title):
            continue
        target = None
        for rx in (TITLE_HASH_RE, TITLE_PR_RE, BODY_REVERTS_RE):
            m = rx.search(title) or rx.search(body)
            if m:
                target = int(m.group(1))
                break
        if target and target != pr["number"]:
            reverted_by[target] = pr["number"]

    # A revert can itself be reverted ("Revert of Revert"). Net effect on the
    # original is a parity walk: stuck(P) = not stuck(reverter(P)). Code at
    # HEAD differs from P iff the chain length is odd.
    _stuck_cache: dict[int, bool] = {}
    def stuck(num: int) -> bool:
        if num in _stuck_cache:
            return _stuck_cache[num]
        if num not in reverted_by:
            _stuck_cache[num] = True
            return True
        result = not stuck(reverted_by[num])
        _stuck_cache[num] = result
        return result

    reverted_pr_numbers: set[int] = {
        target for target in reverted_by
        if not stuck(target)        # only count PRs actually missing from HEAD
    }

    # (b) Fix-followed: walk merged PRs in time order
    merged_sorted = sorted(
        [pr for pr in all_prs if pr.get("mergedAt")],
        key=lambda pr: pr["mergedAt"],
    )
    fix_followed_pr_numbers: set[int] = set()
    # iterated PR -> list of (fixer_pr_number, fixer_author_login). Used by
    # the optional iteration credit delta below; for the binary iteration
    # signal we just take the keys.
    for i, p in enumerate(merged_sorted):
        p_num    = p["number"]
        p_author = (p.get("author") or {}).get("login")
        if not p_author or p_author in BOTS:
            continue
        p_files  = files_by_pr.get(p_num, set())
        if len(p_files) < ITERATION_MIN_SHARED:
            continue
        p_dt = datetime.fromisoformat(p["mergedAt"].replace("Z", "+00:00"))
        for q in merged_sorted[i + 1:]:
            q_dt = datetime.fromisoformat(q["mergedAt"].replace("Z", "+00:00"))
            if (q_dt - p_dt).days > ITERATION_WINDOW_DAYS:
                break
            q_author = (q.get("author") or {}).get("login")
            if not q_author or q_author == p_author or q_author in BOTS:
                continue
            if not FIX_TITLE_RE.search(q.get("title") or ""):
                continue
            # Skip fixes whose own changes were reverted out — net effect on P
            # is no iteration.
            if not stuck(q["number"]):
                continue
            q_files = files_by_pr.get(q["number"], set())
            if len(p_files & q_files) >= ITERATION_MIN_SHARED:
                fix_followed_pr_numbers.add(p_num)
                fix_followed_by[p_num].append((q["number"], q_author))
                # Keep collecting; multiple fixers may each earn iteration credit.

    iterated_pr_numbers = reverted_pr_numbers | fix_followed_pr_numbers
    print(f"Iteration signal: {len(iterated_pr_numbers)} PRs iterated on "
          f"({len(reverted_pr_numbers)} reverts + {len(fix_followed_pr_numbers)} fix-followed)",
          file=sys.stderr)
else:
    print("Iteration signal: disabled (set SHOW_ITERATION=1 to enable)",
          file=sys.stderr)

def empty_pr_stats():
    return {"prs": 0, "pr_credit": 0.0,
            "prs_iterated": 0, "pr_credit_iterated": 0.0,
            "reviews": 0,
            "ttm_seconds": 0, "ttm_count": 0,
            "pr_commit_total": 0, "pr_count_for_commits": 0}

pr_slices = {s: defaultdict(empty_pr_stats) for s in SLICES}

# Per-PR reviewer roster — captured during PR parsing, consumed after file
# weights are derived for review_impact aggregation and the optional reviewer
# credit shift. Each entry is (login, review_weight, ts, comments).
pr_reviewers: dict[int, list] = {}

# Per-PR record of who got credit and in which slices — needed to subtract
# specific shares later when applying similarity-based credit shifts.
pr_credit_log: dict[int, dict] = {}    # pr_number -> {"shares": {login: share}, "slices": [...]}

def compute_pr_shares(pr, fallback_login):
    """
    Split PR credit across commit authors by share of additions. Bot-authored
    commits (Copilot, Cursor agent, etc.) inside a human's PR get attributed
    to the PR opener — they're supervising the tool, not the tool itself.
    Returns {gh_login: share_in_[0,1]}.
    """
    author_adds = defaultdict(int)
    total = 0
    for cnode in pr.get("commits", {}).get("nodes", []):
        c = cnode.get("commit") or {}
        a = c.get("author") or {}
        u_obj = a.get("user") or {}
        login = u_obj.get("login") or fallback_login
        if login in BOTS:
            login = fallback_login    # bot's commits → PR opener
        adds = c.get("additions", 0) or 0
        author_adds[login] += adds
        total += adds
    if total > 0:
        return {u: adds / total for u, adds in author_adds.items()}
    return {fallback_login: 1.0}

for pr in all_prs:
    if not pr.get("author"):
        continue
    pr_author_login = pr["author"]["login"]

    # Re-attribute trigger-bot PRs to the human who filed the originating issue.
    # Done BEFORE the BOTS skip so the trigger bot can also live in BOTS (which
    # excludes its reviews/commits) without dropping the PRs it opened.
    if TRIGGER_BOT_LOGIN and pr_author_login == TRIGGER_BOT_LOGIN:
        trigger = TRIGGER_BOT_TRIGGERS.get(pr["number"])
        if trigger:
            pr_author_login = trigger
            shares = {trigger: 1.0}
        else:
            # No human trigger found — fall through; if the bot is in BOTS,
            # the next check drops it entirely (correct for unattributed bot PRs).
            shares = {pr_author_login: 1.0}
    else:
        shares = compute_pr_shares(pr, pr_author_login)

    if pr_author_login in BOTS:
        continue

    merged_at  = datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00"))
    created_at = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))

    # Pre-history line recovery: PRs merged before this repo's first commit
    # had their commits squashed into the migration root, losing per-author
    # line attribution. Credit them via PR data instead.
    is_prehistory = (
        INCLUDE_PREHISTORY_PRS
        and first_main_commit_ts is not None
        and merged_at < first_main_commit_ts
    )
    if is_prehistory:
        pr_nodes = pr_file_nodes.get(pr["number"], [])
        pr_total_add = pr.get("additions", 0) or 0
        pr_total_del = pr.get("deletions", 0) or 0
        week_of_merge = merged_at.strftime("%G-W%V")
        for u, share in shares.items():
            if u in BOTS:
                continue
            display = GH_TO_NAME.get(u, u)
            attr_add = int(round(pr_total_add * share))
            attr_del = int(round(pr_total_del * share))
            for s in SLICES:
                if not in_slice(merged_at, s):
                    continue
                slices_data[s][display]["add"] += attr_add
                slices_data[s][display]["del"] += attr_del
            for fnode in pr_nodes:
                fpath = fnode.get("path")
                fadd  = fnode.get("additions", 0) or 0
                if not fpath:
                    continue
                attr_fadd = int(round(fadd * share))
                for s in SLICES:
                    if in_slice(merged_at, s):
                        slices_data[s][display]["files_touched"].add(fpath)
                        if attr_fadd:
                            per_author_file_add_slice[s][display][fpath] += attr_fadd
                if attr_fadd:
                    per_author_file_add[display][fpath] += attr_fadd
                    per_file_author_add[fpath][display] += attr_fadd
                    if SHOW_WEEKLY_CHART:
                        file_lines_per_week[display][week_of_merge][fpath] += attr_fadd
            if SHOW_WEEKLY_CHART:
                lines_added_per_week[display][week_of_merge] += attr_add
    ttm_seconds = (merged_at - created_at).total_seconds()
    commits_count = pr.get("commits", {}).get("totalCount", 0)
    was_iterated = pr["number"] in iterated_pr_numbers

    # Track activity for streak/active-weeks (keyed by display name to match
    # the contributor record). Counts each PR-author and reviewer for the week
    # of the merge / review.
    merge_week = merged_at.strftime("%G-W%V")
    pr_display = GH_TO_NAME.get(pr_author_login, pr_author_login)
    weekly_activity[pr_display].add(merge_week)
    if SHOW_WEEKLY_CHART:
        prs_per_week[pr_display][merge_week] += 1
    if earliest_activity_ts is None or merged_at < earliest_activity_ts:
        earliest_activity_ts = merged_at

    active_slices = []
    for s in SLICES:
        if not in_slice(merged_at, s):
            continue
        active_slices.append(s)
        # Raw count (PR opener) — kept for ttm/legacy
        opener = pr_slices[s][pr_author_login]
        opener["prs"] += 1
        if was_iterated:
            opener["prs_iterated"] += 1
        opener["ttm_seconds"] += ttm_seconds
        opener["ttm_count"] += 1
        if commits_count:
            opener["pr_commit_total"] += commits_count
            opener["pr_count_for_commits"] += 1
        # Fractional credit by line authorship
        for u, share in shares.items():
            stats = pr_slices[s][u]
            stats["pr_credit"] += share
            if was_iterated:
                stats["pr_credit_iterated"] += share

    if active_slices:
        pr_credit_log[pr["number"]] = {
            "shares":           dict(shares),
            "slices":           active_slices,
            "is_prehistory":    is_prehistory,
            "effective_author": pr_author_login,    # after trigger-bot reattribution
        }

    # Reviews
    pr_reviewers_for_credit = []   # (login, weight, ts, comments) for credit shift later
    for review in pr.get("reviews", {}).get("nodes", []):
        if not review.get("author"):
            continue
        r_login = review["author"]["login"]
        if r_login in BOTS or r_login == pr_author_login:  # don't count self-reviews
            continue
        r_ts = datetime.fromisoformat(review["submittedAt"].replace("Z", "+00:00")) if review.get("submittedAt") else merged_at
        review_week = r_ts.strftime("%G-W%V")
        r_display = GH_TO_NAME.get(r_login, r_login)
        weekly_activity[r_display].add(review_week)
        if SHOW_WEEKLY_CHART:
            reviews_per_week[r_display][review_week] += 1
        if earliest_activity_ts is None or r_ts < earliest_activity_ts:
            earliest_activity_ts = r_ts
        # review_weight: 1 by default; with depth weighting, 1 + ln(1 + comments)
        comments = (review.get("comments") or {}).get("totalCount", 0) or 0
        rw = (1 + math.log1p(comments)) if WEIGHT_REVIEWS_BY_DEPTH else 1
        for s in SLICES:
            if in_slice(r_ts, s):
                pr_slices[s][r_login]["reviews"] += rw
        pr_reviewers_for_credit.append((r_login, rw, r_ts, comments))

    if pr_reviewers_for_credit:
        pr_reviewers[pr["number"]] = pr_reviewers_for_credit

print(f"Parsed PR data: {len(pr_slices['all'])} active gh logins all-time", file=sys.stderr)

# Minimum PR sample for the iteration rate to be displayed (else None).
ITERATION_MIN_PRS = 5

# ─── 3. Compute hotspot weights ────────────────────────────────────────────────

file_churn = {}
with open(DATA_DIR / "file_churn_all.txt") as f:
    for line in f:
        line = line.strip()
        if not line: continue
        parts = line.split(None, 1)
        if len(parts) != 2: continue
        count, path = int(parts[0]), parts[1]
        file_churn[path] = count

# Percentile weight = rank-percentile of file's commit count, scaled 0.5 → 2.0
sorted_files = sorted(file_churn.items(), key=lambda x: x[1])
total_files = len(sorted_files)
file_weight = {}
for i, (path, _) in enumerate(sorted_files):
    pct = i / max(total_files - 1, 1)        # 0..1
    file_weight[path] = 0.5 + 1.5 * pct      # 0.5..2.0

print(f"Hotspot weights computed for {len(file_weight)} files", file=sys.stderr)

# ─── 4. Per-author weighted lines (all-time + per slice) ──────────────────────

weighted_lines = defaultdict(float)
for name, files in per_author_file_add.items():
    for path, add in files.items():
        weighted_lines[name] += add * file_weight.get(path, 1.0)

# Per-slice version: same hotspot weights (which are inherently all-time —
# file importance shouldn't shift with the time window), applied to lines
# added inside the slice.
weighted_lines_per_slice: dict = {s: defaultdict(float) for s in SLICES}
for s, by_author in per_author_file_add_slice.items():
    for name, files in by_author.items():
        for path, add in files.items():
            weighted_lines_per_slice[s][name] += add * file_weight.get(path, 1.0)

# Per-week per-author weighted lines, computed now that hotspot weights exist.
weighted_lines_per_week: dict = defaultdict(lambda: defaultdict(float))
if SHOW_WEEKLY_CHART:
    for name, weeks_d in file_lines_per_week.items():
        for week, files in weeks_d.items():
            for path, add in files.items():
                weighted_lines_per_week[name][week] += add * file_weight.get(path, 1.0)
    # Free the per-file detail; we only needed it to derive weighted_lines_per_week.
    file_lines_per_week.clear()

def weekly_metric_value(name: str, week: str) -> float:
    if WEEKLY_CHART_METRIC == "commits":
        return commits_per_week[name].get(week, 0)
    if WEEKLY_CHART_METRIC == "lines_added":
        return lines_added_per_week[name].get(week, 0)
    if WEEKLY_CHART_METRIC == "weighted_lines":
        return weighted_lines_per_week[name].get(week, 0)
    if WEEKLY_CHART_METRIC == "prs":
        return prs_per_week[name].get(week, 0)
    if WEEKLY_CHART_METRIC == "reviews":
        return reviews_per_week[name].get(week, 0)
    return 0

# Per-PR per-author weighted-lines contribution. Lets credit deltas shift the
# headline impact metric in lockstep with PR credit. For non-pre-history PRs
# we attribute the whole PR's weighted lines to the squash author (where git
# log actually puts them); for pre-history PRs we use the per-commit-author
# share (consistent with how lines were credited via PR data above).
#
# Keys are stored as DISPLAY NAMES so the values can be subtracted from
# weighted_lines_per_slice (also keyed by display) without an extra mapping
# step at shift time. This avoids a bug where a contributor missing from
# IDENTITIES_FILE had their weighted_lines stranded under the gh-login key
# while the slice dict held them under their git-author display name.
pr_weighted_by_login: dict = defaultdict(dict)
for pr_num, info in pr_credit_log.items():
    nodes = pr_file_nodes.get(pr_num, [])
    pr_total_w = 0.0
    for n in nodes:
        path = n.get("path")
        adds = n.get("additions", 0) or 0
        if path and adds:
            pr_total_w += adds * file_weight.get(path, 1.0)
    if pr_total_w <= 0:
        continue
    if info.get("is_prehistory"):
        for login, share in info["shares"].items():
            if share > 0:
                pr_weighted_by_login[pr_num][GH_TO_NAME.get(login, login)] = share * pr_total_w
    else:
        eff = info["effective_author"]
        pr_weighted_by_login[pr_num][GH_TO_NAME.get(eff, eff)] = pr_total_w

# Per-PR weighted total — used by reviewer credit / review impact below.
pr_weighted_total: dict[int, float] = {}
for pr_num, by_login in pr_weighted_by_login.items():
    pr_weighted_total[pr_num] = sum(by_login.values())

# ─── 4b. Review impact per slice ────────────────────────────────────────────
# review_impact[slice][display_name] = sum over PRs reviewed of
#   review_weight / total_review_weight × pr_weighted_total
# A reviewer's "share" of a PR's impact is their depth-weighted slice of the
# review attention that PR received. Always emitted (cheap); displayed only
# when SHOW_REVIEW_IMPACT=1.
review_impact_per_slice: dict = {s: defaultdict(float) for s in SLICES}
# Per-PR per-reviewer attribution, kept for the credit-shift phase.
pr_review_attribution: dict[int, dict[str, float]] = {}    # pr_num -> reviewer_login -> credit_share

for pr_num, reviewers in pr_reviewers.items():
    pr_w = pr_weighted_total.get(pr_num, 0)
    if pr_w <= 0 or not reviewers:
        continue
    total_rw = sum(rw for _, rw, _, _ in reviewers) or 1
    for r_login, rw, r_ts, _comments in reviewers:
        share = rw / total_rw
        pr_review_attribution.setdefault(pr_num, {})[r_login] = share
        r_display = GH_TO_NAME.get(r_login, r_login)
        for s in SLICES:
            if in_slice(r_ts, s):
                review_impact_per_slice[s][r_display] += share * pr_w

# ─── 5. Per-author top files (all-time) ────────────────────────────────────────

top_files_per_author = {}
for name, files in per_author_file_add.items():
    sorted_files = sorted(files.items(), key=lambda x: -x[1])[:5]
    top_files_per_author[name] = [[p, n] for p, n in sorted_files]

# ─── 6. Per-author per-package breakdown (all-time) ────────────────────────────

def package_of(path):
    if path.startswith("packages/plugins/"):
        rest = path[len("packages/plugins/"):]
        return f"plugins/{rest.split('/')[0]}"
    if path.startswith("packages/"):
        return path.split("/")[1]
    if path.startswith("apps/"):
        return path.split("/")[1]
    if "/" in path:
        return path.split("/")[0]
    return "(root)"

per_author_packages = defaultdict(lambda: defaultdict(int))
for name, files in per_author_file_add.items():
    for path, add in files.items():
        per_author_packages[name][package_of(path)] += add

# ─── 7. Top hotspots for context display ───────────────────────────────────────

top_hotspots = [{"path": p, "commits": c, "weight": round(file_weight.get(p, 1.0), 2)}
                for p, c in sorted(file_churn.items(), key=lambda x: -x[1])[:15]]

# ─── 8. Diff-similarity (flagging only by default) ─────────────────────────────
# Pairs (closed PR by A, merged PR by B) where A != B, the file sets overlap,
# and the merge happened within a time window of the close. Heuristic only:
# false positives include legitimate parallel work and post-merge follow-ups.

SIMILARITY_MIN_FILES        = 5    # ignore PRs touching < N files
SIMILARITY_TIME_WINDOW_DAYS = 90   # merged within ±N days of closed
SIMILARITY_MAX_PAIRS        = 40   # cap output
# Flag if EITHER:
#   - Jaccard ≥ JACCARD_HI (a tightly matched pair regardless of size), OR
#   - Jaccard ≥ JACCARD_LO AND shared_files ≥ SHARED_LO (substantial absolute
#     overlap even on a big PR with broader scope — catches partial re-impl).
SIMILARITY_JACCARD_HI = 0.5
SIMILARITY_JACCARD_LO = 0.15
SIMILARITY_SHARED_LO  = 10

similarity_pairs = []
if SHOW_SIMILARITY:
    pr_files: dict[int, dict] = {}
    try:
        with open(DATA_DIR / "pr_files.jsonl") as f:
            for line in f:
                pr = json.loads(line)
                if not pr.get("author"):
                    continue
                pr_files[pr["number"]] = {
                    "author": pr["author"]["login"],
                    "title": pr.get("title", ""),
                    "closedAt": datetime.fromisoformat(pr["closedAt"].replace("Z", "+00:00")) if pr.get("closedAt") else None,
                    "mergedAt": datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00")) if pr.get("mergedAt") else None,
                    "files": [n["path"] for n in pr.get("files", {}).get("nodes", []) if n.get("path")],
                }
    except FileNotFoundError:
        pass

    closed_prs = [(n, p) for n, p in pr_files.items()
                  if p["mergedAt"] is None and p["closedAt"] and len(p["files"]) >= SIMILARITY_MIN_FILES]
    merged_prs = [(n, p) for n, p in pr_files.items()
                  if p["mergedAt"] and len(p["files"]) >= SIMILARITY_MIN_FILES]
    print(f"Similarity scan: {len(closed_prs)} closed × {len(merged_prs)} merged",
          file=sys.stderr)
    for cnum, c in closed_prs:
        c_files = set(c["files"])
        c_author = c["author"]
        if c_author in BOTS:
            continue
        for mnum, m in merged_prs:
            if m["author"] == c_author or m["author"] in BOTS:
                continue
            delta_days = (m["mergedAt"] - c["closedAt"]).days
            if abs(delta_days) > SIMILARITY_TIME_WINDOW_DAYS:
                continue
            m_files = set(m["files"])
            inter = c_files & m_files
            if not inter:
                continue
            jaccard = len(inter) / len(c_files | m_files)
            shared = len(inter)
            keep = (jaccard >= SIMILARITY_JACCARD_HI) or \
                   (jaccard >= SIMILARITY_JACCARD_LO and shared >= SIMILARITY_SHARED_LO)
            if keep:
                similarity_pairs.append({
                    "closed":          cnum,
                    "closed_author":   c_author,
                    "closed_title":    c["title"],
                    "merged":          mnum,
                    "merged_author":   m["author"],
                    "merged_title":    m["title"],
                    "file_overlap":    round(jaccard, 2),
                    "shared_files":    len(inter),
                    "delta_days":      delta_days,
                })

    # Sort by absolute shared-file count first (the most concrete signal),
    # tiebreak by overlap percentage. Keeps big-overlap, tight-match, and
    # many-files-in-common pairs near the top together.
    similarity_pairs.sort(key=lambda x: (-x["shared_files"], -x["file_overlap"]))
    similarity_pairs = similarity_pairs[:SIMILARITY_MAX_PAIRS]
    print(f"Similarity flags: {len(similarity_pairs)}", file=sys.stderr)
else:
    print("Similarity scan: disabled (set SHOW_SIMILARITY=1 to enable)",
          file=sys.stderr)

# ─── 9. Optional: apply similarity-based credit deltas ────────────────────────
# Off by default. When enabled, for each flagged pair where the merged PR came
# AFTER the closed PR, shift `jaccard × per-author share` of the merged PR's
# credit from each merged-PR contributor to the closed-PR author. Total shift
# per merged PR is capped at SIMILARITY_DELTA_CAP. Iteration attribution does
# NOT shift — fixes follow whoever wrote the merged code.

CREDIT_DELTA_ENABLED = _flag("SIMILARITY_CREDIT_DELTA") and SHOW_SIMILARITY
CREDIT_DELTA_CAP     = float(os.environ.get("SIMILARITY_DELTA_CAP", "0.4"))
if _flag("SIMILARITY_CREDIT_DELTA") and not SHOW_SIMILARITY:
    print("WARN: SIMILARITY_CREDIT_DELTA requires SHOW_SIMILARITY=1; disabled.",
          file=sys.stderr)

credit_delta_log = []   # list of applied shifts, for transparency
if CREDIT_DELTA_ENABLED:
    pr_total_shifted: dict[int, float] = defaultdict(float)
    for pair in similarity_pairs:
        if pair["delta_days"] <= 0:
            continue
        m_num = pair["merged"]
        log = pr_credit_log.get(m_num)
        if not log:
            continue
        budget = CREDIT_DELTA_CAP - pr_total_shifted[m_num]
        if budget <= 0:
            continue
        shift_pct = min(pair["file_overlap"], budget)
        if shift_pct <= 0:
            continue
        pr_total_shifted[m_num] += shift_pct

        to_login = pair["closed_author"]
        to_display = GH_TO_NAME.get(to_login, to_login)
        applied  = []
        for from_login, share in log["shares"].items():
            if from_login == to_login or from_login in BOTS:
                continue
            amount   = share * shift_pct
            from_display = GH_TO_NAME.get(from_login, from_login)
            amount_w = pr_weighted_by_login.get(m_num, {}).get(from_display, 0) * shift_pct
            if amount <= 0 and amount_w <= 0:
                continue
            for s in log["slices"]:
                pr_slices[s][from_login]["pr_credit"] -= amount
                pr_slices[s][to_login]["pr_credit"] += amount
                if amount_w > 0:
                    weighted_lines_per_slice[s][from_display] -= amount_w
                    weighted_lines_per_slice[s][to_display]   += amount_w
            applied.append({"from": from_login, "to": to_login,
                            "amount": round(amount, 3),
                            "weighted": round(amount_w, 1)})

        if applied:
            credit_delta_log.append({
                "closed":        pair["closed"],
                "merged":        m_num,
                "shift_pct":     round(shift_pct, 2),
                "applied":       applied,
            })

    print(f"Similarity credit delta: applied to {len(credit_delta_log)} pairs "
          f"(cap {CREDIT_DELTA_CAP}, total entries {sum(len(p['applied']) for p in credit_delta_log)})",
          file=sys.stderr)
else:
    print("Similarity credit delta: disabled (set SIMILARITY_CREDIT_DELTA=1 to enable)",
          file=sys.stderr)

# Iteration credit delta: each fix-followup earns a small slice of credit from
# the iterated PR's contributors. Independent flag, independent cap.
ITERATION_CREDIT_DELTA = _flag("ITERATION_CREDIT_DELTA") and SHOW_ITERATION
if _flag("ITERATION_CREDIT_DELTA") and not SHOW_ITERATION:
    print("WARN: ITERATION_CREDIT_DELTA requires SHOW_ITERATION=1; disabled.",
          file=sys.stderr)
ITERATION_DELTA_PER_FIX = float(os.environ.get("ITERATION_DELTA_PER_FIX", "0.1"))
ITERATION_DELTA_CAP     = float(os.environ.get("ITERATION_DELTA_CAP",     "0.3"))

iteration_delta_log = []
if ITERATION_CREDIT_DELTA:
    for p_num, fixers in fix_followed_by.items():
        log = pr_credit_log.get(p_num)
        if not log or not fixers:
            continue
        already = 0.0
        for q_num, q_author in fixers:
            if q_author in BOTS:
                continue
            budget = ITERATION_DELTA_CAP - already
            if budget <= 0:
                break
            shift = min(ITERATION_DELTA_PER_FIX, budget)
            q_display = GH_TO_NAME.get(q_author, q_author)
            applied = []
            for from_login, share in log["shares"].items():
                if from_login == q_author or from_login in BOTS:
                    continue
                amount   = share * shift
                from_display = GH_TO_NAME.get(from_login, from_login)
                amount_w = pr_weighted_by_login.get(p_num, {}).get(from_display, 0) * shift
                if amount <= 0 and amount_w <= 0:
                    continue
                for s in log["slices"]:
                    pr_slices[s][from_login]["pr_credit"] -= amount
                    pr_slices[s][q_author]["pr_credit"]   += amount
                    if amount_w > 0:
                        weighted_lines_per_slice[s][from_display] -= amount_w
                        weighted_lines_per_slice[s][q_display]    += amount_w
                applied.append({"from": from_login, "to": q_author,
                                "amount": round(amount, 3),
                                "weighted": round(amount_w, 1)})
            if applied:
                already += shift
                iteration_delta_log.append({
                    "iterated":  p_num,
                    "fixer":     q_num,
                    "shift_pct": round(shift, 2),
                    "applied":   applied,
                })

    print(f"Iteration credit delta: applied {len(iteration_delta_log)} fix-followups "
          f"(per-fix {ITERATION_DELTA_PER_FIX}, cap {ITERATION_DELTA_CAP})",
          file=sys.stderr)
else:
    print("Iteration credit delta: disabled (set ITERATION_CREDIT_DELTA=1 to enable, requires SHOW_ITERATION=1)",
          file=sys.stderr)

# Reviewer credit shift: each non-self reviewer earns REVIEWER_CREDIT_RATE of
# the PR's credit and weighted_lines, capped per-PR at REVIEWER_CREDIT_CAP.
# Pulled proportionally from the PR's contributors (same pattern as iteration
# delta). Default rate=0 means no shift.
reviewer_delta_log = []
if REVIEWER_CREDIT_RATE > 0:
    for pr_num, attribution in pr_review_attribution.items():
        log = pr_credit_log.get(pr_num)
        if not log or not attribution:
            continue
        # Total fraction shifted to reviewers — capped.
        per_review_shift = min(REVIEWER_CREDIT_RATE, REVIEWER_CREDIT_CAP)
        total_shift = min(REVIEWER_CREDIT_CAP, per_review_shift * len(attribution))
        # Each reviewer's slice of the total
        reviewer_slices = {r: total_shift * share for r, share in attribution.items()}
        # Each PR contributor loses pro-rata
        applied = []
        for r_login, r_share in reviewer_slices.items():
            if r_login in BOTS:
                continue
            r_display = GH_TO_NAME.get(r_login, r_login)
            for from_login, share in log["shares"].items():
                if from_login == r_login or from_login in BOTS:
                    continue
                from_display = GH_TO_NAME.get(from_login, from_login)
                amount   = share * r_share
                amount_w = pr_weighted_by_login.get(pr_num, {}).get(from_display, 0) * r_share
                if amount <= 0 and amount_w <= 0:
                    continue
                for s in log["slices"]:
                    pr_slices[s][from_login]["pr_credit"]   -= amount
                    pr_slices[s][r_login]["pr_credit"]      += amount
                    if amount_w > 0:
                        weighted_lines_per_slice[s][from_display] -= amount_w
                        weighted_lines_per_slice[s][r_display]    += amount_w
                applied.append({"from": from_login, "to": r_login,
                                "amount": round(amount, 3),
                                "weighted": round(amount_w, 1)})
        if applied:
            reviewer_delta_log.append({
                "pr": pr_num,
                "shift_pct": round(total_shift, 2),
                "n_reviewers": len(attribution),
                "applied":  applied,
            })

    print(f"Reviewer credit delta: applied {len(reviewer_delta_log)} PRs "
          f"(rate {REVIEWER_CREDIT_RATE} per reviewer, cap {REVIEWER_CREDIT_CAP})",
          file=sys.stderr)
else:
    print("Reviewer credit delta: disabled (set REVIEWER_CREDIT_RATE>0 to enable)",
          file=sys.stderr)

# ─── Calendar enumeration per slice ──────────────────────────────────────────
# Always computed (used by both the per-slice sparkline and the optional
# streak/active-weeks columns). Each entry is the ordered list of ISO week
# strings that fall inside the slice window.

slice_calendar_weeks: dict[str, list[str]] = {}
all_time_start = earliest_activity_ts or NOW
for s, delta in SLICES.items():
    start = (NOW - delta) if delta is not None else all_time_start
    weeks: list[str] = []
    seen_w: set[str] = set()
    cur = start
    while cur <= NOW:
        wk = cur.strftime("%G-W%V")
        if wk not in seen_w:
            seen_w.add(wk)
            weeks.append(wk)
        cur += timedelta(days=1)
    slice_calendar_weeks[s] = weeks
print(f"Slice calendars: " +
      ", ".join(f"{s}={len(w)}w" for s, w in slice_calendar_weeks.items()),
      file=sys.stderr)

def streak_metrics(name: str, slice_name: str) -> dict:
    cal = slice_calendar_weeks.get(slice_name) or []
    activity = weekly_activity.get(name, set())
    active = [w in activity for w in cal]
    active_count = sum(active)
    max_run = run = 0
    for a in active:
        if a:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
    cur_run = 0
    for a in reversed(active):
        if a:
            cur_run += 1
        else:
            break
    return {
        "active_weeks":   active_count,
        "total_weeks":    len(cal),
        "streak_longest": max_run,
        "streak_current": cur_run,
    }

# ─── 10. Build the unified contributor records ────────────────────────────────
# Reads pr_slices AFTER credit deltas have been applied (if enabled).

all_names = set(slices_data["all"].keys())
for s in pr_slices:
    for login in pr_slices[s]:
        all_names.add(GH_TO_NAME.get(login, login))

contributors = {}
weeks_sorted = sorted(weeks_seen)

for name in all_names:
    if name in BOTS:
        continue
    gh = NAME_TO_GH.get(name, name)
    record = {
        "ghLogin": gh,
        "slices": {},
        "weighted_lines": round(weighted_lines.get(name, 0)),
        "top_files": top_files_per_author.get(name, []),
        "packages": dict(per_author_packages.get(name, {})),
        "sparkline": [commits_per_week[name].get(w, 0) for w in weeks_sorted],
    }
    for s in SLICES:
        gd = slices_data[s].get(name, empty_stats())
        pd = pr_slices[s].get(gh, empty_pr_stats())
        slice_rec = {
            "commits": gd["commits"],
            "commits_per_pr": (
                round(gd["commits"] / pd["prs"], 1)
                if pd["prs"] > 0 else None
            ),
            "weighted_lines": round(weighted_lines_per_slice[s].get(name, 0)),
            "sparkline": [commits_per_week[name].get(w, 0) for w in slice_calendar_weeks[s]],
            "weekly_metric": ([round(weekly_metric_value(name, w), 1)
                               for w in slice_calendar_weeks[s]]
                              if SHOW_WEEKLY_CHART else None),
            "add": gd["add"],
            "del": gd["del"],
            "net": gd["add"] - gd["del"],
            "files": gd["files_touched"] if isinstance(gd["files_touched"], int) else len(gd["files_touched"]),
            "prs": round(pd["pr_credit"], 1),       # credit-weighted: split by commit-author share
            "prs_raw": pd["prs"],                    # # of PRs opened by this user
            "reviews": round(pd["reviews"], 1) if isinstance(pd["reviews"], float) else pd["reviews"],
            "review_impact": round(review_impact_per_slice[s].get(name, 0)),
        }
        if SHOW_COMPOSITE_IMPACT:
            slice_rec["composite_impact"] = round(
                slice_rec["weighted_lines"] +
                COMPOSITE_REVIEW_ALPHA * slice_rec["review_impact"]
            )
        if SHOW_ITERATION:
            slice_rec["iteration_count"] = round(pd["pr_credit_iterated"], 1)
            slice_rec["iteration_rate"]  = (
                round(100 * pd["pr_credit_iterated"] / pd["pr_credit"], 1)
                if pd["pr_credit"] >= ITERATION_MIN_PRS else None
            )
        if SHOW_STREAK:
            slice_rec.update(streak_metrics(name, s))
        record["slices"][s] = slice_rec
    contributors[name] = record

contributors = {n: r for n, r in contributors.items()
                if any(s["commits"] or s["add"] or s["prs"] or s["reviews"] for s in r["slices"].values())}

print(f"Final contributors: {len(contributors)}", file=sys.stderr)

# ─── 11. Aggregate totals ──────────────────────────────────────────────────────

totals = {}
for s in SLICES:
    t = {"commits": 0, "add": 0, "prs": 0.0, "reviews": 0, "people": 0}
    for r in contributors.values():
        sl = r["slices"][s]
        t["commits"] += sl["commits"]
        t["add"] += sl["add"]
        # Sum credit-weighted PR count to match the PRs column underneath.
        # Credit deltas redistribute among non-bot contributors so the total
        # remains close to the raw PR count, but the unit now matches the row.
        t["prs"] += sl.get("prs", 0)
        t["reviews"] += sl["reviews"]
        if sl["commits"] or sl["add"] or sl["prs"] or sl["reviews"]:
            t["people"] += 1
    t["prs"] = round(t["prs"], 1)
    totals[s] = t

REPO_OWNER = os.environ.get("REPO_OWNER", "")
REPO_NAME  = os.environ.get("REPO_NAME", "")
repo_pr_url_base = (f"https://github.com/{REPO_OWNER}/{REPO_NAME}/pull/"
                    if REPO_OWNER and REPO_NAME else "")

# Standouts cards — comma-separated list of dimension codes, in render order.
# See render_html.py STANDOUT_DEFS for the full set. Default keeps the
# pre-config behavior so nothing breaks for adopters who don't set this.
DEFAULT_STANDOUTS = "top_impact,top_reviewer,highest_output,most_prs"
standouts_config = [
    s.strip()
    for s in os.environ.get("STANDOUTS", DEFAULT_STANDOUTS).split(",")
    if s.strip()
]

data = {
    "generated_at": NOW.isoformat(),
    "repo_first_commit": REPO_FIRST_COMMIT,
    "repo_pr_url_base": repo_pr_url_base,
    "standouts_config": standouts_config,
    "weeks": weeks_sorted,
    "totals": totals,
    "contributors": contributors,
    "top_hotspots": top_hotspots,
    "show_iteration": SHOW_ITERATION,
    "show_similarity": SHOW_SIMILARITY,
    "show_streak": SHOW_STREAK,
    "show_weekly_chart": SHOW_WEEKLY_CHART,
    "show_review_impact": SHOW_REVIEW_IMPACT,
    "show_composite_impact": SHOW_COMPOSITE_IMPACT,
    "weight_reviews_by_depth": WEIGHT_REVIEWS_BY_DEPTH,
    "reviewer_credit_rate":   REVIEWER_CREDIT_RATE if REVIEWER_CREDIT_RATE > 0 else None,
    "reviewer_delta_log":     reviewer_delta_log,
    "weekly_chart_metric": WEEKLY_CHART_METRIC if SHOW_WEEKLY_CHART else None,
    "weekly_chart_position": WEEKLY_CHART_POSITION if SHOW_WEEKLY_CHART else None,
    "weekly_chart_top_n": WEEKLY_CHART_TOP_N if SHOW_WEEKLY_CHART else None,
    "slice_calendar_weeks": slice_calendar_weeks if SHOW_WEEKLY_CHART else None,
    "slices": [{"code": code, "label": SLICE_LABELS[code]} for code in SLICES],
    "similarity_pairs": similarity_pairs,
    "credit_delta_enabled": CREDIT_DELTA_ENABLED,
    "credit_delta_cap": CREDIT_DELTA_CAP if CREDIT_DELTA_ENABLED else None,
    "credit_delta_log": credit_delta_log,
    "iteration_delta_enabled":   ITERATION_CREDIT_DELTA,
    "iteration_delta_per_fix":   ITERATION_DELTA_PER_FIX if ITERATION_CREDIT_DELTA else None,
    "iteration_delta_cap":       ITERATION_DELTA_CAP     if ITERATION_CREDIT_DELTA else None,
    "iteration_delta_log":       iteration_delta_log,
}

out_path = DATA_DIR / "report_data.json"
with open(out_path, "w") as f:
    json.dump(data, f, indent=2)

print(f"Wrote {out_path} ({len(json.dumps(data)):,} bytes)", file=sys.stderr)
