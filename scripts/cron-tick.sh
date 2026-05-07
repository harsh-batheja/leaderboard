#!/bin/bash
# Cron-friendly wrapper around refresh.sh for unattended generation hosts.
#
# Self-updates the leaderboard repo and the tracked source repo, then runs
# refresh.sh against the given config. refresh.sh itself stays unchanged so
# manual / dev runs don't touch git state.
#
# Usage:
#   cron-tick.sh <abs-config-path>
#
# Cron example (every 30 min):
#   */30 * * * *  /home/harsh/projects/leaderboard/scripts/cron-tick.sh \
#                 /home/harsh/.config/leaderboard/ao.env >> /tmp/leaderboard.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:?usage: cron-tick.sh <abs-config-path>}"
[ -f "$CONFIG" ] || { echo "missing config: $CONFIG" >&2; exit 2; }

# shellcheck disable=SC1090
source "$CONFIG"

echo "[$(date -Iseconds)] cron-tick: self-updating leaderboard repo at $ROOT"
git -C "$ROOT" pull --ff-only --quiet || echo "WARN: leaderboard self-update failed" >&2

if [ -n "${REPO_DIR:-}" ] && [ -d "$REPO_DIR/.git" ]; then
  echo "[$(date -Iseconds)] cron-tick: refreshing tracked repo at $REPO_DIR"
  git -C "$REPO_DIR" fetch --quiet origin main
  git -C "$REPO_DIR" reset --hard --quiet origin/main
fi

exec "$ROOT/refresh.sh" --config "$CONFIG"
