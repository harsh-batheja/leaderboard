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

# Detect "rework": PR numbers that were later reverted by another merged PR.
# Title patterns:  `Revert "..." (#N)`  or  `revert: ... (PR #N)` or `revert: ... PR #N`
# Body fallback:   `Reverts #N`  or  `Reverts owner/repo#N`
REVERT_TITLE_RE = re.compile(r'^\s*revert\b', re.IGNORECASE)
TITLE_HASH_RE   = re.compile(r'\(#(\d+)\)')
TITLE_PR_RE     = re.compile(r'\bPR\s*#(\d+)', re.IGNORECASE)
BODY_REVERTS_RE = re.compile(r'(?im)\breverts?\s+(?:[\w.-]+/[\w.-]+)?#(\d+)')

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

print(f"Rework signal: {len(reverted_pr_numbers)} PRs were later reverted",
      file=sys.stderr)

def empty_pr_stats():
    return {"prs": 0, "prs_reverted": 0, "reviews": 0,
            "ttm_seconds": 0, "ttm_count": 0,
            "pr_commit_total": 0, "pr_count_for_commits": 0}

pr_slices = {s: defaultdict(empty_pr_stats) for s in SLICES}

for pr in all_prs:
    if not pr.get("author"):
        continue
    author_login = pr["author"]["login"]
    if author_login in BOTS:
        continue
    # Re-attribute trigger-bot PRs to the human who filed the originating issue.
    if TRIGGER_BOT_LOGIN and author_login == TRIGGER_BOT_LOGIN:
        trigger = TRIGGER_BOT_TRIGGERS.get(pr["number"])
        if trigger:
            author_login = trigger
    merged_at = datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00"))
    created_at = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
    ttm_seconds = (merged_at - created_at).total_seconds()
    commits_count = pr.get("commits", {}).get("totalCount", 0)
    was_reverted = pr["number"] in reverted_pr_numbers

    for s in SLICES:
        if not in_slice(merged_at, s):
            continue
        stats = pr_slices[s][author_login]
        stats["prs"] += 1
        if was_reverted:
            stats["prs_reverted"] += 1
        stats["ttm_seconds"] += ttm_seconds
        stats["ttm_count"] += 1
        if commits_count:
            stats["pr_commit_total"] += commits_count
            stats["pr_count_for_commits"] += 1

    # Reviews
    for review in pr.get("reviews", {}).get("nodes", []):
        if not review.get("author"):
            continue
        r_login = review["author"]["login"]
        if r_login in BOTS or r_login == author_login:  # don't count self-reviews
            continue
        r_ts = datetime.fromisoformat(review["submittedAt"].replace("Z", "+00:00")) if review.get("submittedAt") else merged_at
        for s in SLICES:
            if in_slice(r_ts, s):
                pr_slices[s][r_login]["reviews"] += 1

print(f"Parsed PR data: {len(pr_slices['all'])} active gh logins all-time", file=sys.stderr)

# Minimum PR sample for the rework rate to be displayed (else None).
REWORK_MIN_PRS = 5

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

# ─── 7. Build the unified contributor records ──────────────────────────────────

# Combine git + PR data via NAME_TO_GH / GH_TO_NAME mapping.
# Canonical key = display name. For PR-only contributors with no git presence,
# the gh login becomes the display name.

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
        # PR data — match by gh login (handle the same login mapping to multiple names)
        pd = pr_slices[s].get(gh, empty_pr_stats())
        rework_rate = (round(100 * pd["prs_reverted"] / pd["prs"], 1)
                       if pd["prs"] >= REWORK_MIN_PRS else None)
        record["slices"][s] = {
            "commits": gd["commits"],
            "add": gd["add"],
            "del": gd["del"],
            "net": gd["add"] - gd["del"],
            "files": gd["files_touched"] if isinstance(gd["files_touched"], int) else len(gd["files_touched"]),
            "prs": pd["prs"],
            "reviews": pd["reviews"],
            "rework_count": pd["prs_reverted"],
            "rework_rate": rework_rate,
        }
    contributors[name] = record

# Drop completely empty contributors
contributors = {n: r for n, r in contributors.items()
                if any(s["commits"] or s["add"] or s["prs"] or s["reviews"] for s in r["slices"].values())}

print(f"Final contributors: {len(contributors)}", file=sys.stderr)

# ─── 8. Aggregate stats ────────────────────────────────────────────────────────

totals = {}
for s in SLICES:
    t = {"commits": 0, "add": 0, "prs": 0, "reviews": 0, "people": 0}
    for r in contributors.values():
        sl = r["slices"][s]
        t["commits"] += sl["commits"]
        t["add"] += sl["add"]
        t["prs"] += sl["prs"]
        t["reviews"] += sl["reviews"]
        if sl["commits"] or sl["add"] or sl["prs"] or sl["reviews"]:
            t["people"] += 1
    totals[s] = t

# ─── 9. Top hotspots for context display ───────────────────────────────────────

top_hotspots = [{"path": p, "commits": c, "weight": round(file_weight.get(p, 1.0), 2)}
                for p, c in sorted(file_churn.items(), key=lambda x: -x[1])[:15]]

data = {
    "generated_at": NOW.isoformat(),
    "repo_first_commit": REPO_FIRST_COMMIT,
    "weeks": weeks_sorted,
    "totals": totals,
    "contributors": contributors,
    "top_hotspots": top_hotspots,
}

out_path = DATA_DIR / "report_data.json"
with open(out_path, "w") as f:
    json.dump(data, f, indent=2)

print(f"Wrote {out_path} ({len(json.dumps(data)):,} bytes)", file=sys.stderr)
