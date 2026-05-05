#!/usr/bin/env python3
"""Process all data sources into a JSON blob + render the HTML report."""

import json, os, re, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/_data"))
REPO_FIRST_COMMIT = os.environ.get("REPO_FIRST_COMMIT", "")

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
SLICES = {
    "all": None,          # all-time
    "d30": timedelta(days=30),
    "d7":  timedelta(days=7),
}

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

# Commits per week per author (for sparkline)
commits_per_week = defaultdict(lambda: defaultdict(int))  # name -> iso-week -> count
weeks_seen = set()

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
            if current_author in BOTS:
                current_author = None
                continue
            week = current_ts.strftime("%Y-W%V")
            weeks_seen.add(week)
            commits_per_week[current_author][week] += 1
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
                # All-time per-file per-author tracking
                per_file_author_add[path][current_author] += add
                per_author_file_add[current_author][path] += add

# Convert sets to counts before serializing
for s in slices_data:
    for name in slices_data[s]:
        slices_data[s][name]["files_touched"] = len(slices_data[s][name]["files_touched"])

print(f"Parsed git data: {len(slices_data['all'])} authors all-time, "
      f"{len(slices_data['d30'])} active in last 30d", file=sys.stderr)

# ─── 2. Parse PR data + reviews ────────────────────────────────────────────────

# Load all PRs into memory (small — typically a few hundred to low thousands of rows).
all_prs = []
with open(DATA_DIR / "prs.jsonl") as f:
    for line in f:
        all_prs.append(json.loads(line))

# Detect "iteration": a PR is iterated-on if either
#   (a) a later merged Revert PR points at it, OR
#   (b) a later merged "fix"-titled PR by a different author touches ≥ N of its
#       files within the iteration window (default 30 days).
REVERT_TITLE_RE = re.compile(r'^\s*revert\b', re.IGNORECASE)
TITLE_HASH_RE   = re.compile(r'\(#(\d+)\)')
TITLE_PR_RE     = re.compile(r'\bPR\s*#(\d+)', re.IGNORECASE)
BODY_REVERTS_RE = re.compile(r'(?im)\breverts?\s+(?:[\w.-]+/[\w.-]+)?#(\d+)')
FIX_TITLE_RE    = re.compile(r'^\s*fix\b', re.IGNORECASE)

ITERATION_WINDOW_DAYS = int(os.environ.get("ITERATION_WINDOW_DAYS", "30"))
ITERATION_MIN_SHARED  = int(os.environ.get("ITERATION_MIN_SHARED",  "2"))

# (a) Strict reverts via title/body cross-reference
reverted_pr_numbers: set[int] = set()
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
        reverted_pr_numbers.add(target)

# (b) Fix-followed: load file lists from pr_files.jsonl (already pulled for
# the similarity scan) and walk merged PRs in time order.
files_by_pr: dict[int, set] = {}
try:
    with open(DATA_DIR / "pr_files.jsonl") as f:
        for line in f:
            p = json.loads(line)
            files_by_pr[p["number"]] = set(
                n["path"] for n in p.get("files", {}).get("nodes", [])
                if n.get("path")
            )
except FileNotFoundError:
    pass

merged_sorted = sorted(
    [pr for pr in all_prs if pr.get("mergedAt")],
    key=lambda pr: pr["mergedAt"],
)
fix_followed_pr_numbers: set[int] = set()
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
        q_files = files_by_pr.get(q["number"], set())
        if len(p_files & q_files) >= ITERATION_MIN_SHARED:
            fix_followed_pr_numbers.add(p_num)
            break

iterated_pr_numbers = reverted_pr_numbers | fix_followed_pr_numbers
print(f"Iteration signal: {len(iterated_pr_numbers)} PRs iterated on "
      f"({len(reverted_pr_numbers)} reverts + {len(fix_followed_pr_numbers)} fix-followed)",
      file=sys.stderr)

def empty_pr_stats():
    return {"prs": 0, "pr_credit": 0.0,
            "prs_iterated": 0, "pr_credit_iterated": 0.0,
            "reviews": 0,
            "ttm_seconds": 0, "ttm_count": 0,
            "pr_commit_total": 0, "pr_count_for_commits": 0}

pr_slices = {s: defaultdict(empty_pr_stats) for s in SLICES}

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
    if pr_author_login in BOTS:
        continue

    # Re-attribute trigger-bot PRs to the human who filed the originating issue.
    # In that case the *whole* PR (including line credit) goes to the trigger;
    # the bot's commits are not the human's but they're acting on the human's behalf.
    if TRIGGER_BOT_LOGIN and pr_author_login == TRIGGER_BOT_LOGIN:
        trigger = TRIGGER_BOT_TRIGGERS.get(pr["number"])
        if trigger:
            pr_author_login = trigger
            shares = {trigger: 1.0}
        else:
            shares = {pr_author_login: 1.0}
    else:
        shares = compute_pr_shares(pr, pr_author_login)

    merged_at  = datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00"))
    created_at = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
    ttm_seconds = (merged_at - created_at).total_seconds()
    commits_count = pr.get("commits", {}).get("totalCount", 0)
    was_iterated = pr["number"] in iterated_pr_numbers

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
        pr_credit_log[pr["number"]] = {"shares": dict(shares), "slices": active_slices}

    # Reviews
    for review in pr.get("reviews", {}).get("nodes", []):
        if not review.get("author"):
            continue
        r_login = review["author"]["login"]
        if r_login in BOTS or r_login == pr_author_login:  # don't count self-reviews
            continue
        r_ts = datetime.fromisoformat(review["submittedAt"].replace("Z", "+00:00")) if review.get("submittedAt") else merged_at
        for s in SLICES:
            if in_slice(r_ts, s):
                pr_slices[s][r_login]["reviews"] += 1

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

