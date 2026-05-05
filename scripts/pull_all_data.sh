#!/bin/bash
# Pull every dataset we need into $DATA_DIR.
# Requires env vars from config/<repo>.env: REPO_DIR, REPO_OWNER, REPO_NAME, DATA_DIR.
set -euo pipefail

: "${REPO_DIR:?REPO_DIR not set; source config/<repo>.env}"
: "${REPO_OWNER:?REPO_OWNER not set}"
: "${REPO_NAME:?REPO_NAME not set}"
: "${DATA_DIR:?DATA_DIR not set}"

cd "$REPO_DIR"
mkdir -p "$DATA_DIR"

# 1. Time-stamped commits + numstat. Format:
#    COMMIT<TAB>sha<TAB>iso-timestamp<TAB>author-name
#    add<TAB>del<TAB>path
git log --no-merges --use-mailmap --pretty=tformat:'COMMIT	%H	%aI	%aN' --numstat \
  > "$DATA_DIR/commits_numstat.tsv"

# 2. Per-file commit count (last 90 days) for hotspot weights
git log --no-merges --use-mailmap --since="90 days ago" --name-only \
  --pretty=format:'' | sort | uniq -c | sort -rn \
  > "$DATA_DIR/file_churn_90d.txt"

# 3. Per-file commit count (all-time) — fallback weight
git log --no-merges --use-mailmap --name-only --pretty=format:'' \
  | sort | uniq -c | sort -rn > "$DATA_DIR/file_churn_all.txt"

# 4. Per-file per-author lines (heavy — only top files matter, but cheap to compute fully)
git log --no-merges --use-mailmap --pretty=tformat:'COMMIT	%aN' --numstat \
  > "$DATA_DIR/per_file_per_author.tsv"

# 5. PRs with reviews + commit counts via GraphQL
cat > "$DATA_DIR/query.graphql" <<GQL
query(\$cursor: String) {
  repository(owner: "$REPO_OWNER", name: "$REPO_NAME") {
    pullRequests(first: 50, after: \$cursor, states: MERGED, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        author { login }
        createdAt
        mergedAt
        additions
        deletions
        commits { totalCount }
        reviews(first: 50) {
          nodes {
            author { login }
            state
            submittedAt
          }
        }
      }
    }
  }
}
GQL

: > "$DATA_DIR/prs.jsonl"
cursor="null"
page=0
while true; do
  page=$((page+1))
  echo "  Fetching PR page $page (cursor=$cursor)..." >&2
  if [ "$cursor" = "null" ]; then
    resp=$(gh api graphql -F query=@"$DATA_DIR/query.graphql")
  else
    resp=$(gh api graphql -F query=@"$DATA_DIR/query.graphql" -f cursor="$cursor")
  fi
  echo "$resp" | jq -c '.data.repository.pullRequests.nodes[]' >> "$DATA_DIR/prs.jsonl"
  has_next=$(echo "$resp" | jq -r '.data.repository.pullRequests.pageInfo.hasNextPage')
  cursor=$(echo "$resp" | jq -r '.data.repository.pullRequests.pageInfo.endCursor')
  if [ "$has_next" != "true" ]; then break; fi
done
echo "  PRs fetched: $(wc -l < "$DATA_DIR/prs.jsonl")" >&2

# 6. Optional: resolve trigger-bot PR attributions
# If TRIGGER_BOT_LOGIN is set, list PRs opened by that account and try to map
# each one back to the human who triggered it. Resolution priority:
#   1. PR body: "Requested by @user" / "Reported by @user" / etc.
#   2. PR body has "Closes #N" AND issue creator is a human (not the bot itself)
#   3. Otherwise: skip (PR stays attributed to the bot)
# Optional TRIGGER_BOT_MENTIONS_FILE maps short @mentions to canonical gh logins.
if [ -n "${TRIGGER_BOT_LOGIN:-}" ]; then
  echo "  Resolving $TRIGGER_BOT_LOGIN PR triggers..." >&2
  : > "$DATA_DIR/trigger_bot_triggers.tsv"
  gh pr list --repo "$REPO_OWNER/$REPO_NAME" --author "$TRIGGER_BOT_LOGIN" --state merged --limit 200 \
    --json number,body 2>/dev/null \
    | REPO_OWNER="$REPO_OWNER" REPO_NAME="$REPO_NAME" \
      TRIGGER_BOT_LOGIN="$TRIGGER_BOT_LOGIN" \
      TRIGGER_BOT_MENTIONS_FILE="${TRIGGER_BOT_MENTIONS_FILE:-}" \
      python3 -c "
import json, os, re, sys, subprocess

REPO = f\"{os.environ['REPO_OWNER']}/{os.environ['REPO_NAME']}\"
BOT  = os.environ['TRIGGER_BOT_LOGIN']

mentions_file = os.environ.get('TRIGGER_BOT_MENTIONS_FILE', '')
DISPLAY_TO_GH = json.load(open(mentions_file)) if mentions_file and os.path.exists(mentions_file) else {}

TRIGGER_RE = re.compile(
    r'(?i)\b(?:requested|reported|filed|raised|triggered|asked)\s*by:?\s*\*{0,2}\s*@([a-zA-Z0-9_-]+)'
)

def gh_get(path):
    r = subprocess.run(['gh', 'api', path], capture_output=True, text=True, timeout=10)
    return json.loads(r.stdout) if r.returncode == 0 else None

def normalize(u):
    return DISPLAY_TO_GH.get(u, u)

prs = json.load(sys.stdin)
for pr in prs:
    body = pr.get('body') or ''
    m = TRIGGER_RE.search(body)
    if m:
        print(f\"{pr['number']}\t{normalize(m.group(1))}\")
        continue
    m = re.search(r'(?i)\b(?:closes|fixes|resolves)\s+#(\d+)', body)
    if not m: continue
    issue_num = m.group(1)
    issue = gh_get(f'repos/{REPO}/issues/{issue_num}')
    if not issue: continue
    issue_body = issue.get('body') or ''
    m = TRIGGER_RE.search(issue_body)
    if m:
        print(f\"{pr['number']}\t{normalize(m.group(1))}\")
        continue
    creator = (issue.get('user') or {}).get('login', '')
    if creator and creator != BOT:
        print(f\"{pr['number']}\t{creator}\")
" >> "$DATA_DIR/trigger_bot_triggers.tsv"
  echo "  Triggers resolved: $(wc -l < "$DATA_DIR/trigger_bot_triggers.tsv")" >&2
fi

ls -la "$DATA_DIR/"
