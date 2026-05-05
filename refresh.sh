#!/bin/bash
# Regenerate the contributor leaderboard.
#
# Usage:
#   ./refresh.sh --config <name>          # uses ./config/<name>.env
#   ./refresh.sh --config /abs/path.env   # uses an absolute path
#   ./refresh.sh --config <x> --deploy    # build, then rsync $OUT_DIR/ to $DEPLOY_TARGET
#
# Cron example (every 30 min, with an absolute config path):
#   */30 * * * *  /path/to/leaderboard/refresh.sh --config $HOME/.config/leaderboard/repo.env --deploy >> /tmp/leaderboard.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG=""
DEPLOY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --deploy) DEPLOY=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -n "$CONFIG" ] || { echo "usage: $0 --config <name|path> [--deploy]" >&2; exit 2; }

# Resolve config: absolute path wins, otherwise look in ./config/<name>.env
case "$CONFIG" in
  /*)  CONFIG_FILE="$CONFIG" ;;
  *)   CONFIG_FILE="$ROOT/config/$CONFIG.env" ;;
esac
[ -f "$CONFIG_FILE" ] || { echo "missing config: $CONFIG_FILE" >&2; exit 2; }

# shellcheck disable=SC1090
source "$CONFIG_FILE"

echo "[$(date -Iseconds)] Refreshing leaderboard ($CONFIG_FILE)..."
bash   "$ROOT/scripts/pull_all_data.sh"
python3 "$ROOT/scripts/build_report.py"
python3 "$ROOT/scripts/render_html.py"

if [ "$DEPLOY" = "1" ]; then
  : "${DEPLOY_TARGET:?DEPLOY_TARGET not set in config (format: user@host:/path/)}"
  echo "[$(date -Iseconds)] Deploying $OUT_DIR/ to $DEPLOY_TARGET ..."
  rsync -avz --delete "$OUT_DIR/" "$DEPLOY_TARGET"
fi

echo "[$(date -Iseconds)] Done."