# ─── 4. Per-author weighted lines (all-time) ──────────────────────────────────

weighted_lines = defaultdict(float)
for name, files in per_author_file_add.items():
    for path, add in files.items():
        weighted_lines[name] += add * file_weight.get(path, 1.0)

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

similarity_pairs = []
if pr_files:
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

# ─── 9. Optional: apply similarity-based credit deltas ────────────────────────
# Off by default. When enabled, for each flagged pair where the merged PR came
# AFTER the closed PR, shift `jaccard × per-author share` of the merged PR's
# credit from each merged-PR contributor to the closed-PR author. Total shift
# per merged PR is capped at SIMILARITY_DELTA_CAP. Iteration attribution does
# NOT shift — fixes follow whoever wrote the merged code.

CREDIT_DELTA_ENABLED = os.environ.get("SIMILARITY_CREDIT_DELTA", "0") == "1"
CREDIT_DELTA_CAP     = float(os.environ.get("SIMILARITY_DELTA_CAP", "0.4"))

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
        applied  = []
        for from_login, share in log["shares"].items():
            if from_login == to_login or from_login in BOTS:
                continue
            amount = share * shift_pct
            if amount <= 0:
                continue
            for s in log["slices"]:
                pr_slices[s][from_login]["pr_credit"] -= amount
                pr_slices[s][to_login]["pr_credit"] += amount
            applied.append({"from": from_login, "to": to_login, "amount": round(amount, 3)})

        if applied:
            credit_delta_log.append({
                "closed":        pair["closed"],
                "merged":        m_num,
                "shift_pct":     round(shift_pct, 2),
                "applied":       applied,
            })

    print(f"Credit delta: applied to {len(credit_delta_log)} pairs "
          f"(cap {CREDIT_DELTA_CAP}, total entries {sum(len(p['applied']) for p in credit_delta_log)})",
          file=sys.stderr)
else:
    print("Credit delta: disabled (set SIMILARITY_CREDIT_DELTA=1 to enable)",
          file=sys.stderr)

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
        iteration_rate = (round(100 * pd["pr_credit_iterated"] / pd["pr_credit"], 1)
                          if pd["pr_credit"] >= ITERATION_MIN_PRS else None)
        record["slices"][s] = {
            "commits": gd["commits"],
            "add": gd["add"],
            "del": gd["del"],
            "net": gd["add"] - gd["del"],
            "files": gd["files_touched"] if isinstance(gd["files_touched"], int) else len(gd["files_touched"]),
            "prs": round(pd["pr_credit"], 1),       # credit-weighted: split by commit-author share
            "prs_raw": pd["prs"],                    # # of PRs opened by this user
            "reviews": pd["reviews"],
            "iteration_count": round(pd["pr_credit_iterated"], 1),
            "iteration_rate": iteration_rate,
        }
    contributors[name] = record

contributors = {n: r for n, r in contributors.items()
                if any(s["commits"] or s["add"] or s["prs"] or s["reviews"] for s in r["slices"].values())}

print(f"Final contributors: {len(contributors)}", file=sys.stderr)

# ─── 11. Aggregate totals ──────────────────────────────────────────────────────

totals = {}
for s in SLICES:
    t = {"commits": 0, "add": 0, "prs": 0, "reviews": 0, "people": 0}
    for r in contributors.values():
        sl = r["slices"][s]
        t["commits"] += sl["commits"]
        t["add"] += sl["add"]
        t["prs"] += sl.get("prs_raw", 0)
        t["reviews"] += sl["reviews"]
        if sl["commits"] or sl["add"] or sl["prs"] or sl["reviews"]:
            t["people"] += 1
    totals[s] = t

REPO_OWNER = os.environ.get("REPO_OWNER", "")
REPO_NAME  = os.environ.get("REPO_NAME", "")
repo_pr_url_base = (f"https://github.com/{REPO_OWNER}/{REPO_NAME}/pull/"
                    if REPO_OWNER and REPO_NAME else "")

data = {
    "generated_at": NOW.isoformat(),
    "repo_first_commit": REPO_FIRST_COMMIT,
    "repo_pr_url_base": repo_pr_url_base,
    "weeks": weeks_sorted,
    "totals": totals,
    "contributors": contributors,
    "top_hotspots": top_hotspots,
    "similarity_pairs": similarity_pairs,
    "credit_delta_enabled": CREDIT_DELTA_ENABLED,
    "credit_delta_cap": CREDIT_DELTA_CAP if CREDIT_DELTA_ENABLED else None,
    "credit_delta_log": credit_delta_log,
}

out_path = DATA_DIR / "report_data.json"
with open(out_path, "w") as f:
    json.dump(data, f, indent=2)

print(f"Wrote {out_path} ({len(json.dumps(data)):,} bytes)", file=sys.stderr)
